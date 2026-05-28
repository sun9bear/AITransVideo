"""Phase 4.3b-C — temporary voice cleanup audit emitter (gateway-side JSONL).

清理 sweeper / CLI 每处理一个音色（成功 / 失败 / give-up / dry-run）落一行
JSONL 到 ``<runtime_logs>/express_voice_cleanup.jsonl`` + runtime log。

**纯本地 sidecar + log**，绝不打外部 API。写失败只 log（非致命）——audit 是
排障用，不能因写不进盘就影响清理。注入给 ``cleanup_expired_temporary_voices``
的 ``audit_emit`` 回调。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUDIT_KIND = "express_temp_voice_cleanup"
AUDIT_PHASE_VERSION = "4.3b"
_AUDIT_FILENAME = "express_voice_cleanup.jsonl"
# 与 docker-compose 的 AIVIDEOTRANS_RUNTIME_LOGS_DIR 对齐（CLAUDE.md）。
_DEFAULT_RUNTIME_LOGS_DIR = "/opt/aivideotrans/data/runtime_logs"

# decision 取值（与 cleanup core 的 _safe_audit 调用对齐）
_VALID_DECISIONS = frozenset(
    {"cleaned", "cleanup_failed", "cleanup_give_up", "dry_run"}
)


def _runtime_logs_dir() -> Path:
    return Path(
        os.environ.get("AIVIDEOTRANS_RUNTIME_LOGS_DIR", _DEFAULT_RUNTIME_LOGS_DIR)
    )


def emit_voice_cleanup_audit(
    *,
    decision: str,
    voice_id: str,
    user_id: str | None = None,
    error: str | None = None,
    dry_run: bool = False,
    **extra: Any,
) -> None:
    """追加一行清理决策 JSONL。decision ∈ cleaned / cleanup_failed /
    cleanup_give_up / dry_run。写盘失败非致命（吞 + log）。"""
    record: dict[str, Any] = {
        "kind": AUDIT_KIND,
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase_version": AUDIT_PHASE_VERSION,
        "decision": decision,
        "voice_id": voice_id,
        "user_id": user_id,
        "error": error,
        "dry_run": bool(dry_run),
    }
    if extra:
        record.update(extra)

    logger.info(
        "express temp voice cleanup: decision=%s voice=%s user=%s error=%s dry_run=%s",
        decision, voice_id, user_id, error, dry_run,
    )
    try:
        audit_dir = _runtime_logs_dir()
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / _AUDIT_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("failed to append express voice cleanup audit JSONL (non-fatal)")


__all__ = ["emit_voice_cleanup_audit", "AUDIT_KIND", "AUDIT_PHASE_VERSION"]
