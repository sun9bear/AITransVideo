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
    "EditingAudioSyncRequiredError",
    "commit_editing_pipeline",
]


class CommitPipelineError(Exception):
    """Raised when commit_editing_pipeline cannot complete. Caller should
    surface the message to the user so they know their editing state is
    (depending on phase) either fully preserved for retry or half-applied."""


class EditingAudioSyncRequiredError(EditingConflictError):
    """Raised when text edits would be committed without matching TTS audio."""

    code = "editing_audio_sync_required"

    def __init__(self, unsynced_segments: list[dict[str, Any]]) -> None:
        self.unsynced_segments = unsynced_segments
        self.payload = {
            "code": self.code,
            "message": "Some edited text segments need regenerated TTS before commit.",
            "unsynced_segments": unsynced_segments,
        }
        super().__init__(self.payload["message"])


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


def _merge_editing_speakers_into_review_state(
    project_dir: Path | str,
    edit_speakers: list,  # list[EditingSpeaker]
) -> None:
    """Merge editing speakers into baseline review_state.json (Task 9).

    Updates:
    - ``speaker_review.payload.speaker_names`` — adds new speaker_id → display_name
    - ``speaker_review.payload.speaker_options`` — keeps unique sorted list
    - ``voice_selection_review.payload.voice_profiles`` — adds speaker_id → profile,
      ONLY for speakers with ``status='ready'`` AND non-empty ``voice_profile``.

    Idempotent: re-running with the same input yields the same output.
    No-op if ``edit_speakers`` is empty.

    Note: ``ReviewStateManager.set_stage`` REPLACES the payload (not merges),
    so we always read existing payload first, mutate locally, and write back
    the full result.
    """
    if not edit_speakers:
        return

    from services.review_state import (
        ReviewStateManager,
        SPEAKER_REVIEW_STAGE,
        VOICE_SELECTION_REVIEW_STAGE,
        REVIEW_STATUS_APPROVED,
    )

    rs_path = Path(project_dir) / "review_state.json"
    if not rs_path.is_file():
        # baseline review_state 不存在 — commit 不应抵达这里,但容错跳过
        return
    manager = ReviewStateManager(rs_path)

    # speaker_review
    sr = manager.get_stage(SPEAKER_REVIEW_STAGE) or {}
    sr_payload = dict(sr.get("payload") or {})
    names = dict(sr_payload.get("speaker_names") or {})
    for sp in edit_speakers:
        names[sp.speaker_id] = sp.display_name
    sr_payload["speaker_names"] = names
    # Preserve dict insertion order (Python 3.7+) — matches baseline writers
    # in pipeline/process.py:3596-3600 which use detection order. New
    # editing speakers naturally append to the end.
    sr_payload["speaker_options"] = [
        {"speaker_id": sid, "display_name": dn}
        for sid, dn in names.items()
    ]
    sr_status = sr.get("status") or REVIEW_STATUS_APPROVED
    manager.set_stage(SPEAKER_REVIEW_STAGE, status=sr_status, payload=sr_payload)

    # voice_selection_review:
    # 1) voice_profiles dict (仅写 ready 且有 profile 的)
    # 2) speakers list (前端 VoiceModifyTab 加载的真实数据源 — 必须 append,
    #    否则 commit 后重进编辑,音色 Tab 看不到 editing-added speaker)。
    #    2026-05-09 round 3 追修:之前只写 profiles dict 漏了 speakers list。
    profiles_to_write = {
        sp.speaker_id: sp.voice_profile
        for sp in edit_speakers
        if sp.profile_status == "ready" and sp.voice_profile
    }

    # 计算每个新 speaker 的 segment_count + total_duration_s 从 baseline
    # editor/segments.json (此时 _apply_editing_to_baseline 已把 editing
    # 段写到 baseline)。auto_match / probe_texts 字段全空 — 用户后续可
    # 显式 clone 或选预设音色。
    baseline_segs_path = Path(project_dir) / "editor" / "segments.json"
    seg_stats: dict[str, tuple[int, int]] = {}  # speaker_id -> (count, total_ms)
    if baseline_segs_path.is_file():
        try:
            data = json.loads(baseline_segs_path.read_text("utf-8"))
            if isinstance(data, dict):
                data = data.get("segments", [])
            if isinstance(data, list):
                for s in data:
                    if not isinstance(s, dict):
                        continue
                    sid = s.get("speaker_id")
                    if not isinstance(sid, str):
                        continue
                    dur = int(s.get("end_ms", 0)) - int(s.get("start_ms", 0))
                    if dur < 0:
                        dur = 0
                    cur = seg_stats.get(sid, (0, 0))
                    seg_stats[sid] = (cur[0] + 1, cur[1] + dur)
        except (OSError, json.JSONDecodeError):
            pass

    vsr = manager.get_stage(VOICE_SELECTION_REVIEW_STAGE) or {}
    vsr_payload = dict(vsr.get("payload") or {})
    vsr_dirty = False

    # voice_profiles dict
    if profiles_to_write:
        merged_profiles = dict(vsr_payload.get("voice_profiles") or {})
        merged_profiles.update(profiles_to_write)
        vsr_payload["voice_profiles"] = merged_profiles
        vsr_dirty = True

    # speakers list — append editing speakers that aren't already there
    existing_speakers = list(vsr_payload.get("speakers") or [])
    existing_ids = {s.get("speaker_id") for s in existing_speakers if isinstance(s, dict)}
    for sp in edit_speakers:
        if sp.speaker_id in existing_ids:
            continue
        seg_count, total_ms = seg_stats.get(sp.speaker_id, (0, 0))
        existing_speakers.append({
            "speaker_id": sp.speaker_id,
            "speaker_name": sp.display_name,
            "segment_count": seg_count,
            "total_duration_s": total_ms / 1000.0,
            # editing-added speaker 没跑过 auto-match,留空让前端走 fallback
            # 选预设音色或显式 clone
            "auto_matched_by_provider": {},
            "auto_matched_voice": None,
            "probe_texts": [],
            # can_clone:依靠时长够不够 (主流程用 5s 阈值,这里保持一致)
            "can_clone": total_ms >= 5000,
        })
        existing_ids.add(sp.speaker_id)
        vsr_dirty = True
    if vsr_dirty:
        vsr_payload["speakers"] = existing_speakers

    if vsr_dirty:
        vsr_status = vsr.get("status") or REVIEW_STATUS_APPROVED
        manager.set_stage(
            VOICE_SELECTION_REVIEW_STAGE,
            status=vsr_status,
            payload=vsr_payload,
        )


