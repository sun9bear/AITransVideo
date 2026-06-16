"""APF P0 T9 — Anonymous Preview TTL & media cleanup sweeper.

周期后台任务（gateway lifespan），处理两条生命周期：

(a) **block/reject 终局 record** 即删媒体文件（audit 标 media_purged_at；
    record 行留存供 30d 审计）。
(b) **过期 record**（expires_at < now）→ 删媒体 → 追加 JSONL → 删 record 行。
(c) **过期 anonymous_sessions 行**删除。
(d) **anonymous_preview_daily_usage 中 usage_date < today-7** 的行删除。

**边界（F16 / plan T9）**：

- **job 工作区不碰**：有 job_id 的 record 只删 gateway 侧上传/teaser 文件与
  record 行，绝不触碰 ``projects/`` / ``jobs/`` 路径。
- **审计 JSONL 仅写**: ``preview_id / status / status_reason / source_hash /
  mode / created_at / expires_at / anonymous_consent / media_purged_at``；
  无转录文本、无 raw IP、无媒体内容。
- **不 import services.jobs**：gateway 容器不装 pydub；services.jobs 传染依赖。
- **单条失败 log warning + continue**；DB 不可用 → 整轮返回（不 raise）。
- **fail-safe**：sweeper_loop 内任何 tick 异常只 log，绝不崩 gateway。

默认间隔 600 s（保守，10 分钟）。env 可调。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_S = int(
    os.environ.get("AVT_ANON_PREVIEW_SWEEP_INTERVAL_S", "600")
)
INITIAL_DELAY_S = int(
    os.environ.get("AVT_ANON_PREVIEW_SWEEP_INITIAL_DELAY_S", "120")
)
# Usage rows older than this many days are purged
USAGE_RETENTION_DAYS = int(
    os.environ.get("AVT_ANON_PREVIEW_USAGE_RETENTION_DAYS", "7")
)

_AUDIT_FILENAME = "anonymous_preview_audit.jsonl"
_DEFAULT_RUNTIME_LOGS_DIR = "/opt/aivideotrans/data/runtime_logs"

# Statuses that are terminal "block/reject" states — media should be deleted
# immediately (the record row is kept for 30d audit).
_BLOCK_STATUSES = frozenset(
    {
        "rejected",
        "failed",
        "rate_limited",
        "content_blocked",
        "admission_rejected",
        "error",
        "cancelled",
    }
)

# Fields written to audit JSONL — no transcription text, no raw IP, no media.
_AUDIT_JSONL_FIELDS = (
    "preview_id",
    "status",
    "status_reason",
    "source_hash",
    "mode",
    "created_at",
    "expires_at",
    "anonymous_consent",
    "media_purged_at",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _runtime_logs_dir() -> Path:
    return Path(
        os.environ.get("AIVIDEOTRANS_RUNTIME_LOGS_DIR", _DEFAULT_RUNTIME_LOGS_DIR)
    )


def _safe_unlink(path_str: str | None) -> bool:
    """Delete a file if it exists.  Returns True if deleted, False otherwise.
    Swallows all exceptions (logs warning on unexpected failure)."""
    if not path_str:
        return False
    try:
        p = Path(path_str)
        if p.is_file():
            p.unlink()
            return True
    except Exception:
        logger.warning(
            "anonymous_preview_sweeper: failed to delete file %r", path_str, exc_info=True
        )
    return False


def _purge_media_files(audit: dict | None) -> bool:
    """Delete stored_upload_path and teaser_path from the audit dict.

    Returns True if at least one file was actually deleted.
    """
    if not audit:
        return False
    deleted_any = False
    deleted_any |= _safe_unlink(audit.get("stored_upload_path"))
    deleted_any |= _safe_unlink(audit.get("teaser_path"))
    return deleted_any


def _append_audit_jsonl(record: Any, media_purged_at: str) -> None:
    """Append one line to the anonymous_preview_audit.jsonl.

    Only safe fields are written (see _AUDIT_JSONL_FIELDS). Write failure is
    non-fatal (logged, not raised).
    """
    audit_sub: dict = record.audit or {} if record.audit else {}

    row: dict[str, Any] = {
        "kind": "anonymous_preview_sweeper",
        "ts": datetime.now(timezone.utc).isoformat(),
        "preview_id": record.preview_id,
        "status": record.status,
        "status_reason": record.status_reason,
        "source_hash": record.source_hash,
        "mode": record.mode,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        # anonymous_consent is stored inside audit JSONB — safe to surface
        "anonymous_consent": audit_sub.get("anonymous_consent"),
        "media_purged_at": media_purged_at,
    }
    try:
        audit_dir = _runtime_logs_dir()
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / _AUDIT_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception(
            "anonymous_preview_sweeper: failed to append audit JSONL (non-fatal)"
        )


# ---------------------------------------------------------------------------
# Core sweep logic
# ---------------------------------------------------------------------------


async def sweep_anonymous_previews_once(db: Any) -> dict:
    """Run one sweep pass.  Returns a stats dict.

    Parameters
    ----------
    db:
        An open SQLAlchemy AsyncSession.

    Returns
    -------
    dict with keys:
        blocked_media_purged   — files deleted for block/reject terminal records
        expired_records_purged — expired records deleted (media + row)
        sessions_purged        — anonymous_sessions rows deleted
        usage_rows_purged      — daily_usage rows older than retention purged
    """
    from sqlalchemy import delete, select
    from models import AnonymousPreviewDailyUsage, AnonymousPreviewRecord, AnonymousSession

    stats: dict[str, int] = {
        "blocked_media_purged": 0,
        "expired_records_purged": 0,
        "sessions_purged": 0,
        "usage_rows_purged": 0,
    }
    now = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # (a) block/reject terminal records — delete media, mark audit,
    #     keep DB row for 30d retention.
    # ------------------------------------------------------------------
    try:
        blocked_result = await db.execute(
            select(AnonymousPreviewRecord).where(
                AnonymousPreviewRecord.status.in_(list(_BLOCK_STATUSES))
            )
        )
        blocked_records = blocked_result.scalars().all()
    except Exception:
        logger.exception(
            "anonymous_preview_sweeper: failed to query blocked records (skipping block sweep)"
        )
        blocked_records = []

    for rec in blocked_records:
        try:
            # Only process if not already purged
            audit = rec.audit or {}
            if audit.get("media_purged_at"):
                continue
            _purge_media_files(audit)
            purged_at = now.isoformat()
            merged_audit = {**audit, "media_purged_at": purged_at}
            rec.audit = merged_audit
            stats["blocked_media_purged"] += 1
        except Exception:
            logger.warning(
                "anonymous_preview_sweeper: error handling blocked record %r",
                getattr(rec, "preview_id", "?"), exc_info=True,
            )

    # Commit audit updates for blocked records
    try:
        await db.commit()
    except Exception:
        logger.exception(
            "anonymous_preview_sweeper: failed to commit blocked record audit updates"
        )
        await db.rollback()

    # ------------------------------------------------------------------
    # (b) expired records — delete media, append JSONL, delete row.
    # ------------------------------------------------------------------
    try:
        expired_result = await db.execute(
            select(AnonymousPreviewRecord).where(
                AnonymousPreviewRecord.expires_at <= now
            )
        )
        expired_records = expired_result.scalars().all()
    except Exception:
        logger.exception(
            "anonymous_preview_sweeper: failed to query expired records (skipping expiry sweep)"
        )
        expired_records = []

    for rec in expired_records:
        try:
            _purge_media_files(rec.audit or {})
            purged_at = now.isoformat()
            # Append audit trail BEFORE deleting the row
            _append_audit_jsonl(rec, media_purged_at=purged_at)
            await db.delete(rec)
            stats["expired_records_purged"] += 1
        except Exception:
            logger.warning(
                "anonymous_preview_sweeper: error handling expired record %r",
                getattr(rec, "preview_id", "?"), exc_info=True,
            )

    try:
        await db.commit()
    except Exception:
        logger.exception(
            "anonymous_preview_sweeper: failed to commit expired record deletions"
        )
        await db.rollback()

    # ------------------------------------------------------------------
    # (c) expired anonymous_sessions rows
    # ------------------------------------------------------------------
    try:
        del_sessions = await db.execute(
            delete(AnonymousSession).where(AnonymousSession.expires_at <= now)
        )
        await db.commit()
        stats["sessions_purged"] = del_sessions.rowcount or 0
    except Exception:
        logger.exception(
            "anonymous_preview_sweeper: failed to purge expired sessions"
        )
        try:
            await db.rollback()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # (d) daily_usage rows older than retention window
    # ------------------------------------------------------------------
    try:
        cutoff_date = (now - timedelta(days=USAGE_RETENTION_DAYS)).strftime("%Y-%m-%d")
        del_usage = await db.execute(
            delete(AnonymousPreviewDailyUsage).where(
                AnonymousPreviewDailyUsage.usage_date < cutoff_date
            )
        )
        await db.commit()
        stats["usage_rows_purged"] = del_usage.rowcount or 0
    except Exception:
        logger.exception(
            "anonymous_preview_sweeper: failed to purge stale daily usage rows"
        )
        try:
            await db.rollback()
        except Exception:
            pass

    return stats


# ---------------------------------------------------------------------------
# Loop helpers (mirrors express_reservation_sweeper pattern)
# ---------------------------------------------------------------------------


async def _interruptible_sleep(delay_s: float, stop_event: asyncio.Event | None) -> bool:
    """Sleep delay_s seconds; return True if stop_event fires early."""
    if stop_event is None:
        await asyncio.sleep(delay_s)
        return False
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_s)
        return True
    except asyncio.TimeoutError:
        return False


async def sweeper_loop(*, stop_event: asyncio.Event | None = None) -> None:
    """Periodic sweep loop.  Single-tick failures only log; loop continues.

    Production: ``create_task(sweeper_loop())`` with stop_event=None; shutdown
    via ``task.cancel()``.  Tests: pass stop_event + zero delays.
    """
    from database import async_session

    logger.info(
        "anonymous_preview_sweeper started (interval=%ss, initial_delay=%ss)",
        SWEEP_INTERVAL_S, INITIAL_DELAY_S,
    )
    if await _interruptible_sleep(INITIAL_DELAY_S, stop_event):
        return
    while True:
        try:
            async with async_session() as db:
                stats = await sweep_anonymous_previews_once(db)
            total = sum(stats.values())
            if total > 0:
                logger.info(
                    "anonymous_preview_sweeper: blocked_media_purged=%d "
                    "expired_records_purged=%d sessions_purged=%d "
                    "usage_rows_purged=%d",
                    stats["blocked_media_purged"],
                    stats["expired_records_purged"],
                    stats["sessions_purged"],
                    stats["usage_rows_purged"],
                )
        except Exception:
            logger.exception(
                "anonymous_preview_sweeper tick failed; retrying next interval"
            )
        if await _interruptible_sleep(SWEEP_INTERVAL_S, stop_event):
            return


__all__ = [
    "sweep_anonymous_previews_once",
    "sweeper_loop",
    "SWEEP_INTERVAL_S",
    "INITIAL_DELAY_S",
    "USAGE_RETENTION_DAYS",
]
