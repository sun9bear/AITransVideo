"""editing/commit full flow (T1-9).

Plan ref: §7.8 (overwrite + copy_as_new two-phase), D26 (no TTS re-entry),
D28 (runner entry point), D34 (A-rollback preserves source draft),
§3.5 (baseline untouched until commit).

Two strategies, one entry point ``commit_editing_pipeline``:

* ``overwrite`` — apply editing/ edits to the source project_dir:
    1. Write ``editing/segments.json`` (merged with voice_map) → baseline
       ``editor/segments.json``.
    2. For every wav under ``editing/tts_segments_draft/``: safely replace
       the baseline wav at ``editor/tts_segments/{sid}.wav`` via
       ``apply_draft_segment`` (never ``open('wb')`` — inode-safe).
    3. Delete ``editor/editing/``.
    4. Flip status to ``running`` + ``current_stage='alignment'`` +
       ``edit_generation += 1`` + clear ``editing_touched_at``.
    5. Submit runner with ``continue_existing=True`` / ``start_stage='alignment'``.

* ``copy_as_new`` (two-phase per D34):
    Phase A (PREPARE — source must stay 100% intact until Phase A
    completes; any exception rolls back the target):
      A1. Generate new job_id + project_dir.
      A2. ``copy_service.prepare_copy_project_dir`` — hardlinks + draft apply.
      A3. Create new JobRecord (status=``queued``, new display_name,
          copy_of_job_id / root_job_id set, edit_generation=0).
      A4. Submit runner on new record; failure → rollback A3 + A2,
          source untouched.

    Phase B (COMMIT SOURCE — runs ONLY after runner.start returned):
      B1. Reset source ``status=succeeded`` + clear editing_touched_at.
      B2. Delete source ``editor/editing/``.

    Phase B failures are logged but NOT rolled back — the new job is
    already running and rolling source back would create a messier state.
    Admins intervene via ``editing_idle_scanner`` force-cancel if needed.

This module does NOT touch Gateway's PostgreSQL ``jobs`` table. That
dual-write is the Gateway intercept layer's job (plan §13.1). The Job API
layer just returns the new_job_id in the commit response; gateway reads
it and INSERTs the PG row in the same request.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid as _uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from services.jobs.copy_service import (
    CopyPreparationError,
    apply_draft_segment,
    prepare_copy_project_dir,
    prune_project_state_payload,
    rollback_prepared_target,
)
from utils.atomic_io import atomic_write_json
from services.jobs.editing import EDITING_SUBDIR, EditingConflictError
from services.jobs.events import EVENT_LEVEL_INFO, EVENT_TYPE_STATUS, JobEvent
from services.jobs.input_validators import validate_commit_strategy
from services.jobs.models import (
    JOB_STATUS_EDITING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    STAGE_ALIGNMENT,
    JobRecord,
)
from services.jobs.runner_extensions import submit_job_from_existing_project_dir
from services.jobs.store import JobStore

logger = logging.getLogger(__name__)

__all__ = [
    "CommitPipelineError",
    "CommitRunner",
    "commit_editing_pipeline",
]


class CommitPipelineError(Exception):
    """Raised when commit_editing_pipeline cannot complete. Caller should
    surface the message to the user so they know their editing state is
    (depending on phase) either fully preserved for retry or half-applied."""


class CommitRunner(Protocol):
    """The subset of the JobRunner interface that commit actually needs.

    A real ``ProcessJobRunner`` satisfies this out of the box; tests can
    pass a minimal fake.
    """

    def start(self, record: JobRecord, continue_existing: bool = False) -> None: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_event(store: JobStore, record: JobRecord, *, message: str) -> None:
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
        logger.exception("commit: failed to append event for %s", record.job_id)


def _apply_voice_map(
    segments: list[dict[str, Any]],
    voice_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay voice_map overrides onto the segment list.

    Writes to the canonical ``tts_provider`` field — the same key that
    ``DubbingSegment.tts_provider`` / the single-segment regen overlay
    (in services.jobs.editing_tts) / the TTS router / the γ publish
    loader all read. Writing the drifted ``provider`` key would
    silently strand the user's provider pick (DubbingSegment
    constructor filters unknown keys → tts_provider="" → downstream
    falls back to the global default).

    ``segment_id`` in editing/segments.json may be int (legacy lazy-seed
    snapshots). voice_map keys are always str (load_voice_map coerces
    via ``str(sid)``). Normalise the lookup key via ``str()`` instead of
    gating on ``isinstance(sid, str)`` — the old gate silently dropped
    every int-typed segment's override.
    """
    if not voice_map:
        return segments
    out: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            out.append(seg)
            continue
        sid = seg.get("segment_id")
        override = voice_map.get(str(sid)) if sid is not None else None
        if override:
            new_seg = dict(seg)
            new_seg["tts_provider"] = override["provider"]
            new_seg["voice_id"] = override["voice_id"]
            # Scrub any legacy ``provider`` key so editor/segments.json
            # stays single-source-of-truth on tts_provider.
            new_seg.pop("provider", None)
            out.append(new_seg)
        else:
            out.append(seg)
    return out


