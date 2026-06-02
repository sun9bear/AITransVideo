"""Threaded batch re-TTS with status-file progress reporting (D39).

Same pattern as ``video_render_async.py``: POST spawns a daemon thread
and returns a ``task_id`` immediately; the thread iterates
``regenerate_segment_tts`` over every dirty segment and writes progress
to ``{project_dir}/editor/editing/regen_status.json`` after each step.
GET reads that file.

Why threads, not asyncio: Job API is ``stdlib http.server`` (no event
loop). Why a status file, not ``job_events``: per-segment progress
updates would inflate the events file by hundreds of rows per batch;
we want a small, read-only progress snapshot that the frontend can
poll at ~1Hz. Terminal state (completed / failed with D38 summary) is
what the API consumer needs — no need to reconstruct intermediate
states from events.

Plan references: §7.4 / D38 / D39. The sync
``services.jobs.editing_batch.regenerate_all_dirty_segments`` is
preserved as the building block — this module only wraps it with
progress tracking and thread dispatch.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.jobs.editing_batch import BATCH_REGENERATE_TRIGGER_STATUSES
from services.jobs.editing_segments import load_segment_status
from services.jobs.editing_tts import SegmentTTSCaller, regenerate_segment_tts

logger = logging.getLogger(__name__)

__all__ = [
    "read_regen_all_status",
    "request_regen_all_cancel",
    "start_regen_all_async",
    "status_file_path",
]


# ---------------------------------------------------------------------------
# Single-flight: per-project in-process lock.
#
# Why: the POST endpoint used to unconditionally spawn a new thread on every
# call. Double-click / proxy retry / two-tab scenarios would spin up N
# parallel batches iterating the SAME dirty segments → N × TTS provider
# calls per segment (paid API double-billing, violates CLAUDE.md's "付费 API
# 不能自动调用" constraint — even user-triggered, duplicate spends are
# silent money-burn).
#
# Fix: a module-level ``{project_key: task_id}`` dict guarded by a lock.
# Second call while a batch is in flight returns the existing task_id —
# the frontend already polls /status and will see the in-flight progress.
# The slot is released BEFORE writing the terminal status snapshot so that
# when a user sees ``stage=completed`` and clicks regenerate again, the
# new POST is immediately eligible for a fresh task.
# ---------------------------------------------------------------------------

_active_tasks_lock = threading.Lock()
_active_tasks: dict[str, str] = {}


def _project_key(project_dir: Path) -> str:
    """Stable string identity for the single-flight dict."""
    try:
        return str(project_dir.resolve())
    except OSError:
        return str(project_dir)


def _new_task_id() -> str:
    return uuid.uuid4().hex[:12]


def status_file_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / "editor" / "editing" / "regen_status.json"


def read_regen_all_status(
    project_dir: str | Path, task_id: str,
) -> dict[str, Any] | None:
    """Return the progress snapshot if status file exists and matches.

    - ``None`` if the file doesn't exist (no batch has ever started, or
      the editing dir was cleaned up).
    - ``{"mismatch": True, "actual_task_id": <id>}`` when the file
      belongs to a newer batch — caller should reset state on its side.
    - Otherwise the full status dict (same shape as ``_write_status``).
    """
    path = status_file_path(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("task_id") != task_id:
        return {"mismatch": True, "actual_task_id": data.get("task_id")}
    return data


def _read_status_raw(
    project_dir: str | Path, task_id: str,
) -> dict[str, Any] | None:
    """Internal counterpart to :func:`read_regen_all_status` that does
    NOT return the ``{"mismatch": ...}`` sentinel — used inside the
    worker loop where a mismatch shouldn't be possible (single-flight
    slot is held) and we just want to check ``cancel_requested``.
    Returns None if the file is missing / unreadable."""
    path = status_file_path(project_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("task_id") != task_id:
        return None
    return data


def _write_status(project_dir: Path, payload: dict[str, Any]) -> None:
    """Atomic write via tmpfile + rename. Silently drops if the editing
    dir has been removed (user cancelled / committed — thread racing
    with _rm_editing_dir).

    Must NOT recreate the editing/ directory. Legit invariant: the
    directory exists iff the job is in ``editing`` status. An
    unconditional ``mkdir(parents=True, exist_ok=True)`` here would
    resurrect the dir as a zombie (just the status file, no segments /
    drafts / voice_map) and leave it on disk after commit/cancel —
    violating the docstring and confusing cleanup scanners (Claude
    Code ultrareview #1)."""
    path = status_file_path(project_dir)
    if not path.parent.is_dir():
        # editing/ removed by cancel / commit — drop this write silently.
        return
    # Small retry loop — Windows `os.replace` can briefly raise
    # ``PermissionError`` (WinError 5 / 32) when another thread or
    # process has the target open for read (the GET /status poller or
    # another thread's _write_status racing with ours). POSIX rename
    # is atomic so Linux production never hits this; the retries are
    # tiny Windows-only friction, not a reliability hack.
    import time as _time
    tmp = path.with_suffix(".json.tmp")
    last_exc: OSError | None = None
    for attempt in range(3):
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
            return
        except OSError as exc:
            last_exc = exc
            _time.sleep(0.02 * (attempt + 1))
    logger.warning("Failed to write regen status after retries: %s", last_exc)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _initial_status(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "stage": "starting",
        "total": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "succeeded_segment_ids": [],
        "failed_segment_ids": [],
        "failures": [],
        "current_segment_id": None,
        "result": None,
        "error": None,
        # 2026-04-21 plan §7.10 / D39: user may hit "取消批量合成" mid-run.
        # The thread checks this flag between segments and exits into
        # stage='cancelled' preserving already-done work. Default False —
        # nobody has asked to cancel yet.
        "cancel_requested": False,
        "updated_at": _utc_now_iso(),
    }


def request_regen_all_cancel(
    project_dir: str | Path, task_id: str,
) -> bool:
    """Mark a running batch as cancel-requested.

    Writes ``cancel_requested=true`` into the live status file atomically.
    The running thread sees it on its next per-segment boundary and
    transitions to ``stage='cancelled'`` with already-done counts intact.

    Returns True if the flag was written, False if:
      - status file doesn't exist (no batch is running for this project),
      - the file's task_id doesn't match (stale request for a previous
        batch — silently ignored; the live batch keeps going).

    Idempotent: calling twice is a no-op on the second pass.
    """
    path = status_file_path(project_dir)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("task_id") != task_id:
        # Someone else's batch or a stale id — leave alone.
        return False
    if data.get("cancel_requested") is True:
        return True  # idempotent
    data["cancel_requested"] = True
    data["updated_at"] = _utc_now_iso()
    _write_status(Path(project_dir), data)
    return True


def start_regen_all_async(
    *,
    project_dir: str | Path,
    tts_caller: SegmentTTSCaller | None = None,
    default_tts_model: str | None = None,
    segment_ids: list[str] | None = None,
) -> str:
    """Spawn a daemon thread that batches re-TTS over dirty segments.

    Returns the ``task_id`` that the caller should pass back to
    ``read_regen_all_status``. The status file is seeded synchronously
    before the thread starts so that an immediate GET sees
    ``stage="starting"`` rather than ``None``.

    Single-flight: if a batch is already in flight for this project, the
    existing ``task_id`` is returned (no new thread, no new TTS calls).
    The slot is released in ``_run_batch`` when the batch reaches its
    terminal state.
    """
    project_path = Path(project_dir)
    project_key = _project_key(project_path)

    with _active_tasks_lock:
        existing = _active_tasks.get(project_key)
        if existing is not None:
            return existing
        task_id = _new_task_id()
        _active_tasks[project_key] = task_id

    # Past this point we own the slot — any failure to spawn the worker
    # must release it, otherwise the project is permanently blocked.
    try:
        _write_status(project_path, _initial_status(task_id))
        thread = threading.Thread(
            target=_run_batch,
            name=f"regen-all-{task_id}",
            kwargs={
                "task_id": task_id,
                "project_dir": project_path,
                "tts_caller": tts_caller,
                "default_tts_model": default_tts_model,
                "project_key": project_key,
                "segment_ids": segment_ids,
            },
            daemon=True,
        )
        thread.start()
    except Exception:
        with _active_tasks_lock:
            if _active_tasks.get(project_key) == task_id:
                del _active_tasks[project_key]
        raise
    return task_id


def _release_active_slot(project_key: str | None, task_id: str) -> None:
    """Idempotent: remove this batch's slot only if it still owns it."""
    if project_key is None:
        return
    with _active_tasks_lock:
        if _active_tasks.get(project_key) == task_id:
            del _active_tasks[project_key]


def _run_batch(
    *,
    task_id: str,
    project_dir: Path,
    tts_caller: SegmentTTSCaller | None,
    default_tts_model: str | None,
    project_key: str | None = None,
    segment_ids: list[str] | None = None,
) -> None:
    """Thread body. Mirrors ``regenerate_all_dirty_segments`` semantics
    but writes per-segment progress to the status file and distinguishes
    "per-segment failure (continue)" from "top-level crash (stage=failed)".

    Releases the single-flight slot before writing each terminal status
    snapshot so the "user sees completed → re-POST" path is never gated
    on thread-teardown timing. The ``finally`` is a safety net for
    unforeseen exceptions bubbling past the inner handlers.
    """
    try:
        try:
            status_map = load_segment_status(project_dir)
        except Exception as exc:  # missing segment_status / unreadable JSON
            logger.exception("regen batch %s: failed to load segment_status", task_id)
            _release_active_slot(project_key, task_id)
            _write_status(
                project_dir,
                {
                    **_initial_status(task_id),
                    "stage": "failed",
                    "error": f"failed to load segment_status: {exc}"[:500],
                    "updated_at": _utc_now_iso(),
                },
            )
            return

        if segment_ids is None:
            eligible = sorted(
                sid for sid, status in status_map.items()
                if status in BATCH_REGENERATE_TRIGGER_STATUSES
            )
        else:
            seen: set[str] = set()
            eligible = []
            for raw_sid in segment_ids:
                sid = str(raw_sid).strip()
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                if status_map.get(sid) in BATCH_REGENERATE_TRIGGER_STATUSES:
                    eligible.append(sid)
        total = len(eligible)
        succeeded: list[str] = []
        failed_ids: list[str] = []
        failures: list[dict[str, str]] = []

        # Initial running snapshot with known total.
        _write_status(
            project_dir,
            {
                **_initial_status(task_id),
                "stage": "running",
                "total": total,
                "updated_at": _utc_now_iso(),
            },
        )

        cancelled = False
        for segment_id in eligible:
            # D39 cancel — check the status file each iteration. Cheap (few
            # KB JSON read) compared to a 2-5s TTS call, so the polling
            # overhead is <1% even on small batches. Stopping BEFORE the
            # TTS call guarantees we don't burn paid provider quota on a
            # batch the user has already abandoned.
            current = _read_status_raw(project_dir, task_id)
            if current is not None and current.get("cancel_requested") is True:
                cancelled = True
                break

            # "Current" snapshot so the UI can show "正在重合成 seg_042"
            _write_status(
                project_dir,
                {
                    "task_id": task_id,
                    "stage": "running",
                    "total": total,
                    "succeeded_count": len(succeeded),
                    "failed_count": len(failures),
                    "succeeded_segment_ids": list(succeeded),
                    "failed_segment_ids": list(failed_ids),
                    "failures": list(failures),
                    "current_segment_id": segment_id,
                    "result": None,
                    "error": None,
                    "cancel_requested": False,
                    "updated_at": _utc_now_iso(),
                },
            )
            try:
                regenerate_segment_tts(
                    project_dir,
                    segment_id,
                    tts_caller=tts_caller,
                    default_tts_model=default_tts_model,
                )
                succeeded.append(segment_id)
            except Exception as exc:
                # Per-segment failure: log, record, continue (plan D38).
                logger.info(
                    "regen batch %s: segment_id=%s failed: %s",
                    task_id, segment_id, exc,
                )
                failed_ids.append(segment_id)
                failures.append(
                    {"segment_id": segment_id, "error": str(exc)[:300]}
                )

        if cancelled:
            logger.info(
                "regen batch %s cancelled after %d succeeded / %d failed",
                task_id, len(succeeded), len(failures),
            )
            summary = {
                "total": total,
                "succeeded_count": len(succeeded),
                "failed_count": len(failures),
                "succeeded_segment_ids": succeeded,
                "failed_segment_ids": failed_ids,
                "failures": failures,
                "cancelled": True,
            }
            _release_active_slot(project_key, task_id)
            _write_status(
                project_dir,
                {
                    "task_id": task_id,
                    "stage": "cancelled",
                    "total": total,
                    "succeeded_count": len(succeeded),
                    "failed_count": len(failures),
                    "succeeded_segment_ids": succeeded,
                    "failed_segment_ids": failed_ids,
                    "failures": failures,
                    "current_segment_id": None,
                    "result": summary,
                    "error": None,
                    "cancel_requested": True,
                    "updated_at": _utc_now_iso(),
                },
            )
            return

        # Completion snapshot with D38 summary.
        summary = {
            "total": total,
            "succeeded_count": len(succeeded),
            "failed_count": len(failures),
            "succeeded_segment_ids": succeeded,
            "failed_segment_ids": failed_ids,
            "failures": failures,
        }
        # Release BEFORE writing "completed" so that once the frontend sees
        # terminal state, the next POST is already unblocked.
        _release_active_slot(project_key, task_id)
        _write_status(
            project_dir,
            {
                "task_id": task_id,
                "stage": "completed",
                "total": total,
                "succeeded_count": len(succeeded),
                "failed_count": len(failures),
                "succeeded_segment_ids": succeeded,
                "failed_segment_ids": failed_ids,
                "failures": failures,
                "current_segment_id": None,
                "result": summary,
                "error": None,
                "updated_at": _utc_now_iso(),
            },
        )
    finally:
        # Safety net — idempotent via task_id identity check.
        _release_active_slot(project_key, task_id)
