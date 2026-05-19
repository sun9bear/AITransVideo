"""Shared pan.* JSONL event emitter (Phase 9 §T9.4 + CodeX 2026-05-19 P1b).

Wraps ``storage.event_log.emit_download_event`` with:
  - ``stage='pan'`` so scripts/r2_observability.py groups correctly.
  - A defensive try/except so event-write failure NEVER aborts the
    backup/restore/cleanup workflow. The underlying writer already
    catches exceptions, this is double-coverage for the
    ``import`` itself (in case the gateway image somehow lacks the
    storage subpackage on a partial deploy).

Used by:
  - gateway/pan/backup_executor.py — backup.started / succeeded / failed
  - gateway/pan/restore_executor.py — restore.started / succeeded / failed
  - gateway/pan/residue_cleanup.py — residue_cleanup.completed
  - gateway/pan/auth.py — token_revoked (alongside dispatch_event for
    notifications)

Lives under ``gateway/pan/`` rather than ``gateway/storage/`` because
the pan-specific stage label + best-effort guarantee are pan-flavored
conventions — keeping the underlying ``emit_download_event`` writer
unopinionated.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


def emit_pan_event_safe(
    *,
    job_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any],
    level: str = "info",
) -> None:
    """Append one pan.* event line to the job's JSONL events file.

    Best-effort: a write failure logs WARNING and returns. Callers must
    not depend on the write succeeding — the event is observability,
    not state.

    Parameters
    ----------
    job_id : str
        The pan operation's Job.job_id (NOT BackupRecord.id).
    event_type : str
        One of the 8 ``pan.*`` types in
        ``services.jobs.events.SUPPORTED_EVENT_TYPES``.
    message : str
        Human-readable line. Used by log viewers.
    payload : dict
        Arbitrary extra fields — typically ``user_id`` (str), plus
        operation-specific keys (``backup_id``, ``provider``,
        ``size_bytes``, ``reason``, ...).
    level : str
        ``info`` / ``warn`` / ``error`` / ``critical``. ``error`` for
        ``*.failed`` events, ``info`` for ``*.started`` /
        ``*.succeeded`` / ``*.completed``, ``warn`` for
        ``token_revoked``.
    """
    try:
        # Lazy import so unit tests that stub `storage` / `config`
        # don't have to materialize at import time.
        from storage.event_log import emit_download_event  # noqa: PLC0415

        emit_download_event(
            job_id=job_id,
            event_type=event_type,
            message=message,
            payload=payload,
            stage="pan",
            level=level,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pan event emit failed (best-effort) job=%s type=%s err=%s",
            job_id, event_type, exc,
        )


async def dispatch_pan_failure_notification(
    engine: AsyncEngine,
    *,
    event_type: str,
    user_id: _uuid.UUID,
    job_id: str,
    reason: str,
) -> None:
    """Insert a user_notifications row for a pan backup/restore failure.

    CodeX 2026-05-19 P1d: the pan.backup.failed / pan.restore.failed
    DISPATCH_MAP recipes (added in Phase 9 T9.3) were never reached
    from production — executors only emitted JSONL. This helper closes
    that loop:

      1. Opens a fresh ``AsyncSession`` from the engine because pan
         executors hold an ``AsyncConnection`` (single-conn long-hold
         pattern for advisory locks), not a session.
      2. Resolves the recipe's ``{display_name}`` token (CodeX 2026-05-19
         P2a: ``Job.display_name`` is the user-visible field — it's the
         Studio edit label, shown in workspace UI. ``Job.title`` is the
         scraped source video title and may be in a different language
         or empty. Order: display_name → title → job_id.
      3. Calls ``notifications_service.dispatch_event`` with the
         appropriate payload. ``reason`` is already truncated to ≤200
         chars by the caller.
      4. Commits the notification insert.

    Best-effort: any exception is logged at WARNING and swallowed.
    Pan executor flow must NEVER abort because notification dispatch
    failed.

    The reason this isn't a sync wrapper around the JSONL helper:
    notifications need a real PG session + recipe lookup + sanitized
    payload format, all of which require the existing
    notifications_service machinery.
    """
    from sqlalchemy.ext.asyncio import (  # noqa: PLC0415
        AsyncSession, async_sessionmaker,
    )
    from sqlalchemy import select  # noqa: PLC0415

    try:
        from models import Job  # noqa: PLC0415
        from notifications_service import dispatch_event  # noqa: PLC0415

        Session = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False,
        )
        async with Session() as db:
            row = (await db.execute(
                select(Job.display_name, Job.title)
                .where(Job.job_id == job_id)
            )).one_or_none()
            # Prefer Studio's user-editable display_name; fall back to
            # the source video title (Job.title) if user hasn't set one;
            # final fallback to the opaque job_id so the notification is
            # never empty / mangled.
            display_name = job_id
            if row is not None:
                if row.display_name:
                    display_name = row.display_name
                elif row.title:
                    display_name = row.title
            await dispatch_event(
                db,
                event_type=event_type,
                user_id=user_id,
                job_id=job_id,
                payload={
                    'display_name': display_name,
                    'reason': reason,
                },
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pan failure notification dispatch failed (best-effort) "
            "job=%s type=%s err=%s",
            job_id, event_type, exc,
        )
