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
from services.jobs.events import (
    EVENT_LEVEL_CRITICAL,
    EVENT_LEVEL_INFO,
    EVENT_TYPE_STATUS,
    JobEvent,
)
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


def _emit_event(
    store: JobStore,
    record: JobRecord,
    *,
    message: str,
    level: str = EVENT_LEVEL_INFO,
    payload: dict[str, object] | None = None,
) -> None:
    """Append a commit-lifecycle event to the job's event log.

    Default level is INFO (for happy-path transitions). Pass
    ``level=EVENT_LEVEL_CRITICAL`` for needs-ops-intervention cases
    like D35 Phase B cleanup failures. ``payload`` carries structured
    context (e.g. ``failed_step``) that admins can read without having
    to parse the free-form message string.
    """
    try:
        event = JobEvent(
            job_id=record.job_id,
            event_type=EVENT_TYPE_STATUS,
            created_at=_utc_now_iso(),
            status=record.status,
            message=message,
            level=level,
            payload=payload,
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

    # 3) re-stamp tts_input_cn_text for every segment whose draft was
    #    promoted in step 2 — the draft was synthesized from that segment's
    #    CURRENT cn_text, and the audio just replaced baseline. Segments
    #    WITHOUT a promoted draft keep their existing tts_input_cn_text
    #    (which may equal cn_text → in sync, or differ → drift detected).
    #
    #    Plan ref: 2026-05-04-subtitle-audio-sync-plan.md Phase A Task A5/A6.
    #    Per §3.5 invariant, baseline audio is untouched until commit, so the
    #    accept-draft endpoint cannot stamp here — only commit can. This is
    #    the single, atomic stamp point covering both single-segment regen
    #    (A5) and batch regen-all-dirty (A6).
    if applied:
        applied_ids = set(applied)
        for seg in segments:
            sid = str(seg.get("segment_id", ""))
            if sid in applied_ids:
                seg["tts_input_cn_text"] = seg.get("cn_text", "")
        # Re-write the now-stamped segments.json over the version we wrote
        # at step 1. Two writes is mildly wasteful but keeps the existing
        # voice_map merge order intact (voice_map applies BEFORE we know
        # which drafts were promoted).
        target_file.write_text(
            json.dumps(segments, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return {
        "applied_draft_segment_ids": applied,
        "segments_count": len(segments),
        "voice_overrides_count": len(voice_map),
    }


def _rm_editing_dir(project_dir: Path) -> None:
    editing = project_dir / EDITING_SUBDIR
    if editing.exists():
        shutil.rmtree(editing, ignore_errors=True)


def _invalidate_jianying_draft_on_commit(job: JobRecord, project_dir: Path | str) -> None:
    """Reset Jianying draft state on Studio editing/commit overwrite.

    Phase 1 K1-K13 stored Jianying draft on JobRecord + a zip in
    {project_dir}/jianying/. After post-edit commit, these reflect
    pre-edit content, so we reset state to idle and delete the on-disk
    artifacts. User can re-trigger generation to get an up-to-date draft.

    Safe to call multiple times — idempotent.
    """
    # Reset JobRecord fields. Caller is responsible for store.save_job(job).
    job.jianying_draft_status = "idle"
    job.jianying_draft_started_at = None
    job.jianying_draft_completed_at = None
    job.jianying_draft_error = None
    job.jianying_draft_zip_path = None
    # Also reset user_root for consistency. User can re-enter it when
    # they re-trigger generation (it's not persisted between jobs).
    job.jianying_draft_user_root = None

    # Delete on-disk artifacts. shutil.rmtree silently if missing.
    jianying_root = Path(project_dir) / "jianying"
    if jianying_root.exists():
        shutil.rmtree(jianying_root, ignore_errors=True)


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

    2026-04-21 plan §12 / D8 — subtitle regeneration contract:
    Both strategies resume the pipeline at STAGE_ALIGNMENT, which flows
    through OutputDispatcher → EditorPackageWriter._write_srt() and
    **unconditionally rewrites all three SRTs** (zh / en / bilingual)
    from the just-committed editor/segments.json. The chain is:

        editing/segments.json (with user's cn_text / source_text edits)
        → _apply_editing_to_baseline → editor/segments.json
        → submit_job_from_existing_project_dir(start_stage='alignment')
        → process._run_alignment_and_publish_only
        → _load_segments_for_publish_resume (reads editor/segments.json)
        → _build_process_output_captions / _blocks (cn_text + source_text
          straight through, no caching)
        → EditorPackageWriter._write_srt (no skip-if-unchanged logic)

    Do NOT add "skip alignment when no text changed" optimisation — that
    would silently leak stale SRTs when users edit *only* text (no
    re-TTS). The publish stage is already cheap (~seconds) and the
    subtitle freshness guarantee is the whole point of resuming at
    alignment.
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

    # Step 4.5: invalidate Jianying draft state. Post-edit commit
    # regenerates alignment/publish (SRTs, audio if re-TTS'd), so any
    # existing Jianying draft becomes stale. Reset state to idle and
    # delete on-disk artifacts so user sees "生成剪映草稿" button again
    # (forcing a re-trigger to get up-to-date content).
    _invalidate_jianying_draft_on_commit(updated, project_dir)

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
        # Reset Jianying draft state for the new copy. The source's Jianying
        # draft is for the source's output; the copy has its own output and
        # needs a fresh draft if the user wants one. (Without explicit reset,
        # replace() silently inherits source's jianying_draft_* fields.)
        jianying_draft_status="idle",
        jianying_draft_started_at=None,
        jianying_draft_completed_at=None,
        jianying_draft_error=None,
        jianying_draft_zip_path=None,
        jianying_draft_user_root=None,
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
    # Failures here are not rolled back (new job is already running) — we
    # track which step failed so ops can diagnose from job_events without
    # tailing docker logs. Plan §7.8 / D35 contract.
    failed_step: str | None = None
    source_updated: JobRecord = record
    try:
        source_now = _utc_now_iso()
        source_updated = replace(
            record,
            status=JOB_STATUS_SUCCEEDED,
            editing_touched_at=None,
            updated_at=source_now,
        )
        failed_step = "save_job"
        store.save_job(source_updated)
        failed_step = "rm_editing_dir"
        _rm_editing_dir(project_dir)
        failed_step = None  # success
        _emit_event(
            store, source_updated,
            message=(
                f"editing.commit_succeeded: strategy=copy_as_new "
                f"new_job_id={new_job_id}"
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive
        # D35: elevate to CRITICAL so LogViewer / ops channel picks it up.
        # The 24h idle-scanner will eventually tide over the source's
        # stuck 'editing' state (cancel_editing flow), but that leaves a
        # gap where the user sees the source as "modifying" for hours
        # despite the new copy already running — hence the loud alert.
        logger.critical(
            "copy_as_new phase B failed at step=%s for source=%s "
            "(new job %s already running). Source may be stuck in "
            "'editing' until the 24h idle scanner cancels it — admin "
            "may want to force cancel + rm editor/editing/ manually. "
            "Exception: %s",
            failed_step, record.job_id, new_job_id, exc,
            exc_info=True,
        )
        _emit_event(
            store, record,
            message=(
                f"editing.commit_phase_b_failed: strategy=copy_as_new "
                f"new_job_id={new_job_id} failed_step={failed_step} err={exc}"
            ),
            level=EVENT_LEVEL_CRITICAL,
            payload={
                "event_type": "editing.commit_phase_b_failed",
                "strategy": "copy_as_new",
                "source_job_id": record.job_id,
                "new_job_id": new_job_id,
                "failed_step": failed_step,
                "exception_class": type(exc).__name__,
                "exception_message": str(exc)[:500],
            },
        )

    return {
        "strategy": "copy_as_new",
        "source_job_id": record.job_id,
        "new_job_id": new_job_id,
        "new_project_dir": str(new_project_dir),
        "new_display_name": copy_display_name,
    }
