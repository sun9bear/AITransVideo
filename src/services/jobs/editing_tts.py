"""Single-segment TTS re-synthesis + draft accept/discard (T1-5).

Plan ref: §3.5 (directory layout) + §7.4 (UI flow) + D26 (commit must
never re-invoke TTS — T1-5 writes drafts only, commit consumes them).

Public surface:

- ``regenerate_segment_tts(project_dir, segment_id, tts_caller)`` — call
  ``tts_caller`` to produce a wav into ``editor/editing/tts_segments_draft/``.
  Baseline ``editor/tts_segments/{sid}.wav`` is **never touched**. Failures
  mark the segment ``tts_failed`` and surface the original exception so
  the caller can return a descriptive 500.
- ``accept_draft_tts(project_dir, segment_id)`` — user clicked "接受": mark
  segment ``accepted``, keep the draft file in place (commit will later move
  it to baseline).
- ``discard_draft_tts(project_dir, segment_id)`` — user clicked "丢弃": delete
  the draft, then demote segment_status via
  ``compute_residual_segment_status`` so any surviving dirty source
  (text edit / voice override) is preserved. Only if no residual dirt
  remains does the segment fall back to ``accepted`` + baseline audio.

TTS caller is **injected** rather than imported: (a) real TTS providers
are paid APIs and the CLAUDE.md constraint forbids silent invocation, so
T1-5 ships with a ``_not_wired_tts_caller`` placeholder that raises
``NotImplementedError`` — real provider wiring happens in a dedicated
Phase 2 step where we add tests asserting the caller only runs under
a user-initiated HTTP handler; (b) tests inject deterministic fakes.

Frontend draft audio streaming lives in a separate future endpoint
(``GET /segments/{sid}/draft-audio``); T1-5 only manages the file lifecycle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from services.jobs.editing import EDITING_SUBDIR, EditingConflictError
from services.jobs.editing_segments import (
    SEGMENT_STATUS_ACCEPTED,
    SEGMENT_STATUS_TTS_DIRTY,
    SEGMENT_STATUS_TTS_FAILED,
    SEGMENT_STATUS_TTS_LOADING,
    compute_residual_segment_status,
    load_editing_segments,
    load_segment_status,
    mark_segment_status,
)
from services.jobs.editing_voice_map import load_voice_map
from services.jobs.input_validators import validate_segment_id
from utils.audio_fit import fit_audio_to_slot

logger = logging.getLogger(__name__)

__all__ = [
    "DRAFT_TTS_SUBDIR",
    "SegmentTTSCaller",
    "TtsNotWiredError",
    "accept_draft_tts",
    "discard_draft_tts",
    "draft_audio_path",
    "regenerate_segment_tts",
]

# Relative path within project_dir where per-segment draft wav files live.
DRAFT_TTS_SUBDIR: str = f"{EDITING_SUBDIR}/tts_segments_draft"

# Signature of the injected TTS caller:
#   tts_caller(segment_dict, output_path) -> None
# - segment_dict: the full segment dict (cn_text / voice_id / speaker_id / timing)
# - output_path: pathlib.Path pointing to the desired draft wav location;
#   the caller must create it (parent dir already exists) before returning.
SegmentTTSCaller = Callable[[dict[str, Any], Path], None]


class TtsNotWiredError(NotImplementedError):
    """Raised by the default ``_not_wired_tts_caller`` when T1-5 is exercised
    without real TTS provider wiring. Surfaced as HTTP 501 by the API layer,
    so frontend can render "功能即将上线" rather than a crash toast."""


def _segment_slot_duration_ms(segment: dict[str, Any]) -> int:
    try:
        start_ms = int(segment.get("start_ms") or 0)
        end_ms = int(segment.get("end_ms") or 0)
    except (TypeError, ValueError):
        start_ms = 0
        end_ms = 0
    slot_duration_ms = max(0, end_ms - start_ms)
    if slot_duration_ms > 0:
        return slot_duration_ms
    try:
        return max(0, int(segment.get("target_duration_ms") or 0))
    except (TypeError, ValueError):
        return 0


def _speaker_voice_overlay(
    segments: list[dict[str, Any]],
    target_index: int,
    segment: dict[str, Any],
) -> dict[str, Any]:
    """Overlay the current speaker's representative voice onto ``segment``.

    Split/edit flows can change ``speaker_id`` after the baseline segment was
    copied. Re-TTS must follow the current speaker, not stale inherited voice
    fields. Per-segment voice_map overrides are applied later and still win.
    """
    speaker_id = segment.get("speaker_id")
    if not isinstance(speaker_id, str) or not speaker_id:
        return segment

    rep_voice_id: str | None = None
    rep_tts_provider: str | None = None
    rep_tts_model_key: str | None = None
    for index, candidate in enumerate(segments):
        if index == target_index or not isinstance(candidate, dict):
            continue
        if candidate.get("speaker_id") != speaker_id:
            continue
        if rep_voice_id is None:
            voice = candidate.get("voice_id")
            if isinstance(voice, str) and voice:
                rep_voice_id = voice
        if rep_tts_provider is None:
            provider = candidate.get("tts_provider") or candidate.get("provider")
            if isinstance(provider, str) and provider:
                rep_tts_provider = provider
        if rep_tts_model_key is None:
            model_key = candidate.get("tts_model_key")
            if isinstance(model_key, str) and model_key:
                rep_tts_model_key = model_key
        if (
            rep_voice_id is not None
            and rep_tts_provider is not None
            and rep_tts_model_key is not None
        ):
            break

    if rep_voice_id is None and rep_tts_provider is None and rep_tts_model_key is None:
        return segment

    overlaid = dict(segment)
    if rep_voice_id:
        overlaid["voice_id"] = rep_voice_id
    if rep_tts_provider:
        overlaid["tts_provider"] = rep_tts_provider
        overlaid.pop("provider", None)
    if rep_tts_model_key:
        overlaid["tts_model_key"] = rep_tts_model_key
    return overlaid


def _not_wired_tts_caller(segment: dict[str, Any], output_path: Path) -> None:
    """Default caller: refuses with a clear message. Real wiring arrives in
    a dedicated TTS-router task (tracked as out-of-T1-5 follow-up so the
    paid-API surface has an explicit, AST-scannable entry point)."""
    raise TtsNotWiredError(
        "segment TTS re-generation requires provider wiring that is not yet "
        "in place. Inject a real SegmentTTSCaller (see services.tts.segment_regenerate) "
        "at service construction time."
    )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def draft_audio_path(project_dir: str | Path, segment_id: str) -> Path:
    """Resolve the draft wav path for ``segment_id``.

    Does NOT validate the segment_id (callers pass through ``validate_segment_id``
    first). Exposed so upload / download handlers can share the path logic.
    """
    return Path(project_dir) / DRAFT_TTS_SUBDIR / f"{segment_id}.wav"


def _baseline_audio_path(project_dir: str | Path, segment_id: str) -> Path:
    return Path(project_dir) / "editor" / "tts_segments" / f"{segment_id}.wav"


# ---------------------------------------------------------------------------
# Regenerate
# ---------------------------------------------------------------------------


def regenerate_segment_tts(
    project_dir: str | Path,
    segment_id: str,
    *,
    tts_caller: SegmentTTSCaller | None = None,
    default_tts_model: str | None = None,
) -> dict[str, Any]:
    """Produce a draft TTS wav for ``segment_id``. Returns metadata for the UI.

    Flow:
      1. Validate segment_id allowlist.
      2. Locate the segment in editing/segments.json (404 if absent).
      3. Ensure draft dir exists.
      4. Flag segment_status = tts_loading (UI can disable re-click during call).
      5. Call ``tts_caller`` to write the wav. ``tts_caller`` default is the
         "not wired" placeholder — real wiring is DI.
      6. On success: verify file exists, flag tts_dirty.
      7. On failure: flag tts_failed, re-raise.

    Important: this function **never touches** ``editor/tts_segments/{sid}.wav``
    (the baseline). If the caller wants that audio replaced, it must use the
    commit path (T1-9). Accept/discard only manage the draft.
    """
    validate_segment_id(segment_id)

    caller = tts_caller or _not_wired_tts_caller

    segments = load_editing_segments(project_dir)
    segment = None
    segment_index: int | None = None
    target = str(segment_id)
    for index, s in enumerate(segments):
        if isinstance(s, dict) and str(s.get("segment_id")) == target:
            segment = s
            segment_index = index
            break
    if segment is None:
        raise EditingConflictError(
            f"segment_id {segment_id!r} not found in editing/segments.json"
        )

    # Overlay voice_map.json override onto the segment dict before handing
    # it to the caller (CodeX A.2 P1). voice_map is the authoritative source
    # for per-segment provider/voice_id picks made in the editing session —
    # the underlying editing/segments.json baseline carries the original
    # pipeline selection, which must be preserved there so clear_voice_override
    # can revert. Without this overlay, a user changing voice via the Phase 2
    # voice-modify Tab would still hear the old voice after 重新合成, and a
    # baseline record missing tts_provider would silently fall back to the
    # global default provider inside _generate_one — violating the "no
    # silent provider switch" contract wired into the caller.
    voice_map = load_voice_map(project_dir)
    override = voice_map.get(target)
    if override:
        segment = {
            **segment,
            "tts_provider": override["provider"],
            "voice_id": override["voice_id"],
        }
        if override.get("tts_model_key"):
            segment["tts_model_key"] = override["tts_model_key"]
    elif segment_index is not None:
        segment = _speaker_voice_overlay(segments, segment_index, segment)

    if default_tts_model and not str(segment.get("tts_model_key") or "").strip():
        segment = {**segment, "tts_model_key": str(default_tts_model).strip()}

    draft_path = draft_audio_path(project_dir, segment_id)
    draft_path.parent.mkdir(parents=True, exist_ok=True)

    # Mark loading before the (potentially slow / paid) call so concurrent
    # polls see it. Even if tts_caller crashes the Python process, the
    # status file on disk will still reflect "loading" and the next UI
    # render will prompt the user to retry.
    mark_segment_status(project_dir, segment_id, SEGMENT_STATUS_TTS_LOADING)

    try:
        caller(segment, draft_path)
    except Exception as exc:
        mark_segment_status(project_dir, segment_id, SEGMENT_STATUS_TTS_FAILED)
        logger.info(
            "regenerate_segment_tts: caller raised for segment_id=%s: %s",
            segment_id,
            exc,
        )
        raise

    if not draft_path.is_file():
        mark_segment_status(project_dir, segment_id, SEGMENT_STATUS_TTS_FAILED)
        raise EditingConflictError(
            f"TTS caller returned without writing output at {draft_path}"
        )

    slot_duration_ms = _segment_slot_duration_ms(segment)
    fit_result = fit_audio_to_slot(draft_path, slot_duration_ms=slot_duration_ms)
    mark_segment_status(project_dir, segment_id, SEGMENT_STATUS_TTS_DIRTY)
    size_bytes = draft_path.stat().st_size
    return {
        "segment_id": segment_id,
        "draft_audio_path": str(draft_path),
        "size_bytes": size_bytes,
        "provider": segment.get("tts_provider") or segment.get("provider"),
        "voice_id": segment.get("voice_id"),
        "model": segment.get("tts_model_key"),
        "target_duration_ms": _segment_slot_duration_ms(segment),
        "duration_fit": (
            {
                "initial_duration_ms": fit_result.initial_duration_ms,
                "final_duration_ms": fit_result.final_duration_ms,
                "slot_duration_ms": slot_duration_ms,
                "speed_ratio_used": fit_result.speed_ratio_used,
                "silence_padded_ms": fit_result.silence_padded_ms,
                "truncated_ms": fit_result.truncated_ms,
            }
            if fit_result is not None
            else None
        ),
        "segment_status": load_segment_status(project_dir),
    }


# ---------------------------------------------------------------------------
# Accept / discard
# ---------------------------------------------------------------------------


def accept_draft_tts(
    project_dir: str | Path,
    segment_id: str,
) -> dict[str, Any]:
    """User clicked "接受". Keep draft file; clear dirty status.

    The draft wav is deliberately NOT moved to baseline here — baseline
    stays untouched throughout editing (plan §3.5 invariant). Commit
    (T1-9) is the only operation that promotes drafts to baseline.
    """
    validate_segment_id(segment_id)
    draft = draft_audio_path(project_dir, segment_id)
    if not draft.is_file():
        raise EditingConflictError(
            f"no draft audio to accept for segment_id {segment_id!r}; "
            "nothing was re-generated"
        )
    status_map = mark_segment_status(
        project_dir, segment_id, SEGMENT_STATUS_ACCEPTED
    )
    return {
        "segment_id": segment_id,
        "action": "accepted",
        "draft_audio_path": str(draft),
        "segment_status": status_map,
    }


def discard_draft_tts(
    project_dir: str | Path,
    segment_id: str,
) -> dict[str, Any]:
    """User clicked "丢弃". Delete the draft file; demote segment_status
    to the correct residual dirty state.

    After this, the segment's effective audio is the baseline
    ``editor/tts_segments/{sid}.wav`` (if present) UNLESS a residual
    dirt source (text edit / voice override) still demands re-TTS —
    in that case batch re-TTS will pick the segment up on the next
    run. Unconditional ``accepted`` here would silently hide those
    dirty sources and ship stale audio (Claude Code ultrareview #3 /
    CodeX P1).

    Idempotent — running twice on a missing draft does NOT raise (the
    admin force-cancel / idle-cancel paths need the same cleanup shape)."""
    validate_segment_id(segment_id)
    draft = draft_audio_path(project_dir, segment_id)
    if draft.exists():
        try:
            draft.unlink()
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning(
                "discard_draft_tts: failed to remove %s: %s", draft, exc
            )
            raise
    residual = compute_residual_segment_status(
        project_dir, segment_id, assume_no_draft=True,
    )
    status_map = mark_segment_status(project_dir, segment_id, residual)
    return {
        "segment_id": segment_id,
        "action": "discarded",
        "segment_status": status_map,
    }
