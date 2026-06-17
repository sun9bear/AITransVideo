"""匿名 express Pass 3 诚实失败的 per-scope 配额退还（plan 2026-06-12 §E）。

触发链：pipeline 在 artifact 判定失败时 emit ``[SMART_STATE]
{"anon_pass3_failed": true}`` marker → runner 写进 JobRecord.smart_state →
``mirror_job_terminal_state``（终态结算单一入口，见
feedback_terminal_state_single_entry 教训）镜像到 PG Job.smart_state →
本模块在匿名分支按 marker 精确退还。

退还范围（§E 裁定）：**仅 per-scope per-mode 行**（ip/device/source ×
lane，各 1 次/日）——使该匿名身份当日额度不被诚实失败烧掉；global 总闸
与 express 全局子闸**不退**（防刷失败穿透成本闸）。可退行清单在 intake
时由 ``LaneAwareCounterStore.acquired_mode_scope_keys`` 落进 record audit
（``quota_mode_rows``，HMAC 复合键、无原始 PII）。

幂等：record.audit ``pass3_quota_refund`` 标记——mirror 是 level-triggered
（轮询反复观察同一终态），退还必须恰好一次。

Import constraints：不 import ``services.jobs`` / ``src.pipeline``
（gateway 容器无 pydub）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select, text

from anonymous_preview_intake_wiring import ANON_PREVIEW_COUNTER_SCOPE
from models import AnonymousPreviewRecord

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

    from models import Job

logger = logging.getLogger(__name__)

_AUDIT_FILENAME = "anonymous_preview_audit.jsonl"
_DEFAULT_RUNTIME_LOGS_DIR = "/opt/aivideotrans/data/runtime_logs"

_REFUND_SQL = text(
    """
    UPDATE anonymous_preview_daily_usage
    SET count = GREATEST(count - 1, 0),
        updated_at = now()
    WHERE scope = :scope AND scope_key = :key
      AND mode = :mode AND usage_date = :day
    """
)


def _append_refund_audit_jsonl(row: dict) -> None:
    """best-effort 审计行（与 sweeper 同文件 anonymous_preview_audit.jsonl）。"""
    try:
        audit_dir = Path(
            os.environ.get("AIVIDEOTRANS_RUNTIME_LOGS_DIR", _DEFAULT_RUNTIME_LOGS_DIR)
        )
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / _AUDIT_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 审计写失败不阻断退还
        logger.exception("anon_pass3_refund: audit JSONL append failed (non-fatal)")


async def refund_pass3_failed_quota(db: "AsyncSession", db_job: "Job") -> bool:
    """终态 failed 的匿名 job：按 pass3 marker 退还 per-scope per-mode 配额。

    返回 True 表示有状态变更（record audit 更新 / 计数回退），调用方
    （mirror）负责 commit。任何一步异常上抛给调用方记 warning——退还失败
    不得阻断状态镜像（mirror 侧 try/except 包裹）。
    """
    smart_state = dict(getattr(db_job, "smart_state", None) or {})
    if smart_state.get("anon_pass3_failed") is not True:
        return False

    result = await db.execute(
        select(AnonymousPreviewRecord).where(
            AnonymousPreviewRecord.job_id == db_job.job_id
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        logger.warning(
            "anon_pass3_refund: no preview record for job=%s — skip",
            db_job.job_id,
        )
        return False

    audit = dict(record.audit or {})
    if audit.get("pass3_quota_refund"):
        return False  # 幂等：已处理过（done / skipped_no_keys）

    rows = audit.get("quota_mode_rows") or []
    refunded = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        scope_key = str(row.get("scope_key") or "")
        mode = str(row.get("mode") or "")
        day = str(row.get("day") or "")
        if not (scope_key and mode and day):
            continue
        await db.execute(
            _REFUND_SQL,
            {
                "scope": ANON_PREVIEW_COUNTER_SCOPE,
                "key": scope_key,
                "mode": mode,
                "day": day,
            },
        )
        refunded += 1

    audit["pass3_failed"] = True
    audit["pass3_quota_refund"] = "done" if refunded else "skipped_no_keys"
    audit["pass3_quota_refund_rows"] = refunded
    record.audit = audit

    logger.info(
        "anon_pass3_refund: job=%s preview=%s refunded_rows=%d",
        db_job.job_id, record.preview_id, refunded,
    )
    _append_refund_audit_jsonl(
        {
            "kind": "anon_pass3_quota_refund",
            "ts": datetime.now(timezone.utc).isoformat(),
            "preview_id": record.preview_id,
            "job_id": db_job.job_id,
            "mode": record.mode,
            "voice_profile_missing": True,
            "pass3_failed": True,
            "refunded_rows": refunded,
        }
    )
    return True
