"""Editing state transitions for Studio post-edit workflow (T1-1 skeleton).

Plan ref: ``docs/plans/2026-04-18-studio-post-edit-plan.md`` §4 / §7.8 / §5.4.1

Responsibilities:

- ``enter_editing(record, store)`` — ``succeeded → editing``; create
  ``editor/editing/`` dir; snapshot ``segments.json`` to the editing baseline;
  write ``editing_touched_at``; emit ``editing.entered`` event.
- ``cancel_editing(record, store)`` — ``editing → succeeded``; drop the
  ``editor/editing/`` dir; clear ``editing_touched_at``; emit
  ``editing.cancelled`` event with the cancel reason.
- ``commit_editing(record, store, strategy)`` — **T1-1 contract skeleton**.
  Validates state + strategy and raises ``NotImplementedError``. The real
  commit (overwrite vs copy_as_new + alignment→publish rerun) lives in T1-9.
- ``touch_editing(record, store)`` — refresh ``editing_touched_at``; no-op
  if the job is no longer in editing. Called by every editing mutation
  endpoint once they land (T1-2 onwards); §5.4.1 lists the refresh points.

Design notes:

* We operate on ``JobRecord`` dataclass copies via ``dataclasses.replace``
  — never mutate in place — then persist via ``store.save_job``.
* Event emission uses the generic ``JobEvent`` plumbing (same as pipeline
  status events) so the admin "关键进展" view sees editing transitions
  out of the box. Full ``editing.mutation`` enumeration (§D40) arrives in
  later tasks; T1-1 only emits ``entered`` / ``cancelled`` / (placeholder)
  ``commit_failed``.
* Filesystem side effects happen BEFORE the status write, so that if
  ``mkdir`` / ``rmtree`` fails we surface the error without leaving the
  DB record in an inconsistent state (the ``editing_touched_at`` flip
  would otherwise suggest an editing session that has no backing dir).
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from services.jobs.events import EVENT_LEVEL_INFO, EVENT_TYPE_STATUS, JobEvent
from services.jobs.models import (
    JOB_STATUS_EDITING,
    JOB_STATUS_SUCCEEDED,
    JobRecord,
)
from services.jobs.service import JobConflictError
from services.jobs.store import JobStore

logger = logging.getLogger(__name__)

__all__ = [
    "EditingConflictError",
    "EDITING_SUBDIR",
    "SUPPORTED_COMMIT_STRATEGIES",
    "cancel_editing",
    "commit_editing",
    "enter_editing",
    "touch_editing",
]

# Path inside project_dir for the editing buffer. See §3.5.
EDITING_SUBDIR = "editor/editing"

# Strategy values accepted by editing/commit. overwrite replaces the
# source task's artifacts in place; copy_as_new creates a sibling job.
# Both are validated here even though the pipeline lives in T1-9, so
# the contract is stable from day one.
SUPPORTED_COMMIT_STRATEGIES: frozenset[str] = frozenset({"overwrite", "copy_as_new"})


class EditingConflictError(JobConflictError):
    """Raised when the job's current state does not permit the requested
    editing transition (e.g. enter-edit on a ``running`` job, or
    editing/cancel on a ``succeeded`` job).

    Subclass of ``JobConflictError`` so the existing ``except JobConflictError``
    → HTTP 409 path in ``api.py`` picks it up without a separate branch.
    """


# ---------------------------------------------------------------------------
# Public transitions
# ---------------------------------------------------------------------------


def enter_editing(record: JobRecord, store: JobStore) -> JobRecord:
    """Transition ``succeeded → editing`` for a Studio job.

    Requires:
    - ``record.status == 'succeeded'``
    - ``record.service_mode == 'studio'`` (Express does not support editing)
    - ``record.project_dir`` set and points to an existing directory

    Side effects:
    - Creates ``{project_dir}/editor/editing/`` (+ ``tts_segments_draft/``
      subdir)
    - Snapshots ``editor/segments.json`` into ``editor/editing/segments.json``
      as the editable baseline (§3.5)
    - Saves updated JobRecord (status + editing_touched_at + updated_at)
    - Appends an ``editing.entered`` status event
    """
    if record.status == JOB_STATUS_EDITING:
        raise EditingConflictError(
            f"job {record.job_id} is already in editing state"
        )
    if record.status != JOB_STATUS_SUCCEEDED:
        raise EditingConflictError(
            f"job {record.job_id} can only enter editing from succeeded "
            f"(current status: {record.status})"
        )
    if record.service_mode != "studio":
        raise EditingConflictError(
            f"job {record.job_id} is not a Studio job "
            f"(service_mode={record.service_mode}); only Studio supports editing"
        )
    if not record.project_dir:
        raise EditingConflictError(
            f"job {record.job_id} has no project_dir; cannot create editing buffer"
        )
    project_dir = Path(record.project_dir)
    if not project_dir.is_dir():
        raise EditingConflictError(
            f"job {record.job_id} project_dir does not exist: {project_dir}"
        )

    editing_dir = project_dir / EDITING_SUBDIR
    editing_dir.mkdir(parents=True, exist_ok=True)
    (editing_dir / "tts_segments_draft").mkdir(parents=True, exist_ok=True)

    baseline_segments = project_dir / "editor" / "segments.json"
    editing_segments = editing_dir / "segments.json"
    if baseline_segments.is_file() and not editing_segments.exists():
        shutil.copy2(baseline_segments, editing_segments)

    now = _utc_now_iso()
    updated = replace(
        record,
        status=JOB_STATUS_EDITING,
        editing_touched_at=now,
        updated_at=now,
    )
    store.save_job(updated)
    _emit_event(
        store,
        updated,
        message="editing.entered: user resumed post-edit session",
    )
    return updated


def cancel_editing(
    record: JobRecord,
    store: JobStore,
    *,
    reason: str = "user_cancel",
) -> JobRecord:
    """Transition ``editing → succeeded``; drop the editing buffer.

    ``reason`` is recorded on the event so admins can distinguish
    ``user_cancel`` / ``idle_24h_auto_cancel`` / ``admin_force`` later.
    """
    if record.status != JOB_STATUS_EDITING:
        raise EditingConflictError(
            f"job {record.job_id} is not in editing state "
            f"(current status: {record.status})"
        )

    if record.project_dir:
        editing_dir = Path(record.project_dir) / EDITING_SUBDIR
        if editing_dir.exists():
            shutil.rmtree(editing_dir, ignore_errors=True)

    now = _utc_now_iso()
    updated = replace(
        record,
        status=JOB_STATUS_SUCCEEDED,
        editing_touched_at=None,
        updated_at=now,
    )
    store.save_job(updated)
    _emit_event(
        store,
        updated,
        message=f"editing.cancelled: reason={reason}",
    )
    return updated


def commit_editing(
    record: JobRecord,
    store: JobStore,
    *,
    strategy: str,
    copy_display_name: str | None = None,
) -> JobRecord:
    """T1-1 contract skeleton — does NOT run the commit pipeline.

    Validates the request (state + strategy) and raises
    ``NotImplementedError``. The real overwrite / copy_as_new flow
    (alignment→publish rerun, Phase A/B two-phase for copy_as_new, etc.)
    is implemented in T1-9.

    Accepting the strategy parameter here keeps the HTTP contract stable
    so the frontend can code against it before T1-9 lands; T1-9 swaps
    the ``raise`` for the actual transition without changing callers.
    """
    if record.status != JOB_STATUS_EDITING:
        raise EditingConflictError(
            f"job {record.job_id} is not in editing state "
            f"(current status: {record.status})"
        )
    if strategy not in SUPPORTED_COMMIT_STRATEGIES:
        raise EditingConflictError(
            f"unsupported commit strategy: {strategy!r}; "
            f"must be one of {sorted(SUPPORTED_COMMIT_STRATEGIES)}"
        )
    # Display-name validation is intentionally lenient here — full
    # validation (length / conflict-suffix / character ban) lives in the
    # gateway rename path; commit only requires a non-empty value when
    # strategy == "copy_as_new".
    if strategy == "copy_as_new" and (
        copy_display_name is None or not str(copy_display_name).strip()
    ):
        raise EditingConflictError(
            "copy_as_new strategy requires a non-empty copy_display_name"
        )

    raise NotImplementedError(
        "editing/commit pipeline is T1-9 scope; T1-1 ships the contract "
        "skeleton only. Strategy + display_name were validated."
    )


def touch_editing(record: JobRecord, store: JobStore) -> JobRecord:
    """Refresh ``editing_touched_at`` for the job.

    No-op if the job is not in editing state (avoids spurious updates
    when a mutation endpoint is called on the wrong state; the endpoint
    itself should 409 earlier). Safe to call from every editing-state
    mutation handler per §5.4.1.
    """
    if record.status != JOB_STATUS_EDITING:
        return record
    now = _utc_now_iso()
    updated = replace(record, editing_touched_at=now, updated_at=now)
    store.save_job(updated)
    return updated


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_event(store: JobStore, record: JobRecord, *, message: str) -> None:
    """Append a status event to the job's event log. Non-fatal on failure
    (we don't want event-log I/O to break the primary state transition)."""
    try:
        event = JobEvent(
            job_id=record.job_id,
            event_type=EVENT_TYPE_STATUS,
            created_at=_utc_now_iso(),
            status=record.status,
            message=message,
            level=EVENT_LEVEL_INFO,
        )
        store.append_event(record.job_id, event)
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "editing: failed to append event for job_id=%s; state transition "
            "already persisted",
            record.job_id,
        )
