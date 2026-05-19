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
from typing import Any

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
