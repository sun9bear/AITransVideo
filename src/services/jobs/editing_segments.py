"""Segment CRUD for the editing buffer (plan §3.5 / T1-2).

Provides read + patch helpers that operate on ``project_dir/editor/editing/``
files:

- ``editing/segments.json`` — editable copy of the baseline segments list.
  Snapshotted by ``editing.enter_editing`` at enter time.
- ``editing/segment_status.json`` — per-segment state map
  (``text_dirty`` / ``tts_dirty`` / ``voice_dirty`` / ``accepted``).
  Lazy-created on first mutation; absent → all segments implicit ``accepted``.

All functions assume the caller has already verified the job is in the
``editing`` status and owns a valid ``project_dir``; state-machine checks
live in ``editing.py``.

Schema philosophy: the segment list in ``editing/segments.json`` is a
list of dicts with at minimum ``segment_id`` + ``cn_text``. Other fields
(speaker_id, start_ms, end_ms, voice_id, alignment_method, etc.) pass
through unchanged. We deliberately do NOT impose a strict schema here —
upstream pipeline (``src/modules/alignment/...``) owns the authoritative
shape, and forcing it on the editing layer would couple the two modules.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from services.jobs.editing import EDITING_SUBDIR, EditingConflictError
from services.jobs.input_validators import validate_segment_id

logger = logging.getLogger(__name__)

__all__ = [
    "SEGMENT_STATUS_ACCEPTED",
    "SEGMENT_STATUS_TEXT_DIRTY",
    "SEGMENT_STATUS_TTS_DIRTY",
    "SEGMENT_STATUS_TTS_FAILED",
    "SEGMENT_STATUS_VOICE_DIRTY",
    "SEGMENT_STATUS_TTS_LOADING",
    "SUPPORTED_SEGMENT_STATUSES",
    "PATCHABLE_SEGMENT_FIELDS",
    "compute_residual_segment_status",
    "load_editing_segments",
    "load_segment_status",
    "patch_editing_segment",
    "mark_segment_status",
    "editing_payload",
]

# Segment status vocabulary. Frontend renders one-to-one against this set.
SEGMENT_STATUS_ACCEPTED = "accepted"
SEGMENT_STATUS_TEXT_DIRTY = "text_dirty"      # text changed; TTS out of date
SEGMENT_STATUS_TTS_LOADING = "tts_loading"    # single-segment re-TTS in flight
SEGMENT_STATUS_TTS_DIRTY = "tts_dirty"        # new draft TTS, awaiting user accept
SEGMENT_STATUS_TTS_FAILED = "tts_failed"      # re-TTS attempt failed
SEGMENT_STATUS_VOICE_DIRTY = "voice_dirty"    # voice_id changed; TTS out of date

SUPPORTED_SEGMENT_STATUSES: frozenset[str] = frozenset({
    SEGMENT_STATUS_ACCEPTED,
    SEGMENT_STATUS_TEXT_DIRTY,
    SEGMENT_STATUS_TTS_LOADING,
    SEGMENT_STATUS_TTS_DIRTY,
    SEGMENT_STATUS_TTS_FAILED,
    SEGMENT_STATUS_VOICE_DIRTY,
})

# Fields that PATCH /jobs/{id}/segments/{sid} is allowed to mutate. Anything
# else in the PATCH body is silently ignored. This is the allowlist form of
# field validation — safer than a denylist (future upstream schema additions
# don't accidentally become PATCH-editable).
PATCHABLE_SEGMENT_FIELDS: frozenset[str] = frozenset({
    "cn_text",
    "translation_confirmed",
    "rewrite_requested",
    # 2026-04-20: speaker_id is patchable so users can fix S2-era
    # misclassification ("this segment is actually speaker_b, not
    # speaker_a"). The handler has extra coupling below: it propagates
    # voice_id + tts_provider from the new speaker's baseline and clears
    # any stale voice_map override on this segment.
    "speaker_id",
    # voice_id changes go through the voice_map.json helper (T1-6), NOT this
    # patch path, so ``voice_id`` is intentionally excluded here.
})


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _editing_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / EDITING_SUBDIR


def _segments_path(project_dir: str | Path) -> Path:
    return _editing_dir(project_dir) / "segments.json"


def _segment_status_path(project_dir: str | Path) -> Path:
    return _editing_dir(project_dir) / "segment_status.json"


def _ensure_editing_dir(project_dir: str | Path) -> Path:
    d = _editing_dir(project_dir)
    if not d.is_dir():
        raise EditingConflictError(
            f"editing dir does not exist: {d}; call enter_editing first"
        )
    return d


def _atomic_write_json(path: Path, payload: object) -> None:
    """Write JSON via temp file + os.replace so readers never see half-written
    content (important because the cleanup scanner also reads segments.json)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with open(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        # os.replace is atomic on both POSIX and Windows (ReplaceFileW).
        tmp_path.replace(path)
    except Exception:
        # Best-effort cleanup of the temp file if swap failed.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def load_editing_segments(project_dir: str | Path) -> list[dict[str, Any]]:
    """Return the editing/segments.json list. Empty list if file missing
    (which can happen if enter_editing ran on a project without a baseline)."""
    path = _segments_path(project_dir)
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise EditingConflictError(
            f"editing/segments.json is not a list (got {type(data).__name__}); "
            "refusing to proceed"
        )
    return data


def load_segment_status(project_dir: str | Path) -> dict[str, str]:
    """Return the segment_status map. Missing file → {} (all accepted)."""
    path = _segment_status_path(project_dir)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise EditingConflictError(
            f"editing/segment_status.json is not an object (got {type(data).__name__})"
        )
    return {str(k): str(v) for k, v in data.items()}


def editing_payload(project_dir: str | Path) -> dict[str, Any]:
    """Bundle segments + status map for GET /jobs/{id}/editing/segments.

    Frontend consumes this as-is:
      { "segments": [...], "segment_status": {...}, "total": N }

    Each segment dict is augmented with ``draft_wav_duration_ms`` when a
    ``editor/editing/tts_segments_draft/{sid}.wav`` file exists on disk.
    This lets the frontend compute slot-mismatch warnings (D44): if the
    draft's actual duration is far from ``target_duration_ms``, γ
    publish will DSP-stretch at an extreme ratio and audio quality
    degrades. Surfacing the ratio at edit time lets the user re-phrase
    the translation before committing.

    Segments without a draft omit the field entirely — the baseline
    ``editor/tts_segments/{sid}.wav`` was aligned to slot by the prior
    publish and always matches target within tolerance.
    """
    segments = load_editing_segments(project_dir)
    status = load_segment_status(project_dir)
    augmented = _augment_with_draft_wav_duration(project_dir, segments)
    return {
        "segments": augmented,
        "segment_status": status,
        "total": len(augmented),
    }


def _augment_with_draft_wav_duration(
    project_dir: str | Path,
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add ``draft_wav_duration_ms`` to any segment whose draft wav exists
    and is readable by pydub. Silently skips missing / corrupted wavs
    (field stays absent — frontend treats absent as "no draft on disk")."""
    draft_dir = Path(project_dir) / EDITING_SUBDIR_NAME / "tts_segments_draft"
    if not draft_dir.is_dir():
        return [dict(s) if isinstance(s, dict) else s for s in segments]

    # Deferred pydub import — only pay the cost when segments exist AND
    # a draft dir is present. Keeps the common (no-draft) path fast.
    try:
        from pydub import AudioSegment  # type: ignore
    except ImportError:  # pragma: no cover — pydub is a project dep
        return [dict(s) if isinstance(s, dict) else s for s in segments]

    augmented: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            augmented.append(seg)
            continue
        out = dict(seg)
        sid = str(out.get("segment_id") or "")
        if sid:
            draft_wav = draft_dir / f"{sid}.wav"
            if draft_wav.is_file():
                try:
                    out["draft_wav_duration_ms"] = len(
                        AudioSegment.from_wav(draft_wav)
                    )
                except Exception:
                    # Corrupted / not-a-wav — skip rather than raise.
                    # The editing page still renders; frontend treats
                    # absent field as "no valid draft duration yet".
                    pass
        augmented.append(out)
    return augmented


# Small constant used by _augment_with_draft_wav_duration — kept as a
# string so tests and the helper share the same path fragment.
EDITING_SUBDIR_NAME: str = "editor/editing"


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def patch_editing_segment(
    project_dir: str | Path,
    segment_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Mutate one segment in editing/segments.json. Returns the updated
    segment dict.

    Only ``PATCHABLE_SEGMENT_FIELDS`` are honoured; other keys are
    silently dropped. ``segment_id`` is re-validated here defensively even
    though the HTTP layer already did it — belt-and-braces against
    internal callers that forget.

    On success the segment_status for ``segment_id`` is set to
    ``text_dirty`` (since TTS now needs re-synthesis). Callers that want a
    different status (e.g. ``accepted`` after user accepts a draft TTS)
    should use ``mark_segment_status`` directly.
    """
    validate_segment_id(segment_id)
    _ensure_editing_dir(project_dir)

    segments = load_editing_segments(project_dir)

    # Locate the segment by its segment_id field. Tolerate legacy payloads
    # where segment_id was written as int (e.g. translation/segments.json
    # carries integer ids that early lazy-seed snapshots preserved verbatim).
    # HTTP callers always send strings, so str-cast both sides for comparison.
    index = None
    target = str(segment_id)
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        if str(seg.get("segment_id")) == target:
            index = i
            break
    if index is None:
        raise EditingConflictError(
            f"segment_id {segment_id!r} not found in editing/segments.json"
        )

    updated = dict(segments[index])  # shallow copy
    applied: dict[str, Any] = {}
    for key, value in patch.items():
        if key not in PATCHABLE_SEGMENT_FIELDS:
            continue
        # Minimal normalisation — text fields get str() + strip to avoid
        # accidental whitespace-only "edits".
        if key in {"cn_text"}:
            if value is None:
                continue
            value = str(value)
        elif key in {"translation_confirmed", "rewrite_requested"}:
            value = bool(value)
        elif key == "speaker_id":
            # Speaker reassignment has extra coupling handled below
            # (voice_id propagation + voice_map override clearing).
            # Reject unknown speakers BEFORE writing any state so a bad
            # request doesn't half-update the segment.
            new_speaker_id = str(value).strip()
            if not new_speaker_id:
                continue
            value = new_speaker_id
        updated[key] = value
        applied[key] = value

    if not applied:
        raise ValueError(
            "patch body contained no patchable fields; allowed: "
            f"{sorted(PATCHABLE_SEGMENT_FIELDS)}"
        )

    # Speaker reassignment side-effects — computed BEFORE persisting the
    # patched segments list so failure here leaves disk untouched.
    speaker_changed = (
        "speaker_id" in applied
        and applied["speaker_id"] != segments[index].get("speaker_id")
    )
    if speaker_changed:
        _propagate_speaker_change(
            segments=segments,
            index=index,
            updated=updated,
            new_speaker_id=str(applied["speaker_id"]),
        )
    # Speaker set to same value → silent no-op drop so we don't flip status
    elif "speaker_id" in applied and len(applied) == 1:
        return segments[index]  # nothing actually changed

    segments[index] = updated
    _atomic_write_json(_segments_path(project_dir), segments)

    # Status flip. cn_text edit → text_dirty; speaker change → voice_dirty
    # (different voice coming, audio is stale). Both may apply — prefer
    # voice_dirty when speaker changed since it's a stronger signal and
    # the batch re-TTS trigger list catches both.
    if speaker_changed:
        # Clear any stale voice_map override — it was tied to the old
        # speaker's voice pick. Import late to avoid module cycle.
        from services.jobs.editing_voice_map import (
            clear_voice_override,
            load_voice_map,
        )
        if segment_id in load_voice_map(project_dir):
            clear_voice_override(project_dir, segment_id)
        mark_segment_status(project_dir, segment_id, SEGMENT_STATUS_VOICE_DIRTY)
    elif "cn_text" in applied:
        mark_segment_status(project_dir, segment_id, SEGMENT_STATUS_TEXT_DIRTY)

    return updated


def _propagate_speaker_change(
    *,
    segments: list[dict[str, Any]],
    index: int,
    updated: dict[str, Any],
    new_speaker_id: str,
) -> None:
    """Mutate ``updated`` in-place with the new speaker's baseline voice.

    Rules:
    - The new speaker_id MUST already appear in ``segments`` (at least
      one other segment uses it). This keeps the allowed-speakers set
      tied to the task's actual speaker universe — no implicit speaker
      creation, no upstream coordination with the pipeline's speaker
      registry.
    - Copy ``voice_id`` + ``tts_provider`` from the first other segment
      of the new speaker. This makes re-synth automatically use the
      new speaker's voice without the user also having to touch the
      voice Tab.
    - Preserve other baseline fields (cn_text / timing / etc.) on the
      edited segment itself.
    """
    # Build universe of existing speakers → representative seg voice info
    rep_voice_id: str | None = None
    rep_tts_provider: str | None = None
    known_speakers: set[str] = set()
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        sid = seg.get("speaker_id")
        if not isinstance(sid, str):
            continue
        known_speakers.add(sid)
        if sid == new_speaker_id and i != index and rep_voice_id is None:
            vid = seg.get("voice_id")
            prov = seg.get("tts_provider") or seg.get("provider")
            if isinstance(vid, str) and vid:
                rep_voice_id = vid
            if isinstance(prov, str) and prov:
                rep_tts_provider = prov

    if new_speaker_id not in known_speakers:
        raise ValueError(
            f"speaker {new_speaker_id!r} not found in task; known: "
            f"{sorted(known_speakers)}. "
            "Cannot reassign to an unknown speaker — no implicit creation."
        )

    # Propagate voice if we found a rep. (Edge case: the speaker exists
    # but only on the segment being edited, or all same-speaker segments
    # lack voice_id — leave voice fields as-is; batch re-TTS + voice
    # Tab's overlay will sort it out.)
    if rep_voice_id:
        updated["voice_id"] = rep_voice_id
    if rep_tts_provider:
        updated["tts_provider"] = rep_tts_provider
        # Scrub the legacy key to avoid drift between the two names.
        updated.pop("provider", None)


def mark_segment_status(
    project_dir: str | Path,
    segment_id: str,
    status: str,
) -> dict[str, str]:
    """Set ``segment_status[segment_id] = status``. Returns the full map.

    Setting ``status = SEGMENT_STATUS_ACCEPTED`` clears the entry from the
    map so that ``load_segment_status`` returns the minimal form (implicit
    accepted for all segments not in the map).
    """
    validate_segment_id(segment_id)
    if status not in SUPPORTED_SEGMENT_STATUSES:
        raise ValueError(
            f"unsupported segment status: {status!r}; "
            f"must be one of {sorted(SUPPORTED_SEGMENT_STATUSES)}"
        )
    _ensure_editing_dir(project_dir)
    current = load_segment_status(project_dir)
    if status == SEGMENT_STATUS_ACCEPTED:
        current.pop(segment_id, None)
    else:
        current[segment_id] = status
    _atomic_write_json(_segment_status_path(project_dir), current)
    return current


def _cn_text_differs_from_baseline(
    project_dir: str | Path, segment_id: str,
) -> bool:
    """Does ``editing/segments.json[segment_id].cn_text`` differ from the
    baseline ``editor/segments.json`` snapshot?

    Returns False if either file is missing or the segment is absent in
    either — conservative default (under-flag rather than over-flag).
    """
    baseline = Path(project_dir) / "editor" / "segments.json"
    editing = _segments_path(project_dir)
    if not baseline.is_file() or not editing.is_file():
        return False
    try:
        base = json.loads(baseline.read_text(encoding="utf-8"))
        edit = json.loads(editing.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(base, list) or not isinstance(edit, list):
        return False
    target = str(segment_id)
    base_text: str | None = None
    edit_text: str | None = None
    for s in base:
        if isinstance(s, dict) and str(s.get("segment_id")) == target:
            base_text = str(s.get("cn_text") or "")
            break
    for s in edit:
        if isinstance(s, dict) and str(s.get("segment_id")) == target:
            edit_text = str(s.get("cn_text") or "")
            break
    if base_text is None or edit_text is None:
        return False
    return base_text != edit_text


def compute_residual_segment_status(
    project_dir: str | Path,
    segment_id: str,
    *,
    assume_no_draft: bool = False,
    assume_no_voice_override: bool = False,
) -> str:
    """Compute the segment_status a caller should settle on when one dirty
    source is being removed.

    Demoting a single-slot status blindly to ``accepted`` loses signal —
    e.g. user edits cn_text (text_dirty), then changes voice (voice_dirty
    clobbers), then clears voice: naive ``mark_segment_status(... accepted)``
    leaves baseline audio running against an edited text. This helper
    inspects the other possible dirty sources and picks the strongest
    one still in effect.

    ``assume_no_draft`` / ``assume_no_voice_override``: callers that have
    just removed one of these sources pass True so the filesystem probe
    for that source is skipped — the on-disk JSON may not have flushed
    yet, and re-probing the state we just wrote would race.

    Precedence (first active dirt wins):
      tts_dirty   — draft wav exists on disk
      voice_dirty — voice_map has an override for this segment
      text_dirty  — editing cn_text differs from baseline
      accepted    — none of the above
    """
    if not assume_no_draft:
        draft_path = (
            Path(project_dir) / EDITING_SUBDIR / "tts_segments_draft"
            / f"{segment_id}.wav"
        )
        if draft_path.is_file():
            return SEGMENT_STATUS_TTS_DIRTY
    if not assume_no_voice_override:
        # Late import to avoid editing_segments ↔ editing_voice_map cycle.
        from services.jobs.editing_voice_map import load_voice_map
        if segment_id in load_voice_map(project_dir):
            return SEGMENT_STATUS_VOICE_DIRTY
    if _cn_text_differs_from_baseline(project_dir, segment_id):
        return SEGMENT_STATUS_TEXT_DIRTY
    return SEGMENT_STATUS_ACCEPTED