def _invalidate_jianying_draft_on_commit(job: JobRecord, project_dir: Path | str) -> None:
    """Reset Jianying draft state on Studio editing/commit overwrite.

    Phase 1 K1-K13 stored Jianying draft on JobRecord + a zip in
    {project_dir}/jianying/. After post-edit commit, these reflect
    pre-edit content, so we reset state to idle and delete the on-disk
    artifacts. User can re-trigger generation to get an up-to-date draft.

    Safe to call multiple times — idempotent.

    P1-15b batch 3 (audit 2026-05-07): the production caller in
    ``_commit_overwrite`` no longer uses this helper — it inlines the
    record-field reset into the ``update_job`` mutator (so the field
    flip is atomic with the editing→running status flip) and runs the
    on-disk rmtree after a successful transition. The helper is kept
    here for the docs/graphs/* references and for ad-hoc admin tooling
    that might want to clear a stale draft without driving a state
    transition. No production code path calls it as of 2026-05-07.

    P1-15b batch 4 follow-up (Codex review 6abba13): also clear
    attempt_id / substep / fingerprint. The runner's mutator-side
    defense (status==running guard) makes attempt_id leakage harmless,
    but leaving stale identity fields invites confusion in admin
    tooling and reap_stale heuristics. Clearing them keeps the
    "claim retired" contract crisp.
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
    # Clear claim identity so a stale background worker that bypassed
    # the status guard (e.g. via legacy code path) cannot resurrect
    # state under this attempt_id.
    job.jianying_draft_attempt_id = None
    job.jianying_draft_substep = None
    job.jianying_draft_fingerprint = None

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
    _require_text_audio_sync_before_commit(project_dir)
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


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _segments_by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("segment_id")): item
        for item in items
        if item.get("segment_id") is not None
    }


def _load_segment_status_map(project_dir: Path) -> dict[str, str]:
    path = project_dir / EDITING_SUBDIR / "segment_status.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _find_text_edits_without_tts(project_dir: Path) -> list[dict[str, Any]]:
    """Return text edits whose current audio still represents older text.

    ``text_dirty`` is the authoritative UI/backend signal that a segment's
    text changed after the last matching TTS synthesis. A draft wav may still
    exist from an earlier regeneration; if the status is text_dirty, that
    draft is stale too and must not be promoted at commit.

    P0-8 (audit 2026-05-07): segments produced by split_editing_segment
    have segment_ids (e.g. ``seg_005_s1a``/``_s1b``) that exist in the
    editing/segments.json copy but NOT in the baseline editor/segments.json
    (split is not applied to baseline until commit). Previously this
    branch silently ``continue``-d, so a user who split + edited text +
    skipped re-TTS would pass the audio-sync gate and only fail at the
    alignment stage with "missing wavs" — confusing and hard to recover.
    Now: a missing baseline segment requires a fresh draft wav to clear
    the gate; otherwise the segment surfaces in the unsynced list.
    """
    status_map = _load_segment_status_map(project_dir)
    if not status_map:
        return []
    editing_dir = project_dir / EDITING_SUBDIR
    drafts_dir = editing_dir / "tts_segments_draft"
    editing_by_id = _segments_by_id(_load_json_list(editing_dir / "segments.json"))
    baseline_by_id = _segments_by_id(_load_json_list(project_dir / "editor" / "segments.json"))
    unsynced: list[dict[str, Any]] = []
    for sid, status in sorted(status_map.items()):
        if status != "text_dirty":
            continue
        editing_segment = editing_by_id.get(sid)
        if not editing_segment:
            # Editing layer doesn't have this id either — defensive skip.
            continue
        baseline_segment = baseline_by_id.get(sid)
        has_draft = (drafts_dir / f"{sid}.wav").is_file()
        if baseline_segment is None:
            # Split halves and any other "new in editing only" id reach
            # this branch. Without a draft wav there is no audio that
            # represents the current text — the segment is unsynced.
            if has_draft:
                continue
            unsynced.append({
                "segment_id": sid,
                "status": status,
                "display_name": editing_segment.get("display_name") or "",
                "speaker_id": editing_segment.get("speaker_id") or "",
                "current_cn_text": str(editing_segment.get("cn_text") or ""),
                "audio_cn_text": "",
                "current_source_text": editing_segment.get("source_text") or "",
                "audio_source_text": "",
            })
            continue
        current_text = str(editing_segment.get("cn_text") or "")
        audio_text = str(
            baseline_segment.get("tts_input_cn_text")
            or baseline_segment.get("cn_text")
            or ""
        )
        if current_text == audio_text and not has_draft:
            continue
        unsynced.append({
            "segment_id": sid,
            "status": status,
            "display_name": editing_segment.get("display_name") or "",
            "speaker_id": editing_segment.get("speaker_id") or "",
            "current_cn_text": current_text,
            "audio_cn_text": audio_text,
            "current_source_text": editing_segment.get("source_text") or "",
            "audio_source_text": baseline_segment.get("source_text") or "",
        })
    return unsynced


def _require_text_audio_sync_before_commit(project_dir: Path) -> None:
    unsynced = _find_text_edits_without_tts(project_dir)
    if unsynced:
        raise EditingAudioSyncRequiredError(unsynced)


# ---------------------------------------------------------------------------
# overwrite
# ---------------------------------------------------------------------------


def _commit_overwrite(
    record: JobRecord,
    store: JobStore,
    runner: CommitRunner,
    project_dir: Path,
) -> dict[str, Any]:
    # P1-15b batch 3 follow-up (Codex review of 0949f75):
    #
    # ORDER MATTERS — claim the transition under the per-job lock
    # BEFORE any destructive filesystem work. The previous ordering
    # (apply_editing → rm_editing_dir → prune_state → update_job)
    # had a real failure mode: if a concurrent cancel committed
    # ``editing → succeeded`` between JobService's stale require_job
    # snapshot and update_job's lock acquisition, this commit would
    # raise CommitPipelineError AFTER having already promoted the
    # editing/ buffer into the baseline, removed the editing dir,
    # and pruned project_state. The user/admin would see "cancel
    # won" but the cancelled edits would already be on disk.
    #
    # New flow: 1) claim transition; 2) do FS work; 3) on FS failure,
    # roll status back to editing. This way a lost concurrent claim
    # (mutator raise) leaves the filesystem completely untouched.
    now = _utc_now_iso()

    def _flip_to_running(current: JobRecord) -> JobRecord:
        if current.status != JOB_STATUS_EDITING:
            raise CommitPipelineError(
                f"job {current.job_id} is no longer in editing state "
                f"(current: {current.status}); cannot commit overwrite"
            )
        return replace(
            current,
            status=JOB_STATUS_RUNNING,
            current_stage=STAGE_ALIGNMENT,
            editing_touched_at=None,
            edit_generation=current.edit_generation + 1,
            updated_at=now,
            # Reset Jianying draft fields atomically with the status
            # flip. The on-disk artifacts get rmtree-d below after
            # update_job returns (FS side-effect, deferred).
            # P1-15b batch 4 follow-up (Codex 6abba13): clear claim
            # identity (attempt_id / substep / fingerprint) too — a
            # stale runner thread checks status==running first and
            # bails, but a legacy code path or admin override that
            # only inspects attempt_id should also see the slot
            # vacated. The runner-side guard is the defense; clearing
            # these here keeps the contract clean.
            jianying_draft_status="idle",
            jianying_draft_started_at=None,
            jianying_draft_completed_at=None,
            jianying_draft_error=None,
            jianying_draft_zip_path=None,
            jianying_draft_user_root=None,
            jianying_draft_attempt_id=None,
            jianying_draft_substep=None,
            jianying_draft_fingerprint=None,
        )

    # Step 1 — atomic claim. If a concurrent cancel won, this raises
    # WITHOUT having touched the filesystem.
    updated = store.update_job(record.job_id, _flip_to_running)

    # Step 2 — destructive FS work, now that we own the transition.
    # If anything below fails, roll status back to editing so the
    # user/admin can see commit didn't complete (and perhaps retry).
    # We don't restore the FS itself — once apply_editing_to_baseline
    # has run, the baseline is already mutated; rolling status back
    # is the most we can do without a transactional FS.
    try:
        # Step 2a: apply edits into baseline (atomic per-file).
        summary = _apply_editing_to_baseline(project_dir)
        # Step 2a-bis (Task 9): merge editing/speakers.json into baseline
        # review_state.json so newly-created speakers' display_names and
        # voice_profiles persist past commit. Best-effort — failure here
        # is logged but does not fail the commit (display_name will fall
        # back to speaker_id in downstream rendering, which is recoverable
        # by re-entering edit and editing display_names manually).
        # MUST run before _rm_editing_dir below (which deletes speakers.json).
        try:
            from services.jobs.editing_speakers import (
                load_speakers as _load_editing_speakers,
            )
            _edit_speakers = _load_editing_speakers(project_dir)
            if _edit_speakers:
                _merge_editing_speakers_into_review_state(
                    project_dir, _edit_speakers,
                )
        except Exception:
            logger.exception(
                "_commit_overwrite: speakers merge into review_state failed; "
                "continuing with the rest of overwrite (display_name fallback "
                "to speaker_id will still work)"
            )
        # Step 2b: remove editing buffer.
        _rm_editing_dir(project_dir)
        # Step 2c: reset alignment + publish to PENDING so pipeline re-runs
        # them against the just-applied edits instead of treating the
        # source's succeeded-era state as authoritative.
        _prune_overwrite_project_state(project_dir)
        # Step 2d: drop stale Jianying draft zip so the user sees the
        # regenerate button again.
        jianying_root = Path(project_dir) / "jianying"
        if jianying_root.exists():
            shutil.rmtree(jianying_root, ignore_errors=True)
    except Exception as exc:
        logger.exception(
            "commit overwrite: FS prep step failed AFTER claiming the "
            "editing→running transition for job %s; rolling status "
            "back to editing so caller can retry",
            record.job_id,
        )
        rollback_now = _utc_now_iso()

        def _rollback_to_editing_after_fs_fail(current: JobRecord) -> JobRecord:
            # Only roll back if WE still own the transition (status
            # is still RUNNING from our flip). If a third party (admin
            # force, idle reaper) already moved it elsewhere, leave
            # it alone — fighting the third party would just thrash.
            if current.status != JOB_STATUS_RUNNING:
                return current
            return replace(
                current,
                status=JOB_STATUS_EDITING,
                editing_touched_at=now,
                updated_at=rollback_now,
            )

        try:
            rolled = store.update_job(record.job_id, _rollback_to_editing_after_fs_fail)
            _emit_event(
                store, rolled,
                message=(
                    f"editing.commit_failed: strategy=overwrite "
                    f"phase=fs_prep err={exc}"
                ),
            )
        except Exception:  # pragma: no cover — defensive
            logger.exception(
                "commit overwrite: rollback also failed for job %s; "
                "record may be stuck in 'running' without a worker",
                record.job_id,
            )
        raise CommitPipelineError(
            f"commit overwrite FS prep failed for {record.job_id}: {exc}"
        ) from exc

    _emit_event(
        store, updated,
        message=(
            f"editing.commit_started: strategy=overwrite "
            f"edit_generation={updated.edit_generation}"
        ),
    )

    # Step 4.9 — pre-submit re-validate the claim. The Step 2 FS prep
    # window between update_job(_flip_to_running) and runner.start
    # below is a real cancel/admin race surface: the job has been in
    # status=running for hundreds of ms without a worker process.
    # If a concurrent cancel landed during that window, we MUST NOT
    # call runner.start with a stale `updated` snapshot — runner.start's
    # own _save_job has its own fail-closed guard now (P1-15b batch 3
    # follow-up²), but we surface the conflict here too so the caller
    # gets a clean CommitPipelineError instead of an opaque
    # RunnerStartTerminalError. Cheap O(1) load + check under the
    # per-job lock.
    expected_generation = updated.edit_generation

    def _verify_claim_intact(current: JobRecord) -> JobRecord:
        if current.status != JOB_STATUS_RUNNING:
            raise CommitPipelineError(
                f"job {current.job_id} status changed from running to "
                f"{current.status} between commit claim and runner submit "
                f"(likely concurrent cancel)"
            )
        if current.edit_generation != expected_generation:
            raise CommitPipelineError(
                f"job {current.job_id} edit_generation changed from "
                f"{expected_generation} to {current.edit_generation} "
                f"between commit claim and runner submit"
            )
        return current  # identity — no-op write
    try:
        store.update_job(record.job_id, _verify_claim_intact)
    except CommitPipelineError as exc:
        logger.exception(
            "commit overwrite: claim invalidated before runner submit "
            "for job %s",
            record.job_id,
        )
        # Do NOT roll status back here — the concurrent action that
        # invalidated our claim already moved status where it should
        # be (cancelled / failed / etc). Surfacing the error to the
        # caller is enough.
        _emit_event(
            store, updated,
            message=(
                f"editing.commit_failed: strategy=overwrite "
                f"phase=pre_submit err={exc}"
            ),
        )
        raise

    # Step 5: submit pipeline. Runner is expected to drive the pipeline from
    # alignment → publish. Failures fall into two categories now:
    #   (a) RunnerStartTerminalError — concurrent cancel landed BETWEEN
    #       _verify_claim_intact above and runner.start's own atomic
    #       claim. The cancel already moved status correctly; we MUST
    #       NOT roll status back to editing (that would resurrect a
    #       cancelled job). Just surface CommitPipelineError.
    #   (b) Other exceptions — runner couldn't accept the start (e.g.
    #       OS error, unavailable runner). Roll status back to editing
    #       so the user can retry; drafts were already moved so their
    #       work is not lost.
    try:
        from services.jobs.process_runner import RunnerStartTerminalError
    except ImportError:  # pragma: no cover — defensive
        RunnerStartTerminalError = ()  # type: ignore[assignment]

    try:
        submit_job_from_existing_project_dir(runner, updated, start_stage=STAGE_ALIGNMENT)
    except RunnerStartTerminalError as exc:
        logger.warning(
            "commit overwrite: runner.start refused job %s because "
            "current status is terminal (%s) — concurrent cancel won "
            "the final race window. Not rolling status back.",
            record.job_id, getattr(exc, "observed_status", "?"),
        )
        _emit_event(
            store, store.require_job(record.job_id),
            message=(
                f"editing.commit_failed: strategy=overwrite "
                f"phase=runner_start observed_status={getattr(exc, 'observed_status', '?')}"
            ),
        )
        raise CommitPipelineError(
            f"runner refused commit for {record.job_id}: {exc}"
        ) from exc
    except Exception as exc:
        logger.exception(
            "commit overwrite: runner.start failed for job %s",
            record.job_id,
        )
        # Roll status back to editing; drafts were already moved so the user
        # effectively sees their edits applied but the pipeline never ran.
        # P1-15b batch 3: route through update_job so this rollback can't
        # clobber a concurrent cancel/admin write that landed between
        # update_job(_flip_to_running) and the runner.start failure.
        rollback_now = _utc_now_iso()

        def _rollback_to_editing(current: JobRecord) -> JobRecord:
            # If the concurrent winner already moved status away from
            # running (e.g. user pressed cancel before runner.start
            # actually attempted), don't resurrect editing state.
            if current.status != JOB_STATUS_RUNNING:
                return current
            return replace(
                current,
                status=JOB_STATUS_EDITING,
                editing_touched_at=now,
                updated_at=rollback_now,
            )

        failed = store.update_job(record.job_id, _rollback_to_editing)
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

    # Phase A bis (Task 9): merge source's editing/speakers.json into the
    # new project's review_state.json. We MUST read editing speakers from
    # the source project_dir (Phase B has not yet deleted source editing/),
    # but write to the new_project_dir's review_state.json.
    # Best-effort — failure here is logged but does not abort the copy
    # (the new job still runs; new speakers' display_names will fall back
    # to speaker_id, recoverable by re-entering edit on the new job).
    try:
        from services.jobs.editing_speakers import (
            load_speakers as _load_editing_speakers,
        )
        _edit_speakers = _load_editing_speakers(project_dir)  # source dir
        if _edit_speakers:
            _merge_editing_speakers_into_review_state(
                new_project_dir, _edit_speakers,
            )
    except Exception:
        logger.exception(
            "_commit_copy_as_new: speakers merge into new job's "
            "review_state failed; new job's display_names fallback to "
            "speaker_id"
        )

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
        # P1-15b batch 4 follow-up (Codex 6abba13): also clear claim
        # identity (attempt_id / substep / fingerprint) on the new copy
        # so its idle slot is unambiguously vacant. No source-job worker
        # writes here (different job_id keys a different lock + record),
        # but admin tooling and reap_stale heuristics that scan all
        # jobs see a clean record instead of an "idle but with claim
        # identity" half-state.
        jianying_draft_status="idle",
        jianying_draft_started_at=None,
        jianying_draft_completed_at=None,
        jianying_draft_error=None,
        jianying_draft_zip_path=None,
        jianying_draft_user_root=None,
        jianying_draft_attempt_id=None,
        jianying_draft_substep=None,
        jianying_draft_fingerprint=None,
    )
    # P1-15b batch 3: first-write on a fresh job_id; use update_job
    # with initial=new_record so the write happens under the new
    # job's per-job lock. There's no on-disk record yet (just-allocated
    # job_id), so update_job's load_job returns None and falls back to
    # the initial record. Mutator is a no-op (identity) since we have
    # nothing to merge.
    store.update_job(
        new_record.job_id,
        lambda current: current,
        initial=new_record,
    )

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
        # P1-15b batch 3: route the source's editing→succeeded flip
        # through update_job so a concurrent cancel/admin write on the
        # source can't clobber Phase B's transition (or vice versa).
        # If the source's status is no longer EDITING under the lock,
        # an admin/idle-scanner force-cancel beat us — we do nothing
        # (the source is already out of editing) but still proceed to
        # rm_editing_dir as defense-in-depth FS cleanup.
        def _flip_source_to_succeeded(current: JobRecord) -> JobRecord:
            if current.status != JOB_STATUS_EDITING:
                return current  # concurrent winner already finished
            return replace(
                current,
                status=JOB_STATUS_SUCCEEDED,
                editing_touched_at=None,
                updated_at=source_now,
            )
        failed_step = "save_job"
        source_updated = store.update_job(record.job_id, _flip_source_to_succeeded)
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
