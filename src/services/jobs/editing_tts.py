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
  the draft, mark ``accepted`` (the segment now falls back to the baseline
  ``tts_segments/{sid}.wav`` audio).

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
    target = str(segment_id)
    for s in segments:
        if isinstance(s, dict) and str(s.get("segment_id")) == target:
            segment = s
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

    mark_segment_status(project_dir, segment_id, SEGMENT_STATUS_TTS_DIRTY)
    size_bytes = draft_path.stat().st_size
    return {
        "segment_id": segment_id,
        "draft_audio_path": str(draft_path),
        "size_bytes": size_bytes,
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
