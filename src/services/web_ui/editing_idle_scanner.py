"""Scan editing-state jobs that have been idle too long and auto-cancel them.

Decision D24 + §5.4 of the post-edit plan: a job that enters the ``editing``
state but never gets committed or cancelled would otherwise occupy
``editor/editing/`` disk forever, because the TTL cleaner intentionally skips
editing jobs (to avoid racing the user who is still typing).

This scanner runs on the same 6-hour cadence as ``cleanup_expired_projects``
and cancels any ``editing`` job whose ``editing_touched_at`` is older than
``IDLE_THRESHOLD_HOURS``.

Phase 0 / T0-5 wires the scanner into the cleanup loop with a **no-op cancel
callback**, because the actual ``editing/cancel`` endpoint + rollback plumbing
land in Phase 1 T1-1. Phase 0 therefore only detects and logs candidates;
nothing is mutated. When T1-1 lands, the callback will be swapped to a real
implementation and the detection→cancel path activates with zero additional
wiring.

Why a callback (dependency injection) instead of importing the cancel
handler here:

1. Phase 0 must not import anything from the T1-1 modules — they don't
   exist yet, and pretending they do would couple the Phase 0 commit to
   Phase 1 landing order.
2. Tests can inject a fake callback to assert "the right jobs were
   detected" without spinning up DB / filesystem state.
3. Future admin-triggered idle-cancel can reuse the same callback shape.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

IDLE_THRESHOLD_HOURS = 24
IDLE_THRESHOLD = timedelta(hours=IDLE_THRESHOLD_HOURS)

# Called with (job_id, reason) → True if cancel succeeded, False otherwise.
# Phase 0 registers a no-op; Phase 1 T1-1 registers a real cancel.
CancelCallback = Callable[[str, str], bool]

REASON_IDLE_AUTO = "idle_24h_auto_cancel"


def _noop_cancel(job_id: str, reason: str) -> bool:  # pragma: no cover - trivial
    logger.info(
        "editing_idle_scanner: would cancel job_id=%s reason=%s (no callback registered)",
        job_id,
        reason,
    )
    return False


def _parse_iso_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def find_idle_editing_jobs(
    jobs_dir: Path,
    now: datetime,
    *,
    threshold: timedelta = IDLE_THRESHOLD,
) -> list[str]:
    """Return job_ids whose status is 'editing' and whose editing_touched_at
    is older than ``now - threshold``.

    Pure function: no side effects, safe to call from tests. Reads the same
    ``*.json`` files as ``cleanup.py``.
    """
    if not jobs_dir.is_dir():
        return []
    cutoff = now - threshold
    idle_ids: list[str] = []
    for job_file in jobs_dir.glob("*.json"):
        if job_file.name.endswith(".events.jsonl"):
            continue
        try:
            data = json.loads(job_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("status", "")).strip().lower() != "editing":
            continue
        touched_raw = data.get("editing_touched_at")
        if not touched_raw:
            # Missing timestamp → be conservative, do NOT mark idle. Once T1-1
            # lands, enter-edit will always set this; legacy rows will never
            # exist in editing state before that point.
            continue
        touched = _parse_iso_utc(touched_raw)
        if touched is None:
            continue
        if touched < cutoff:
            idle_ids.append(str(data.get("job_id", job_file.stem)))
    return idle_ids


def scan_editing_idle(
    now: datetime,
    cancel_callback: CancelCallback = _noop_cancel,
    *,
    jobs_dir: Path | None = None,
    threshold: timedelta = IDLE_THRESHOLD,
) -> dict[str, list[str]]:
    """Scan for idle editing jobs and invoke ``cancel_callback`` on each.

    Returns a summary dict with:
    - ``candidates``: every job_id that met the idle criterion
    - ``cancelled``: subset for which the callback returned True
    - ``failed``: subset for which the callback returned False or raised

    The callback is responsible for the actual cancel path (deleting
    ``editor/editing/``, resetting status, emitting events). This function
    only handles detection and dispatch.
    """
    resolved_jobs_dir = jobs_dir or Path(
        os.environ.get("AIVIDEOTRANS_JOBS_DIR", "/opt/aivideotrans/app/jobs")
    )
    candidates = find_idle_editing_jobs(resolved_jobs_dir, now, threshold=threshold)
    cancelled: list[str] = []
    failed: list[str] = []
    for job_id in candidates:
        try:
            if cancel_callback(job_id, REASON_IDLE_AUTO):
                cancelled.append(job_id)
            else:
                failed.append(job_id)
        except Exception as exc:
            logger.exception(
                "editing_idle_scanner: callback raised for job_id=%s: %s",
                job_id,
                exc,
            )
            failed.append(job_id)
    if candidates:
        logger.info(
            "editing_idle_scanner: candidates=%d cancelled=%d failed=%d",
            len(candidates),
            len(cancelled),
            len(failed),
        )
    return {"candidates": candidates, "cancelled": cancelled, "failed": failed}


# Cleanup loop calls ``scan_editing_idle(now, registered_cancel_callback)``
# so a runtime swap takes effect on the next scan cycle without restart.
registered_cancel_callback: CancelCallback = _noop_cancel


def inject_editing_cancel_callback(service_provider: object) -> None:
    """T1-10: bind the real editing-cancel handler to the idle scanner.

    Called from app startup (``main.py`` ``job-api`` subcommand) once the
    JobService instance is available. ``service_provider`` must expose
    ``cancel_editing(job_id: str, *, reason: str) -> JobRecord``.

    After this call, ``scan_editing_idle`` invoked by the cleanup loop
    will drop ``editor/editing/`` + flip status to ``succeeded`` on every
    job idle past the threshold, rather than just logging.
    """
    global registered_cancel_callback

    def _real_cancel(job_id: str, reason: str) -> bool:
        try:
            service_provider.cancel_editing(job_id, reason=reason)  # type: ignore[attr-defined]
            return True
        except Exception as exc:
            logger.warning(
                "editing_idle_scanner: cancel_editing(%s) failed: %s",
                job_id, exc,
            )
            return False

    registered_cancel_callback = _real_cancel
    logger.info(
        "editing_idle_scanner: real cancel callback bound "
        "(replacing _noop_cancel)"
    )


def reset_editing_cancel_callback() -> None:
    """Test helper: restore the no-op default. Also used by shutdown paths
    that want to stop cancelling after the service is torn down."""
    global registered_cancel_callback
    registered_cancel_callback = _noop_cancel