def _apply_editing_to_baseline(project_dir: Path) -> dict[str, Any]:
    """Promote editing/ edits into baseline. Used by overwrite strategy.

    Does not remove the editing/ dir — caller does that once satisfied.
    Raises ``CopyPreparationError`` if editing/ is malformed.
    """
    editing = project_dir / EDITING_SUBDIR
    if not editing.is_dir():
        raise CopyPreparationError(
            f"editing dir does not exist: {editing}"
        )
    editor = project_dir / "editor"

    # 1) merge editing/segments.json + voice_map.json → editor/segments.json
    editing_segments_file = editing / "segments.json"
    if editing_segments_file.is_file():
        segments = json.loads(editing_segments_file.read_text(encoding="utf-8"))
        if not isinstance(segments, list):
            raise CopyPreparationError(
                f"editing/segments.json is not a list"
            )
    else:
        # No text edits made — keep baseline segments.json in place
        baseline_file = editor / "segments.json"
        segments = (
            json.loads(baseline_file.read_text(encoding="utf-8"))
            if baseline_file.is_file() else []
        )

    voice_map_file = editing / "voice_map.json"
    voice_map: dict[str, dict[str, Any]] = {}
    if voice_map_file.is_file():
        raw = json.loads(voice_map_file.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for sid, entry in raw.items():
                if isinstance(entry, dict):
                    p = str(entry.get("provider", "")).strip()
                    v = str(entry.get("voice_id", "")).strip()
                    if p and v:
                        voice_map[str(sid)] = {"provider": p, "voice_id": v}
    if voice_map:
        segments = _apply_voice_map(segments, voice_map)

    editor.mkdir(parents=True, exist_ok=True)
    target_file = editor / "segments.json"
    target_file.write_text(
        json.dumps(segments, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 2) draft wavs → baseline via apply_draft_segment
    drafts_dir = editing / "tts_segments_draft"
    applied: list[str] = []
    if drafts_dir.is_dir():
        baseline_tts = editor / "tts_segments"
        baseline_tts.mkdir(parents=True, exist_ok=True)
        for draft_wav in sorted(drafts_dir.glob("*.wav")):
            target_wav = baseline_tts / draft_wav.name
            apply_draft_segment(draft_wav, target_wav)
            applied.append(draft_wav.stem)

    return {
        "applied_draft_segment_ids": applied,
        "segments_count": len(segments),
        "voice_overrides_count": len(voice_map),
    }


def _rm_editing_dir(project_dir: Path) -> None:
    editing = project_dir / EDITING_SUBDIR
    if editing.exists():
        shutil.rmtree(editing, ignore_errors=True)


def _prune_overwrite_project_state(project_dir: Path) -> None:
    """Reset alignment + publish to PENDING in the overwrite target's
    project_state.json. No-op if the file is absent."""
    state_path = project_dir / "project_state.json"
    if not state_path.is_file():
        return
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    pruned = prune_project_state_payload(payload, new_project_id=project_dir.name)
    atomic_write_json(str(state_path), pruned)


def _compute_copy_project_dir(source_project_dir: Path, new_job_id: str) -> Path:
    """Place the copy sibling under the same parent directory, named by
    new_job_id. This keeps storage layout predictable and lets admins find
    all copies of a source easily (all jobs under the same projects/ root)."""
    return source_project_dir.parent / new_job_id


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def commit_editing_pipeline(
    record: JobRecord,
    store: JobStore,
    runner: CommitRunner,
    *,
    strategy: str,
    copy_display_name: str | None = None,
    new_job_id_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Dispatch to overwrite or copy_as_new. Returns a response payload
    suitable for the HTTP handler to serialize as JSON.

    Response shape:

        overwrite → {
            "strategy": "overwrite",
            "job_id": <same job_id>,
            "edit_generation": N+1,
            "applied_draft_segment_ids": [...],
            "segments_count": K,
        }

        copy_as_new → {
            "strategy": "copy_as_new",
            "source_job_id": <old>,
            "new_job_id": <new>,
            "new_project_dir": <path>,
            "new_display_name": <str>,
        }
    """
    if record.status != JOB_STATUS_EDITING:
        raise EditingConflictError(
            f"job {record.job_id} is not in editing state "
            f"(current status: {record.status})"
        )
    validate_commit_strategy(strategy)
    if not record.project_dir:
        raise EditingConflictError(f"job {record.job_id} has no project_dir")

    project_dir = Path(record.project_dir)
    if strategy == "overwrite":
        return _commit_overwrite(record, store, runner, project_dir)

    # copy_as_new
    if copy_display_name is None or not str(copy_display_name).strip():
        raise EditingConflictError(
            "copy_as_new strategy requires a non-empty copy_display_name"
        )
    new_job_id = (new_job_id_factory or _default_new_job_id)()
    return _commit_copy_as_new(
        record, store, runner,
        project_dir=project_dir,
        new_job_id=new_job_id,
        copy_display_name=str(copy_display_name).strip(),
    )


def _default_new_job_id() -> str:
    return f"job_{_uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# overwrite
# ---------------------------------------------------------------------------


def _commit_overwrite(
    record: JobRecord,
    store: JobStore,
    runner: CommitRunner,
    project_dir: Path,
) -> dict[str, Any]:
    # Step 1-2: apply edits into baseline (atomic per-file).
    summary = _apply_editing_to_baseline(project_dir)
    # Step 3: remove editing buffer.
    _rm_editing_dir(project_dir)
    # Step 3.5: reset alignment + publish to PENDING so pipeline re-runs
    # them against the just-applied edits instead of treating the source's
    # succeeded-era state as authoritative.
    _prune_overwrite_project_state(project_dir)

    # Step 4: flip status.
    now = _utc_now_iso()
    updated = replace(
        record,
        status=JOB_STATUS_RUNNING,
        current_stage=STAGE_ALIGNMENT,
        editing_touched_at=None,
        edit_generation=record.edit_generation + 1,
        updated_at=now,
    )
    store.save_job(updated)
    _emit_event(
        store, updated,
        message=(
            f"editing.commit_started: strategy=overwrite "
            f"edit_generation={updated.edit_generation}"
        ),
    )

    # Step 5: submit pipeline. Runner is expected to drive the pipeline from
    # alignment → publish; if that raises, we roll status back to editing so
    # the user can retry (their applied edits are already persisted — which
    # is intentional: commit moves drafts to baseline before submitting).
    try:
        submit_job_from_existing_project_dir(runner, updated, start_stage=STAGE_ALIGNMENT)
    except Exception as exc:
        logger.exception(
            "commit overwrite: runner.start failed for job %s",
            record.job_id,
        )
        # Roll status back to editing; drafts were already moved so the user
        # effectively sees their edits applied but the pipeline never ran.
        # Re-emit an editing state record so idle scanner starts afresh.
        failed = replace(
            updated,
            status=JOB_STATUS_EDITING,
            editing_touched_at=now,
            updated_at=_utc_now_iso(),
        )
        store.save_job(failed)
        _emit_event(
            store, failed,
            message=f"editing.commit_failed: strategy=overwrite err={exc}",
        )
        raise CommitPipelineError(
            f"runner failed to accept commit for {record.job_id}: {exc}"
        ) from exc

    return {
        "strategy": "overwrite",
        "job_id": updated.job_id,
        "edit_generation": updated.edit_generation,
        **summary,
    }


# ---------------------------------------------------------------------------
# copy_as_new — two-phase per D34
# ---------------------------------------------------------------------------


def _commit_copy_as_new(
    record: JobRecord,
    store: JobStore,
    runner: CommitRunner,
    *,
    project_dir: Path,
    new_job_id: str,
    copy_display_name: str,
) -> dict[str, Any]:
    now = _utc_now_iso()
    new_project_dir = _compute_copy_project_dir(project_dir, new_job_id)

    _emit_event(
        store, record,
        message=(
            f"editing.commit_started: strategy=copy_as_new "
            f"new_job_id={new_job_id}"
        ),
    )

    # Phase A: build target dir.
    try:
        prepare_copy_project_dir(project_dir, new_project_dir)
    except CopyPreparationError as exc:
        rollback_prepared_target(new_project_dir)
        _emit_event(
            store, record,
            message=f"editing.commit_failed: strategy=copy_as_new phase=A1 err={exc}",
        )
        raise CommitPipelineError(
            f"copy_as_new Phase A prepare failed: {exc}"
        ) from exc

    # Phase A continued: create new JobRecord.
    root_job_id = record.root_job_id or record.job_id
    # workspace_dir is the relative sibling-path of project_dir under the
    # user's projects root (shape: "projects/{user_id}/{job_id}"). replace()
    # keeps fields not listed, so we MUST explicitly carry over the target
    # identity — otherwise the new_record inherits source's workspace_dir
    # and manifest_path, and _resolve_job_project_dir can later pick up the
    # source-pointing workspace_dir as priority 2 fallback. (2026-04-19
    # incident: the new_record shipped with source workspace_dir, leading
    # to silent source pollution on the next commit overwrite.)
    if record.workspace_dir and record.job_id in record.workspace_dir:
        new_workspace_dir: str | None = record.workspace_dir.replace(
            record.job_id, new_job_id,
        )
    else:
        new_workspace_dir = None
    new_record = replace(
        record,
        job_id=new_job_id,
        status=JOB_STATUS_QUEUED,
        current_stage=STAGE_ALIGNMENT,
        project_dir=str(new_project_dir),
        workspace_dir=new_workspace_dir,
        # Clear manifest_path; _finalize_process of the new job's γ will
        # re-populate it from the target's project_state.json post-publish.
        manifest_path=None,
        display_name=copy_display_name,
        copy_of_job_id=record.job_id,
        root_job_id=root_job_id,
        edit_generation=0,
        editing_touched_at=None,
        started_at=None,
        completed_at=None,
        created_at=now,
        updated_at=now,
    )
    store.save_job(new_record)

    # Phase A final: submit runner. Any failure here rolls back Phase A.
    try:
        submit_job_from_existing_project_dir(
            runner, new_record, start_stage=STAGE_ALIGNMENT,
        )
    except Exception as exc:
        logger.exception(
            "copy_as_new phase A5: runner.start failed for new job %s",
            new_job_id,
        )
        rollback_prepared_target(new_project_dir)
        try:
            store.delete_job(new_job_id)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "copy_as_new rollback: failed to delete new job record %s",
                new_job_id,
            )
        _emit_event(
            store, record,
            message=(
                f"editing.commit_failed: strategy=copy_as_new phase=A5 "
                f"err={exc}; source editing/ preserved"
            ),
        )
        raise CommitPipelineError(
            f"copy_as_new Phase A runner.submit failed: {exc}"
        ) from exc

    # Phase B: source task becomes succeeded again; its editing/ dir is dropped.
    # Failures here are not rolled back (new job is already running) but we
    # DO log prominently so admins see the half-state in job_events.
    try:
        source_now = _utc_now_iso()
        source_updated = replace(
            record,
            status=JOB_STATUS_SUCCEEDED,
            editing_touched_at=None,
            updated_at=source_now,
        )
        store.save_job(source_updated)
        _rm_editing_dir(project_dir)
        _emit_event(
            store, source_updated,
            message=(
                f"editing.commit_succeeded: strategy=copy_as_new "
                f"new_job_id={new_job_id}"
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "copy_as_new phase B: source cleanup failed for %s; new job %s "
            "is already running. Admin intervention required.",
            record.job_id,
            new_job_id,
        )
        _emit_event(
            store, record,
            message=(
                f"editing.commit_phase_b_failed: strategy=copy_as_new "
                f"new_job_id={new_job_id} err={exc}"
            ),
        )

    return {
        "strategy": "copy_as_new",
        "source_job_id": record.job_id,
        "new_job_id": new_job_id,
        "new_project_dir": str(new_project_dir),
        "new_display_name": copy_display_name,
    }
