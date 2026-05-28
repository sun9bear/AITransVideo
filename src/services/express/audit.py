"""Express auto-clone 审计 emitter（spec §9）。

每个 Express auto-clone 决策写一行 JSONL 到
``<project_dir>/audit/express_decisions.jsonl`` + runtime log。

**边界（Codex PR2-E）**：只写**本地 sidecar 文件 + log**，绝不打任何外部
API。审计写失败只 log（非致命）—— 审计是排障用，不能因为写不进盘就炸主流程。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUDIT_KIND = "express_auto_clone_decision"
AUDIT_PHASE_VERSION = "4.3a"
_AUDIT_SUBDIR = "audit"
_AUDIT_FILENAME = "express_decisions.jsonl"


def emit_express_clone_audit(project_dir, fields: dict[str, Any] | None = None) -> None:
    """追加一行 JSONL 决策记录。

    自动补 ``kind`` / ``ts`` / ``phase_version``；``fields`` 提供 decision /
    reason_code / main_speaker_* / voice_id / reservation_id 等（spec §9 schema）。
    """
    record: dict[str, Any] = {
        "kind": AUDIT_KIND,
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase_version": AUDIT_PHASE_VERSION,
    }
    if fields:
        record.update(fields)

    # runtime log（排障可见，不含敏感样本字节）
    logger.info(
        "express auto-clone: job=%s decision=%s reason=%s speaker=%s voice=%s",
        record.get("job_id"),
        record.get("decision"),
        record.get("reason_code"),
        record.get("main_speaker_id"),
        record.get("voice_id"),
    )

    try:
        audit_dir = Path(project_dir) / _AUDIT_SUBDIR
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / _AUDIT_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # 非致命：审计落盘失败不影响 pipeline / 不改 routing。
        logger.exception("failed to append express auto-clone audit JSONL (non-fatal)")


__all__ = ["emit_express_clone_audit", "AUDIT_KIND", "AUDIT_PHASE_VERSION"]
