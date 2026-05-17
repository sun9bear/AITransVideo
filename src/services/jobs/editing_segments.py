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
import math
import tempfile
from pathlib import Path
from typing import Any

from services._file_lock import file_lock
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
    "EditingCorruptionError",
    "compute_residual_segment_status",
    "load_editing_segments",
    "load_segment_status",
    "load_segment_word_context",
    "patch_editing_segment",
    "revert_text_changes_to_audio_baseline",
    "split_editing_segment",
    "split_editing_segment_many",
    "slice_source_audio_for_editing_segment",
    "mark_segment_status",
    "editing_payload",
    "cache_preview_source_wav",
    "preview_cache_path",
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

# Warning-only guidance for post-edit re-TTS. The editor should not block
# long text: the user may deliberately force synthesis, and publish will
# DSP-fit the resulting wav to the original slot. These bounds only estimate
# when the text is likely to need audible time-warping after synthesis.
TTS_LENGTH_GUIDANCE_MIN_FACTOR = 0.85
TTS_LENGTH_GUIDANCE_MAX_FACTOR = 1.15
TTS_LENGTH_GUIDANCE_WARNING_MIN_RATIO = 0.70
TTS_LENGTH_GUIDANCE_WARNING_MAX_RATIO = 1.30
TTS_LENGTH_GUIDANCE_SEVERE_MIN_RATIO = 0.50
TTS_LENGTH_GUIDANCE_SEVERE_MAX_RATIO = 1.50
DEFAULT_TTS_LENGTH_GUIDANCE_CPS = 4.5
MIN_REASONABLE_TTS_CPS = 2.0
MAX_REASONABLE_TTS_CPS = 8.0

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
    # 2026-04-21: source_text editable so users can correct upstream S1
    # ASR mistakes. No auto-retranslate — user is responsible for also
    # updating cn_text before re-TTS. Symmetric with cn_text: modifies the
    # segment text and marks ``text_dirty`` so the next re-TTS picks up
    # the new content.
    "source_text",
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


def _editing_lock_anchor(project_dir: str | Path) -> Path:
    """Return the shared anchor used as the file_lock target for ALL
    mutating operations on the editing-state files (segments.json,
    segment_status.json, voice_map.json).

    P0-5 (audit 2026-05-07): Job API is multi-threaded
    (ThreadingHTTPServer), and editing-state writes from concurrent HTTP
    requests previously raced — last-write-wins on the whole JSON dict,
    silently losing the earlier user edit. We use ONE shared lock for all
    three editing files because:

    * They cross-reference each other (set_voice_override calls
      mark_segment_status; revert_text_changes_to_audio_baseline mutates
      segments + status atomically). Per-file locks would either deadlock
      or leave gaps where two halves of an "atomic" update interleave.
    * services/_file_lock.py is reentrant (threading.RLock + per-thread
      depth counter), so nested calls within a single thread are free.
    * Lock granularity per project_dir is fine — concurrent edits across
      different projects don't contend on this lock.

    Anchor is segments.json because it's the largest / most-read file in
    the directory; the .lock sidecar lives next to it. Anchor file does
    not need to exist — file_lock.touch() creates the sidecar lazily.
    """
    return _segments_path(project_dir)


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
    # Phase 2a: reconcile any pending split journals before reading. This
    # is idempotent + cheap (no-op when no journal). See plan §5.6
    # write-ahead journal recovery (state A/B/C).
    _reconcile_split_journal_if_needed(project_dir)
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


def load_editing_segments_for_audit(
    project_dir: str | Path, segment_id: str
) -> dict[str, Any] | None:
    """Read-only lookup of one segment by id. Returns None if not found.

    Used by the user-edit audit chokepoint to capture pre-mutation state
    so the resulting event can carry an honest before/after diff. Tolerant
    of missing files / malformed shape — audit must never block the main
    mutation path."""
    try:
        for seg in load_editing_segments(project_dir):
            if isinstance(seg, dict) and str(seg.get("segment_id") or "") == str(segment_id):
                # Return a shallow copy so the caller can compare safely
                # against the post-mutation segment without aliasing.
                return dict(seg)
    except Exception:  # noqa: BLE001
        return None
    return None


def load_segment_status(project_dir: str | Path) -> dict[str, str]:
    """Return the segment_status map. Missing file → {} (all accepted)."""
    # Phase 2a: reconcile any pending split journals before reading.
    _reconcile_split_journal_if_needed(project_dir)
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
    augmented = _augment_with_tts_length_guidance(segments)
    augmented = _augment_with_draft_wav_duration(project_dir, augmented)
    return {
        "segments": augmented,
        "segment_status": status,
        "total": len(augmented),
    }


def _augment_with_tts_length_guidance(
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach warning-only length guidance for pre-synthesis editing.

    The guidance is deterministic and segment-local: prefer explicit voice
    speed metadata if present, then observed first-pass speed, then a
    conservative default. No external service or gateway lookup is needed
    for the read path.
    """
    augmented: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            augmented.append(seg)
            continue
        out = dict(seg)
        out["tts_length_guidance"] = _build_tts_length_guidance(out)
        augmented.append(out)
    return augmented


def _build_tts_length_guidance(segment: dict[str, Any]) -> dict[str, Any]:
    current_chars = _tts_text_char_count(segment.get("cn_text"))
    target_duration_ms = _segment_target_duration_ms(segment)
    cps, cps_source = _segment_chars_per_second(segment)

    guidance: dict[str, Any] = {
        "current_chars": current_chars,
        "target_duration_ms": target_duration_ms,
        "chars_per_second": round(cps, 3) if cps is not None else None,
        "chars_per_second_source": cps_source,
        "suggested_target_chars": None,
        "suggested_min_chars": None,
        "suggested_max_chars": None,
        "estimated_duration_ms": None,
        "estimated_ratio": None,
        "severity": "unknown",
    }
    if target_duration_ms <= 0 or cps is None or cps <= 0:
        return guidance

    target_seconds = target_duration_ms / 1000
    suggested_target = max(1, int(round(target_seconds * cps)))
    suggested_min = max(
        1, int(math.floor(suggested_target * TTS_LENGTH_GUIDANCE_MIN_FACTOR))
    )
    suggested_max = max(
        suggested_min,
        int(math.ceil(suggested_target * TTS_LENGTH_GUIDANCE_MAX_FACTOR)),
    )
    estimated_duration_ms = (
        int(round((current_chars / cps) * 1000)) if current_chars > 0 else 0
    )
    ratio = estimated_duration_ms / target_duration_ms

    guidance.update({
        "suggested_target_chars": suggested_target,
        "suggested_min_chars": suggested_min,
        "suggested_max_chars": suggested_max,
        "estimated_duration_ms": estimated_duration_ms,
        "estimated_ratio": round(ratio, 3),
        "severity": _tts_length_guidance_severity(ratio),
    })
    return guidance


def _tts_length_guidance_severity(ratio: float) -> str:
    if (
        ratio <= TTS_LENGTH_GUIDANCE_SEVERE_MIN_RATIO
        or ratio >= TTS_LENGTH_GUIDANCE_SEVERE_MAX_RATIO
    ):
        return "severe"
    if (
        ratio < TTS_LENGTH_GUIDANCE_WARNING_MIN_RATIO
        or ratio > TTS_LENGTH_GUIDANCE_WARNING_MAX_RATIO
    ):
        return "warning"
    if (
        ratio < TTS_LENGTH_GUIDANCE_MIN_FACTOR
        or ratio > TTS_LENGTH_GUIDANCE_MAX_FACTOR
    ):
        return "mild"
    return "ok"


def _tts_text_char_count(value: Any) -> int:
    return len(str(value or "").strip())


def _segment_target_duration_ms(segment: dict[str, Any]) -> int:
    for key in ("target_duration_ms", "duration_target_ms"):
        value = _coerce_positive_number(segment.get(key))
        if value is not None:
            return int(round(value))
    start = _coerce_positive_number(segment.get("start_ms"), allow_zero=True)
    end = _coerce_positive_number(segment.get("end_ms"), allow_zero=True)
    if start is not None and end is not None and end > start:
        return int(round(end - start))
    return 0


def _segment_chars_per_second(segment: dict[str, Any]) -> tuple[float | None, str]:
    explicit = _explicit_segment_cps(segment)
    if explicit is not None:
        return explicit, "segment"

    first_pass_text = str(segment.get("first_pass_cn_text") or "").strip()
    first_pass_duration_ms = _coerce_positive_number(
        segment.get("first_pass_duration_ms")
    )
    if first_pass_text and first_pass_duration_ms is not None:
        observed = len(first_pass_text) / (first_pass_duration_ms / 1000)
        if _is_reasonable_cps(observed):
            return observed, "observed_first_pass"

    return DEFAULT_TTS_LENGTH_GUIDANCE_CPS, "default"


def _explicit_segment_cps(segment: dict[str, Any]) -> float | None:
    for key in (
        "voice_chars_per_second",
        "chars_per_second",
        "voice_cps",
        "calibrated_chars_per_second",
    ):
        value = _coerce_positive_number(segment.get(key))
        if value is not None and _is_reasonable_cps(value):
            return float(value)

    by_model = segment.get("chars_per_second_by_model")
    if not isinstance(by_model, dict):
        return None
    model_key = str(
        segment.get("tts_model_key")
        or segment.get("tts_model")
        or segment.get("model")
        or ""
    )
    candidates: list[Any] = []
    if model_key:
        candidates.append(by_model.get(model_key))
    candidates.extend(by_model.values())
    for candidate in candidates:
        value = _coerce_positive_number(candidate)
        if value is not None and _is_reasonable_cps(value):
            return float(value)
    return None


def _coerce_positive_number(value: Any, *, allow_zero: bool = False) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if allow_zero and numeric >= 0:
        return numeric
    if numeric > 0:
        return numeric
    return None


def _is_reasonable_cps(value: float) -> bool:
    return MIN_REASONABLE_TTS_CPS <= value <= MAX_REASONABLE_TTS_CPS


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

    # P0-5 (audit 2026-05-07): wrap the entire load → modify → save → side
    # effects sequence in the shared editing lock so concurrent HTTP
    # threads cannot read the same segments.json, both mutate their target
    # index, and last-write-wins. Nested helpers (mark_segment_status /
    # clear_voice_override) reuse the same anchor and are reentrant.
    with file_lock(_editing_lock_anchor(project_dir)):
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

        original = segments[index]
        updated = dict(original)  # shallow copy
        applied: dict[str, Any] = {}
        for key, value in patch.items():
            if key not in PATCHABLE_SEGMENT_FIELDS:
                continue
            # Minimal normalisation — text fields get str() + strip to avoid
            # accidental whitespace-only "edits".
            if key in {"cn_text", "source_text"}:
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
                project_dir=project_dir,
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

        # A cn_text edit invalidates any earlier draft wav for this segment.
        # Commit promotes every draft file it sees, so leaving a stale draft on
        # disk would let old synthesized content overwrite the baseline audio.
        if "cn_text" in applied and applied["cn_text"] != original.get("cn_text"):
            draft = _editing_dir(project_dir) / "tts_segments_draft" / f"{segment_id}.wav"
            if draft.exists():
                draft.unlink()

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
        elif "cn_text" in applied or "source_text" in applied:
            # Either text field edit means the current TTS is for stale content.
            mark_segment_status(project_dir, segment_id, SEGMENT_STATUS_TEXT_DIRTY)

        # Task 5 (plan 2026-05-09): when the user reassigns a segment to an
        # editing-mode speaker that was created without segments, this is
        # the point where Pass 3-style voice profile inference becomes
        # possible — the speaker now has at least one audio range to
        # sample. ``maybe_trigger_inference`` is idempotent (only fires if
        # profile_status == 'pending_segments') and fully non-blocking, so
        # repeat PATCHes of the same segment cost nothing extra. Wrapped
        # in try/except so an inference-side bug never blocks a successful
        # segment patch.
        if speaker_changed:
            try:
                from services.jobs.editing_voice_profile import (
                    maybe_trigger_inference,
                )
                maybe_trigger_inference(project_dir, str(applied["speaker_id"]))
            except Exception:
                logger.exception(
                    "maybe_trigger_inference failed; continuing"
                )

        return updated


def _propagate_speaker_change(
    *,
    project_dir: str | Path,
    segments: list[dict[str, Any]],
    index: int,
    updated: dict[str, Any],
    new_speaker_id: str,
) -> None:
    """Mutate ``updated`` in-place with the new speaker's baseline voice.

    Rules:
    - The new speaker_id MUST be known to the task — either already
      present in ``segments`` (at least one other segment uses it) OR
      registered in ``editor/editing/speakers.json`` (a freshly created
      editing speaker that has no segments assigned yet). This keeps
      the allowed-speakers set tied to the task's known speaker
      universe — no implicit speaker creation.
    - Copy ``voice_id`` + ``tts_provider`` from the first other segment
      of the new speaker. This makes re-synth automatically use the
      new speaker's voice without the user also having to touch the
      voice Tab.
    - Preserve other baseline fields (cn_text / timing / etc.) on the
      edited segment itself.
    """
    # Build universe of existing speakers → representative seg voice info.
    # Scan same-speaker segments until BOTH voice_id and tts_provider are
    # filled — not just voice_id. CodeX nit 2026-04-20: if the first
    # same-speaker segment is legacy data (voice_id only, no tts_provider),
    # stopping at voice_id leaves rep_tts_provider=None, which then leaks
    # the old speaker's tts_provider through on write-back. The
    # `isinstance(...) and <value>` guards below prevent already-filled
    # fields from being clobbered by empty strings on later segments.
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
        if (
            sid == new_speaker_id
            and i != index
            and (rep_voice_id is None or rep_tts_provider is None)
        ):
            vid = seg.get("voice_id")
            prov = seg.get("tts_provider") or seg.get("provider")
            if rep_voice_id is None and isinstance(vid, str) and vid:
                rep_voice_id = vid
            if rep_tts_provider is None and isinstance(prov, str) and prov:
                rep_tts_provider = prov

    if new_speaker_id not in known_speakers:
        # 2026-05-09: editing/speakers.json may register a fresh speaker that
        # has no segments yet. Accept those IDs as the legitimate "first
        # segment assignment" path. Late import avoids a top-level cycle
        # (editing_speakers imports from this module).
        from services.jobs.editing_speakers import load_speakers
        editing_ids = {sp.speaker_id for sp in load_speakers(project_dir)}
        if new_speaker_id not in editing_ids:
            raise ValueError(
                f"speaker {new_speaker_id!r} not found in task or editing "
                f"speakers; known: {sorted(known_speakers | editing_ids)}. "
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
    # P0-5 (audit 2026-05-07): serialize concurrent status writes from
    # HTTP threads + voice_map / segments side-effects. Reentrant safe.
    with file_lock(_editing_lock_anchor(project_dir)):
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


def _baseline_segments_by_id(project_dir: str | Path) -> dict[str, dict[str, Any]]:
    path = Path(project_dir) / "editor" / "segments.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, list):
        return {}
    return {
        str(seg.get("segment_id")): seg
        for seg in data
        if isinstance(seg, dict) and seg.get("segment_id") is not None
    }


def revert_text_changes_to_audio_baseline(
    project_dir: str | Path,
    segment_ids: list[str],
) -> dict[str, Any]:
    """Restore edited text fields to the baseline that matches current audio.

    Used when commit preflight finds text edits without regenerated TTS and
    the user explicitly chooses to discard those text edits.
    """
    _ensure_editing_dir(project_dir)
    targets: list[str] = []
    seen: set[str] = set()
    for raw_sid in segment_ids:
        sid = str(raw_sid or "").strip()
        if not sid:
            continue
        validate_segment_id(sid)
        if sid not in seen:
            targets.append(sid)
            seen.add(sid)
    if not targets:
        raise ValueError("segment_ids must contain at least one segment id")

    # P0-5 (audit 2026-05-07): protect the segments + status pair from
    # interleaved writes. mark_segment_status reuses the same anchor and
    # is reentrant.
    with file_lock(_editing_lock_anchor(project_dir)):
        segments = load_editing_segments(project_dir)
        baseline_by_id = _baseline_segments_by_id(project_dir)
        target_set = set(targets)
        updated_segments: list[dict[str, Any]] = []
        reverted: list[str] = []
        for item in segments:
            if not isinstance(item, dict):
                updated_segments.append(item)
                continue
            sid = str(item.get("segment_id"))
            if sid not in target_set:
                updated_segments.append(item)
                continue
            baseline = baseline_by_id.get(sid)
            if not baseline:
                raise EditingConflictError(f"baseline segment {sid!r} not found")
            next_item = dict(item)
            next_item["cn_text"] = str(
                baseline.get("tts_input_cn_text") or baseline.get("cn_text") or ""
            )
            if "source_text" in baseline:
                next_item["source_text"] = baseline.get("source_text") or ""
            updated_segments.append(next_item)
            reverted.append(sid)

        missing = [sid for sid in targets if sid not in reverted]
        if missing:
            raise EditingConflictError(
                f"segment_id(s) not found in editing/segments.json: {missing}"
            )

        _atomic_write_json(_segments_path(project_dir), updated_segments)
        for sid in reverted:
            draft = _editing_dir(project_dir) / "tts_segments_draft" / f"{sid}.wav"
            if draft.exists():
                draft.unlink()
            residual = compute_residual_segment_status(
                project_dir,
                sid,
                assume_no_draft=True,
            )
            mark_segment_status(project_dir, sid, residual)

        return {
            "reverted_segment_ids": reverted,
            "segments": [seg for seg in updated_segments if isinstance(seg, dict)],
            "segment_status": load_segment_status(project_dir),
        }


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


# ---------------------------------------------------------------------------
# split_editing_segment (2026-04-21 / plan §7.4) — splits one segment into
# two at user-chosen character positions in the source text and cn_text.
# Mirrors ``web_ui/translation_review._split_segment`` in intent but
# operates on the editing-mode ``editor/editing/segments.json`` schema
# (different from translation-review's pre-alignment segments).
# ---------------------------------------------------------------------------


def _derive_split_ids(base_id: str, existing_ids: set[str]) -> tuple[str, str]:
    """Pick two unique segment_ids derived from ``base_id``.

    Scheme: ``<base>_a`` / ``<base>_b``; on collision (rare: user has
    already split this segment before) fall through to numeric suffixes.
    Segment_id format is ``^[a-z0-9_]{1,64}$`` (see
    input_validators.SEGMENT_ID_RE) so the suffix must match that pattern.
    """
    for suffix_a, suffix_b in (
        ("_a", "_b"),
        ("_split_a", "_split_b"),
    ):
        candidate_a = f"{base_id}{suffix_a}"
        candidate_b = f"{base_id}{suffix_b}"
        if candidate_a not in existing_ids and candidate_b not in existing_ids:
            return candidate_a, candidate_b
    # Numeric fallback — loops until free slots show up.
    n = 1
    while True:
        candidate_a = f"{base_id}_s{n}a"
        candidate_b = f"{base_id}_s{n}b"
        if candidate_a not in existing_ids and candidate_b not in existing_ids:
            return candidate_a, candidate_b
        n += 1


def _load_split_words_data(project_dir: str | Path) -> list[dict[str, Any]] | None:
    raw_path = Path(project_dir) / "transcript" / "raw_assemblyai.json"
    try:
        data = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    words = data.get("words") if isinstance(data, dict) else None
    if not isinstance(words, list):
        return None
    valid = [w for w in words if isinstance(w, dict)]
    return valid or None


def split_editing_segment(
    project_dir: str | Path,
    *,
    segment_id: str,
    split_source_index: int,
    split_cn_index: int,
    speaker_a: str,
    speaker_b: str,
) -> dict[str, Any]:
    """Replace one segment with two, both text-dirty.

    Args:
        project_dir: project root (contains ``editor/editing/``).
        segment_id: id of the segment being split.
        split_source_index: character index into ``source_text`` at which
            to cut. Must satisfy ``0 < split_source_index < len(source_text)``.
        split_cn_index: character index into ``cn_text``. Same bounds.
        speaker_a / speaker_b: speaker_id assigned to each half.

    Returns:
        ``{"replaced_segment_id": ..., "new_segments": [A_dict, B_dict],
           "total_count": N}``.

    Side-effects:
        - Writes ``editor/editing/segments.json`` atomically.
        - Marks both new segment_ids as ``text_dirty`` so the next
          re-TTS picks them up. The old segment's status entry (if any)
          is removed since the id no longer exists.
        - Preserves untouched segments' order and fields.
        - Does NOT touch baseline ``editor/segments.json`` — commit
          picks up editing/* as the authoritative copy.
    """
    validate_segment_id(segment_id)
    _ensure_editing_dir(project_dir)

    # P0-5 (audit 2026-05-07): split mutates segments + segment_status as
    # a single logical unit; without the lock another concurrent patch
    # could land between the segments.json write and the status writes.
    with file_lock(_editing_lock_anchor(project_dir)):
        segments = load_editing_segments(project_dir)

        index: int | None = None
        target = str(segment_id)
        for i, seg in enumerate(segments):
            if isinstance(seg, dict) and str(seg.get("segment_id")) == target:
                index = i
                break
        if index is None:
            raise EditingConflictError(
                f"segment_id {segment_id!r} not found in editing/segments.json"
            )

        original = dict(segments[index])
        source_text = str(original.get("source_text") or "")
        cn_text = str(original.get("cn_text") or "")

        if not (0 < split_source_index < len(source_text)):
            raise ValueError(
                f"split_source_index {split_source_index} produces an empty half; "
                f"must be in (0, {len(source_text)}) for source_text of length "
                f"{len(source_text)}"
            )
        if not (0 < split_cn_index < len(cn_text)):
            raise ValueError(
                f"split_cn_index {split_cn_index} produces an empty half; "
                f"must be in (0, {len(cn_text)}) for cn_text of length "
                f"{len(cn_text)}"
            )

        # Prefer the same word-timestamp split estimator used by the
        # transcript-review/main flow. Editing projects normally retain
        # transcript/raw_assemblyai.json, so this maps the source-text split
        # position to a word boundary when possible. If word data is absent
        # or unreliable, estimate_split_ms falls back to character ratio.
        start_ms = int(original.get("start_ms", 0) or 0)
        end_ms = int(original.get("end_ms", start_ms) or start_ms)
        from services.transcript_reviewer import estimate_split_ms

        mid_ms = estimate_split_ms(
            start_ms=start_ms,
            end_ms=end_ms,
            source_text=source_text,
            split_char_pos=split_source_index,
            words_data=_load_split_words_data(project_dir),
        )
        # P0-8 (audit 2026-05-07): refuse splits that would produce a
        # zero-duration half. This happens when (end_ms - start_ms) is very
        # small or when the computed split rounds to one edge; the resulting
        # half can break downstream alignment math (TTS would have to fit
        # text into 0 ms).
        # Better to reject the split up-front with a clear error than let
        # the user discover it minutes later at commit time.
        if mid_ms <= start_ms or mid_ms >= end_ms:
            raise ValueError(
                f"split would produce a zero-duration half: start_ms={start_ms}, "
                f"end_ms={end_ms}, mid_ms={mid_ms}. Segment is too short to split "
                f"or split position is too close to one end."
            )

        existing_ids = {
            str(s.get("segment_id"))
            for s in segments
            if isinstance(s, dict) and s.get("segment_id") is not None
        }
        # The segment we're about to replace is part of ``existing_ids`` but
        # its id slot will be freed; exclude it so we can still reuse ``base_a``.
        existing_ids.discard(target)
        new_id_a, new_id_b = _derive_split_ids(target, existing_ids)
        duration_a_ms = max(0, mid_ms - start_ms)
        duration_b_ms = max(0, end_ms - mid_ms)

        # Build the two new dicts inheriting everything from the original
        # except what we want to split/override. We keep pass-through fields
        # (alignment_method, voice_id, tts_provider, etc.) so the editing
        # state survives the split without losing upstream metadata.
        seg_a = dict(original)
        seg_a.update(
            segment_id=new_id_a,
            source_text=source_text[:split_source_index],
            cn_text=cn_text[:split_cn_index],
            speaker_id=str(speaker_a).strip() or original.get("speaker_id"),
            start_ms=start_ms,
            end_ms=mid_ms,
            target_duration_ms=duration_a_ms,
        )
        if "duration_target_ms" in seg_a:
            seg_a["duration_target_ms"] = duration_a_ms
        seg_b = dict(original)
        seg_b.update(
            segment_id=new_id_b,
            source_text=source_text[split_source_index:],
            cn_text=cn_text[split_cn_index:],
            speaker_id=str(speaker_b).strip() or original.get("speaker_id"),
            start_ms=mid_ms,
            end_ms=end_ms,
            target_duration_ms=duration_b_ms,
        )
        if "duration_target_ms" in seg_b:
            seg_b["duration_target_ms"] = duration_b_ms

        original_speaker = original.get("speaker_id")
        if seg_a.get("speaker_id") != original_speaker:
            _propagate_speaker_change(
                project_dir=project_dir,
                segments=segments,
                index=index,
                updated=seg_a,
                new_speaker_id=str(seg_a.get("speaker_id") or ""),
            )
        if seg_b.get("speaker_id") != original_speaker:
            _propagate_speaker_change(
                project_dir=project_dir,
                segments=segments,
                index=index,
                updated=seg_b,
                new_speaker_id=str(seg_b.get("speaker_id") or ""),
            )

        new_segments = list(segments)
        new_segments[index : index + 1] = [seg_a, seg_b]
        _atomic_write_json(_segments_path(project_dir), new_segments)

        # Status bookkeeping: both halves need re-TTS (old draft was sized
        # for the old full segment). Drop the old id's status entry since
        # that id no longer exists; a stale "accepted" entry would otherwise
        # linger until the next write flushed it.
        mark_segment_status(project_dir, new_id_a, SEGMENT_STATUS_TEXT_DIRTY)
        mark_segment_status(project_dir, new_id_b, SEGMENT_STATUS_TEXT_DIRTY)
        status = load_segment_status(project_dir)
        if target in status:
            status.pop(target, None)
            _atomic_write_json(_segment_status_path(project_dir), status)

        # P0-8 (audit 2026-05-07): migrate any voice_map override from the
        # old segment_id to BOTH new halves. Without this, a user who
        # explicitly picked voice X for seg_005 then split it would silently
        # see the override get dropped (load_voice_map at commit looks up
        # by segment_id; old key has no matching segment, gets ignored —
        # both halves fall back to the speaker default).
        # Late import avoids a circular dependency: editing_voice_map
        # imports from editing_segments at module load time, so we can't
        # do top-level. Module is already loaded by the time split runs.
        from services.jobs.editing_voice_map import (
            _voice_map_path,
            load_voice_map,
        )
        voice_map = load_voice_map(project_dir)
        if target in voice_map:
            old_override = dict(voice_map.pop(target))
            # Both halves inherit the same provider+voice_id. The user can
            # override either side individually afterwards via the voice
            # selection panel; in the common case they want both halves
            # voiced the same as the original.
            voice_map[new_id_a] = dict(old_override)
            voice_map[new_id_b] = dict(old_override)
            _atomic_write_json(_voice_map_path(project_dir), voice_map)

        # P1-16 (audit 2026-05-07, codex P0-8 review): the parent segment
        # may have left a draft wav at editor/editing/tts_segments_draft/
        # {parent_sid}.wav from a previous regenerate-tts. After split,
        # parent_sid no longer exists in the segments list, so the draft
        # is an orphan — commit's draft-promotion phase would copy it
        # back to editor/tts_segments/{parent_sid}.wav, leaving stale
        # audio for an id that has no corresponding segment entry. Clean
        # it up here so commit only sees drafts for live segment_ids.
        # Note: still inside the editing_lock anchor lock so this is
        # atomic with the segments.json write above.
        parent_draft = _editing_dir(project_dir) / "tts_segments_draft" / f"{target}.wav"
        if parent_draft.exists():
            parent_draft.unlink()

        return {
            "replaced_segment_id": target,
            "new_segments": [seg_a, seg_b],
            "total_count": len(new_segments),
        }


# ---------------------------------------------------------------------------
# split_editing_segment_many — N cuts → N+1 segments
#
# Input:  original segment + N cuts (source_idx, cn_idx) + N+1 speaker_ids
# Output: N+1 segments, ids = <base>_a, <base>_b, ... (collision: <base>_s1a)
#
# Atomicity (plan 2026-05-17 §5.6 + Codex round-6/7 P1 #1 fix):
#   write-ahead journal recovery with three-state reconcile.
#
#   ┌───────────┐  validate    ┌─────────┐  write    ┌──────────┐
#   │ baseline  │ ──cuts──→    │ journal │ ────→     │ 3 files  │
#   │ segments. │  ✗ → 422     │ written │  os.      │ replaced │
#   │ json + .. │  unchanged   │ atomic  │  replace  │  delete  │
#   └───────────┘              └─────────┘           │  journal │
#                                                    └──────────┘
#
#   On any failure between "journal written" and "delete journal":
#   the journal stays. Next call to load_editing_segments /
#   load_segment_status / load_voice_map triggers
#   _reconcile_split_journal_if_needed which classifies state:
#     - A: parent_sid still in segments.json  → apply journal (fresh)
#     - B: parent gone + all sub-segs present → backfill missing
#          status/voice_map entries; DO NOT overwrite user's later edits
#     - C: mixed (some sub-segs missing or partial) → EditingCorruptionError
#
# Time alignment: cuts must fall on word boundaries. estimate_split_ms
# snaps off-boundary positions to the nearest word end ms.
# ---------------------------------------------------------------------------


SPLIT_JOURNAL_SCHEMA_VERSION = 1


class EditingCorruptionError(EditingConflictError):
    """Raised by the split-journal reconciler when editor/editing/ is in
    a mixed/inconsistent state that cannot be auto-recovered (state C in
    plan §5.6). Operator must inspect before further edits.

    Subclass of EditingConflictError so api.py's 409 path catches it.
    """


def _split_journal_path(project_dir: str | Path, parent_sid: str) -> Path:
    """Path for the journal of a split keyed by parent segment_id.

    Parent_sid is already segment_id-validated by callers (regex
    ^[a-z0-9_]{1,64}$) so safe to embed in filename verbatim.
    """
    return _editing_dir(project_dir) / f".split_journal_{parent_sid}.json"


def _list_split_journals(project_dir: str | Path) -> list[Path]:
    editing_dir = _editing_dir(project_dir)
    if not editing_dir.is_dir():
        return []
    return sorted(editing_dir.glob(".split_journal_*.json"))


def _derive_split_ids_many(
    base_id: str,
    n: int,
    existing_ids: set[str],
) -> list[str]:
    """Pick N unique segment_ids derived from ``base_id``. Mirrors
    ``_derive_split_ids`` but produces N suffixes instead of two.

    Scheme: ``_a, _b, ..., _<letter_N>`` if all free; on collision
    fall back to ``_s1a, _s1b, ...``, then ``_s2a, ...``.
    """
    letters = "abcdefghijklmnopqrstuvwxyz"
    if n <= 0:
        raise ValueError("n must be ≥ 1")
    if n <= len(letters):
        candidates = [f"{base_id}_{letters[i]}" for i in range(n)]
        if not any(c in existing_ids for c in candidates):
            return candidates
    counter = 1
    while True:
        candidates = [f"{base_id}_s{counter}{letters[i]}" for i in range(n)]
        if not any(c in existing_ids for c in candidates):
            return candidates
        counter += 1


def _validate_many_cuts(
    cuts: list[dict[str, Any]],
    speaker_ids: list[str],
    source_text: str,
    cn_text: str,
) -> None:
    """Raise ValueError on any invariant violation.

    Invariants:
      - len(cuts) ≥ 1
      - len(speaker_ids) == len(cuts) + 1
      - all cut indices in (0, len(text))
      - strictly monotonic in BOTH source_index and cn_index
      - all speakers non-empty strings
    """
    if not isinstance(cuts, list) or not cuts:
        raise ValueError("cuts must be a non-empty list")
    if not isinstance(speaker_ids, list):
        raise ValueError("speaker_ids must be a list")
    expected = len(cuts) + 1
    if len(speaker_ids) != expected:
        raise ValueError(
            f"speaker_ids count {len(speaker_ids)} must equal cuts+1 ({expected})"
        )
    src_len = len(source_text)
    cn_len = len(cn_text)
    prev_si = 0
    prev_ci = 0
    for i, c in enumerate(cuts):
        if not isinstance(c, dict):
            raise ValueError(f"cut {i} must be an object")
        try:
            si = int(c.get("source_index"))
            ci = int(c.get("cn_index"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"cut {i} indices must be ints: {exc}")
        if not (0 < si < src_len):
            raise ValueError(
                f"cut {i}: source_index {si} not in (0, {src_len})"
            )
        if not (0 < ci < cn_len):
            raise ValueError(
                f"cut {i}: cn_index {ci} not in (0, {cn_len})"
            )
        if si <= prev_si:
            raise ValueError(
                f"cut {i}: source_index {si} must be strictly greater than previous ({prev_si})"
            )
        if ci <= prev_ci:
            raise ValueError(
                f"cut {i}: cn_index {ci} must be strictly greater than previous ({prev_ci})"
            )
        prev_si, prev_ci = si, ci
    for i, sp in enumerate(speaker_ids):
        if not isinstance(sp, str) or not sp.strip():
            raise ValueError(f"speaker_ids[{i}] is empty / not a string")


def _build_many_new_segments(
    original: dict[str, Any],
    cuts: list[dict[str, Any]],
    speaker_ids: list[str],
    new_ids: list[str],
    project_dir: str | Path,
) -> list[dict[str, Any]]:
    """Build the N+1 sub-segment dicts inheriting metadata from original.

    Time boundaries computed via estimate_split_ms (same word-boundary
    snapping as single split) — see split_editing_segment.
    """
    from services.transcript_reviewer import estimate_split_ms

    source_text = str(original.get("source_text") or "")
    cn_text = str(original.get("cn_text") or "")
    start_ms = int(original.get("start_ms", 0) or 0)
    end_ms = int(original.get("end_ms", start_ms) or start_ms)

    words_data = _load_split_words_data(project_dir)

    # Compute midpoint ms for each cut.
    mid_ms_list: list[int] = []
    for c in cuts:
        mid = estimate_split_ms(
            start_ms=start_ms,
            end_ms=end_ms,
            source_text=source_text,
            split_char_pos=int(c["source_index"]),
            words_data=words_data,
        )
        mid_ms_list.append(mid)

    # Build time boundaries [start, mid_1, mid_2, ..., mid_N, end]
    boundaries = [start_ms] + mid_ms_list + [end_ms]
    for i in range(len(boundaries) - 1):
        if boundaries[i + 1] <= boundaries[i]:
            raise ValueError(
                "split would produce a zero/negative-duration piece "
                f"(boundary {i}: {boundaries[i]} → {boundaries[i + 1]}). "
                "Segment too short or cut positions too close together."
            )

    # Source/CN text slices: [0..s_1), [s_1..s_2), ..., [s_N..end)
    src_indices = [0] + [int(c["source_index"]) for c in cuts] + [len(source_text)]
    cn_indices = [0] + [int(c["cn_index"]) for c in cuts] + [len(cn_text)]

    pieces: list[dict[str, Any]] = []
    for i in range(len(new_ids)):
        piece = dict(original)
        seg_start = boundaries[i]
        seg_end = boundaries[i + 1]
        piece.update(
            segment_id=new_ids[i],
            source_text=source_text[src_indices[i]:src_indices[i + 1]],
            cn_text=cn_text[cn_indices[i]:cn_indices[i + 1]],
            speaker_id=str(speaker_ids[i]).strip() or original.get("speaker_id"),
            start_ms=seg_start,
            end_ms=seg_end,
            target_duration_ms=max(0, seg_end - seg_start),
        )
        if "duration_target_ms" in piece:
            piece["duration_target_ms"] = max(0, seg_end - seg_start)
        pieces.append(piece)
    return pieces


def split_editing_segment_many(
    project_dir: str | Path,
    *,
    segment_id: str,
    cuts: list[dict[str, Any]],
    speaker_ids: list[str],
) -> dict[str, Any]:
    """Atomic multi-cut split — replace one segment with N+1 pieces.

    Args:
        project_dir: project root (contains editor/editing/).
        segment_id: id of the segment being split.
        cuts: ordered list of {source_index, cn_index} dicts (strictly
            increasing in both indices). At least 1 cut.
        speaker_ids: per-piece speaker assignment; length = len(cuts) + 1.

    Returns:
        {
          "replaced_segment_id": <orig_id>,
          "new_segments": [seg_1, ..., seg_N+1],
          "total_count": <total segment count after split>,
        }

    Raises:
        EditingConflictError: segment_id not found.
        ValueError: cut validation failure (empty piece, non-monotonic,
            out of bounds, zero-duration piece in ms-space).

    Atomicity: write-ahead journal (.split_journal_<parent>.json) is
    laid down BEFORE any of segments/segment_status/voice_map .json get
    replaced. Failure between journal-write and journal-delete is
    recovered by the next loader call via
    ``_reconcile_split_journal_if_needed``.
    """
    validate_segment_id(segment_id)
    _ensure_editing_dir(project_dir)

    # Late import to avoid circular dep (editing_voice_map imports us).
    from services.jobs.editing_voice_map import (
        _voice_map_path,
    )

    with file_lock(_editing_lock_anchor(project_dir)):
        # First reconcile any leftover journal so we start from clean state.
        _reconcile_split_journal_if_needed(project_dir)

        segments = _read_segments_json_raw(project_dir)
        target = str(segment_id)
        index: int | None = None
        for i, seg in enumerate(segments):
            if isinstance(seg, dict) and str(seg.get("segment_id")) == target:
                index = i
                break
        if index is None:
            raise EditingConflictError(
                f"segment_id {segment_id!r} not found in editing/segments.json"
            )

        original = dict(segments[index])
        source_text = str(original.get("source_text") or "")
        cn_text = str(original.get("cn_text") or "")
        _validate_many_cuts(cuts, speaker_ids, source_text, cn_text)

        # Reserve new ids
        existing_ids: set[str] = {
            str(s.get("segment_id"))
            for s in segments
            if isinstance(s, dict) and s.get("segment_id") is not None
        }
        existing_ids.discard(target)
        n_pieces = len(speaker_ids)
        new_ids = _derive_split_ids_many(target, n_pieces, existing_ids)

        # Build new sub-segment dicts (raises ValueError on zero-duration).
        new_pieces = _build_many_new_segments(
            original, cuts, speaker_ids, new_ids, project_dir,
        )

        # ── Compose proposed new state for all three files ──
        new_segments = list(segments)
        new_segments[index : index + 1] = new_pieces

        # Status map: pop old id, add all new ids as text_dirty.
        old_status = _read_segment_status_json_raw(project_dir)
        new_status = dict(old_status)
        new_status.pop(target, None)
        for nid in new_ids:
            new_status[nid] = SEGMENT_STATUS_TEXT_DIRTY

        # Voice_map: copy parent override (if any) to ALL N+1 sub-segs
        # (mirrors single-split P0-8 pattern at editing_segments.py:1145).
        old_vm = _read_voice_map_json_raw(project_dir)
        new_voice_map = dict(old_vm)
        if target in new_voice_map:
            override = dict(new_voice_map.pop(target))
            for nid in new_ids:
                new_voice_map[nid] = dict(override)

        # ── Write-ahead journal ── plan §5.6 step 4
        journal_payload = {
            "schema_version": SPLIT_JOURNAL_SCHEMA_VERSION,
            "parent_sid": target,
            "new_sub_segment_ids": new_ids,
            "new_segments": new_segments,
            "new_status": new_status,
            "new_voice_map": new_voice_map,
        }
        journal_path = _split_journal_path(project_dir, target)
        _atomic_write_json(journal_path, journal_payload)

        # ── Now write the three files (atomic per-file, not transactional
        #    across the three; journal is the recovery anchor). ──
        try:
            _atomic_write_json(_segments_path(project_dir), new_segments)
            _atomic_write_json(_segment_status_path(project_dir), new_status)
            _atomic_write_json(_voice_map_path(project_dir), new_voice_map)
        except Exception:
            # Any rename failure → journal stays. Next loader reconciles.
            logger.exception(
                "split_many: file-rename phase failed for parent=%s; "
                "journal preserved for next-load reconcile",
                target,
            )
            raise

        # ── Cleanup parent's draft wav (P1-16 pattern: orphan draft would
        #    otherwise be picked up by commit's draft-promotion phase). ──
        parent_draft = _editing_dir(project_dir) / "tts_segments_draft" / f"{target}.wav"
        if parent_draft.exists():
            try:
                parent_draft.unlink()
            except OSError:
                logger.warning(
                    "split_many: failed to cleanup parent draft wav %s; "
                    "may leave orphan",
                    parent_draft,
                )

        # ── Success — delete journal. If this fails the journal becomes
        # stale; reconciler classifies as state B and cleans up. ──
        try:
            journal_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "split_many: failed to delete journal %s; "
                "reconciler will clean on next load",
                journal_path,
            )

        return {
            "replaced_segment_id": target,
            "new_segments": new_pieces,
            "total_count": len(new_segments),
        }


# ---------------------------------------------------------------------------
# Raw read helpers — bypass _reconcile_split_journal_if_needed to avoid
# infinite recursion when the reconciler itself needs to inspect current
# state. Public loaders always go through the reconciling path.
# ---------------------------------------------------------------------------


def _read_segments_json_raw(project_dir: str | Path) -> list[dict[str, Any]]:
    path = _segments_path(project_dir)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _read_segment_status_json_raw(project_dir: str | Path) -> dict[str, str]:
    path = _segment_status_path(project_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _read_voice_map_json_raw(project_dir: str | Path) -> dict[str, dict[str, Any]]:
    from services.jobs.editing_voice_map import _voice_map_path

    path = _voice_map_path(project_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): (dict(v) if isinstance(v, dict) else {})
        for k, v in data.items()
    }


# ---------------------------------------------------------------------------
# _reconcile_split_journal_if_needed — three-state recovery (plan §5.6)
# ---------------------------------------------------------------------------


def _reconcile_split_journal_if_needed(project_dir: str | Path) -> None:
    """Detect + apply any pending split journals. Idempotent; no-op when
    no journal file exists. Called by load_editing_segments /
    load_segment_status / load_voice_map at their top.

    Three-state classification (plan §5.6 step d):
      State A: parent_sid still present in segments.json
               → journal is fresh, apply it (overwrite three files).
      State B: parent_sid absent + all sub-segment_ids present
               → split was committed but journal-delete failed (and
               possibly user has edited sub-segs since). Backfill ONLY
               missing status/voice_map entries; do NOT overwrite
               existing entries (would lose user edits).
      State C: any other mix → EditingCorruptionError. Operator must
               inspect; no auto-recovery.

    All apply paths take the editing_lock_anchor file_lock to coordinate
    with concurrent split_many calls. Reads outside the lock are fine
    because os.replace makes each file's state binary.
    """
    journals = _list_split_journals(project_dir)
    if not journals:
        return

    # Slow path: take the lock to coordinate with any in-flight split.
    with file_lock(_editing_lock_anchor(project_dir)):
        # Re-glob inside the lock — another writer may have cleaned up.
        journals = _list_split_journals(project_dir)
        for journal_path in journals:
            try:
                payload = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # Corrupt journal — delete it (operator must redo split).
                logger.warning(
                    "reconcile: corrupt journal %s; deleting", journal_path,
                )
                try:
                    journal_path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            try:
                _apply_split_journal(project_dir, payload, journal_path)
            except EditingCorruptionError:
                # Surface to caller; journal stays for operator inspection.
                raise
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reconcile: unexpected error applying journal %s; "
                    "leaving for retry",
                    journal_path,
                )
                # Don't delete — next load tries again.


def _apply_split_journal(
    project_dir: str | Path,
    payload: dict[str, Any],
    journal_path: Path,
) -> None:
    """Apply one journal payload. Caller holds file_lock."""
    if not isinstance(payload, dict):
        raise EditingCorruptionError(
            f"split journal {journal_path.name} is not an object"
        )
    parent_sid = payload.get("parent_sid")
    new_sub_ids = payload.get("new_sub_segment_ids", [])
    new_segments = payload.get("new_segments")
    new_status = payload.get("new_status", {})
    new_voice_map = payload.get("new_voice_map", {})
    if not isinstance(parent_sid, str) or not parent_sid:
        raise EditingCorruptionError(
            f"split journal {journal_path.name} missing parent_sid"
        )
    if not isinstance(new_sub_ids, list) or not new_sub_ids:
        raise EditingCorruptionError(
            f"split journal {journal_path.name} missing new_sub_segment_ids"
        )
    if not isinstance(new_segments, list):
        raise EditingCorruptionError(
            f"split journal {journal_path.name} missing new_segments"
        )

    # Classify state by reading current segments.json (raw, no reconcile).
    current_segments = _read_segments_json_raw(project_dir)
    current_ids = {
        str(s.get("segment_id"))
        for s in current_segments
        if isinstance(s, dict) and s.get("segment_id") is not None
    }
    parent_present = parent_sid in current_ids
    all_subs_present = all(sid in current_ids for sid in new_sub_ids)

    if parent_present and not all_subs_present:
        # State A: journal is fresh — split's file-rename phase didn't
        # complete. Apply full state.
        logger.info(
            "reconcile state A: parent=%s present, applying journal", parent_sid,
        )
        _atomic_write_json(_segments_path(project_dir), new_segments)
        _atomic_write_json(_segment_status_path(project_dir), dict(new_status))
        from services.jobs.editing_voice_map import _voice_map_path
        _atomic_write_json(_voice_map_path(project_dir), dict(new_voice_map))
        journal_path.unlink(missing_ok=True)
        return

    if (not parent_present) and all_subs_present:
        # State B: split was committed. Journal is stale (delete-step
        # failed previously). Backfill ONLY missing entries — do NOT
        # overwrite existing status/voice_map (would clobber user edits
        # made between split-commit and reconcile).
        logger.info(
            "reconcile state B: split committed, backfilling missing entries (parent=%s)",
            parent_sid,
        )
        cur_status = _read_segment_status_json_raw(project_dir)
        cur_vm = _read_voice_map_json_raw(project_dir)
        changed_status = False
        changed_vm = False
        if isinstance(new_status, dict):
            for sid in new_sub_ids:
                if sid not in cur_status and sid in new_status:
                    cur_status[sid] = str(new_status[sid])
                    changed_status = True
        if isinstance(new_voice_map, dict):
            for sid in new_sub_ids:
                if sid not in cur_vm and sid in new_voice_map:
                    cur_vm[sid] = dict(new_voice_map[sid])
                    changed_vm = True
        if changed_status:
            _atomic_write_json(_segment_status_path(project_dir), cur_status)
        if changed_vm:
            from services.jobs.editing_voice_map import _voice_map_path
            _atomic_write_json(_voice_map_path(project_dir), cur_vm)
        journal_path.unlink(missing_ok=True)
        return

    # State C: mixed — operator must inspect. Don't auto-recover.
    raise EditingCorruptionError(
        f"split journal {journal_path.name}: inconsistent state — "
        f"parent_sid={parent_sid!r} present={parent_present}, "
        f"all_subs_present={all_subs_present}. "
        f"Operator must reconcile manually."
    )


# ---------------------------------------------------------------------------
# load_segment_word_context — Phase 2b read-only data source for smart
# split-prefill in the frontend modal (plan 2026-05-17 §5.4).
#
# Returns word-level timing + speaker labels for words within the given
# segment's [start_ms, end_ms]. Schema-trimmed to {text, start, end,
# speaker} — frontend doesn't need confidence / channel. The full
# raw_assemblyai.json may be 5-50 MB; this endpoint clips to the segment
# range so the wire payload stays small (~50 words × 80 bytes typical).
#
# Used by GET /jobs/{id}/segments/{sid}/word-context. Read-only; routes
# through the gateway's generic proxy (Codex round 5 P2 #1 decision
# documented in plan §8.2 step 5).
# ---------------------------------------------------------------------------


def load_segment_word_context(
    project_dir: str | Path,
    segment_id: str,
) -> dict[str, Any]:
    """Phase 2b: word-level data for one segment's time range.

    Returns:
        {
          "segment_id": str,
          "words": list of {text, start, end, speaker} dicts,
          "available": bool — False when transcript/raw_*.json is missing,
        }

    Raises:
        EditingConflictError: segment_id not found in editing/segments.json.
    """
    validate_segment_id(segment_id)
    # Raw read — reconcile not needed for this read-only data view.
    segments = _read_segments_json_raw(project_dir)
    seg: dict[str, Any] | None = None
    for s in segments:
        if isinstance(s, dict) and str(s.get("segment_id")) == segment_id:
            seg = s
            break
    if seg is None:
        raise EditingConflictError(
            f"segment_id {segment_id!r} not found in editing/segments.json"
        )
    start_ms = int(seg.get("start_ms", 0) or 0)
    end_ms = int(seg.get("end_ms", start_ms) or start_ms)

    words_all = _load_split_words_data(project_dir)
    if not words_all:
        return {
            "segment_id": segment_id,
            "words": [],
            "available": False,
        }

    filtered: list[dict[str, Any]] = []
    for w in words_all:
        if not isinstance(w, dict):
            continue
        try:
            w_start = int(w.get("start", 0) or 0)
            w_end = int(w.get("end", 0) or 0)
        except (TypeError, ValueError):
            continue
        if w_start >= start_ms and w_end <= end_ms:
            filtered.append({
                "text": str(w.get("text", "")),
                "start": w_start,
                "end": w_end,
                "speaker": w.get("speaker"),
            })

    # Safety cap. 30s segment typically has ~50 words; 1000 covers any
    # plausible single-segment edge case while keeping payload well
    # under 500 KB.
    cap = 1000
    if len(filtered) > cap:
        logger.warning(
            "word-context: %d words for segment %s; capping to %d",
            len(filtered), segment_id, cap,
        )
        filtered = filtered[:cap]

    return {
        "segment_id": segment_id,
        "words": filtered,
        "available": True,
    }


# ---------------------------------------------------------------------------
# slice_source_audio_for_editing_segment (2026-04-21 / plan §7.4) — returns
# a base64-encoded WAV slice of the ORIGINAL (pre-translation) audio for
# one segment's time range. Parallels web_ui.review_actions.preview_segment's
# source-clip branch, but scoped to editing-mode data.
# ---------------------------------------------------------------------------


_SOURCE_AUDIO_CANDIDATES: tuple[str, ...] = (
    # Preferred: the speech-isolated track (mono 16k, smaller + denoised
    # by the separator step). Falls back to the raw original if
    # separation didn't run or was cleaned up.
    "audio/speech_for_asr.wav",
    "audio/original.wav",
)


def _find_source_audio_path(project_dir: str | Path) -> Path | None:
    root = Path(project_dir)
    for candidate_name in _SOURCE_AUDIO_CANDIDATES:
        p = root / candidate_name
        if p.is_file():
            return p
    return None


def _get_editing_segment_timing(
    project_dir: str | Path, segment_id: str
) -> tuple[int, int]:
    """Return (start_ms, end_ms) for one editing segment.

    Raises EditingConflictError if the segment is missing; raises
    ValueError if the segment has no usable timing info (unlikely — upstream
    pipeline always writes start_ms/end_ms, but we don't want to emit a
    0 ms slice silently)."""
    segments = load_editing_segments(project_dir)
    target = str(segment_id)
    for seg in segments:
        if isinstance(seg, dict) and str(seg.get("segment_id")) == target:
            try:
                start_ms = int(seg.get("start_ms", 0) or 0)
                end_ms = int(seg.get("end_ms", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"segment {segment_id!r} has non-integer timing fields"
                ) from exc
            if end_ms <= start_ms:
                raise ValueError(
                    f"segment {segment_id!r} has zero-or-negative duration: "
                    f"start_ms={start_ms}, end_ms={end_ms}"
                )
            return start_ms, end_ms
    raise EditingConflictError(
        f"segment_id {segment_id!r} not found in editing/segments.json"
    )


def _ffmpeg_slice_to_wav_bytes(
    source_audio_path: Path,
    start_ms: int,
    end_ms: int,
    *,
    timeout_s: int = 30,
) -> bytes:
    """Run ffmpeg to cut ``source_audio_path`` to a mono 16k WAV byte
    stream. Separated from the dispatcher so tests can monkeypatch this
    one function without touching the full slicer."""
    import subprocess
    start_s = start_ms / 1000.0
    end_s = end_ms / 1000.0
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(source_audio_path),
            "-ss", str(start_s),
            "-to", str(end_s),
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            "-f", "wav",
            "pipe:1",
        ],
        capture_output=True,
        timeout=timeout_s,
    )
    if result.returncode != 0 or not result.stdout:
        stderr_tail = (result.stderr or b"")[-2000:].decode("utf-8", errors="replace")
        raise RuntimeError(
            f"ffmpeg slice failed (rc={result.returncode}): {stderr_tail}"
        )
    return result.stdout


def slice_source_audio_for_editing_segment(
    project_dir: str | Path,
    segment_id: str,
) -> dict[str, object]:
    """Produce a base64-encoded WAV slice of the source audio for one
    editing segment, ready to be piped into an HTML ``<audio>`` element
    via ``data:audio/wav;base64,...``.

    Timing comes from ``editor/editing/segments.json`` (not the baseline),
    so if a prior split edited the boundaries the preview matches what
    the user sees.

    2026-04-21 NOTE: this path is kept for callers that truly want the
    inline base64 (unit tests). Production HTTP path uses
    :func:`cache_preview_source_wav` + the GET stream endpoint to avoid
    the ``RemoteProtocolError`` on 1 MB+ JSON bodies through
    Uvicorn+httpx.
    """
    import base64

    wav_bytes, meta = _slice_source_audio_bytes(project_dir, segment_id)
    return {
        "source_audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
        **meta,
    }


def _slice_source_audio_bytes(
    project_dir: str | Path, segment_id: str,
) -> tuple[bytes, dict[str, object]]:
    """Shared core: slice the source audio to WAV bytes + return timing meta."""
    validate_segment_id(segment_id)
    start_ms, end_ms = _get_editing_segment_timing(project_dir, segment_id)

    source_path = _find_source_audio_path(project_dir)
    if source_path is None:
        raise RuntimeError(
            "源音频文件不存在（audio/speech_for_asr.wav 或 "
            "audio/original.wav 都未找到）。"
        )

    wav_bytes = _ffmpeg_slice_to_wav_bytes(source_path, start_ms, end_ms)
    meta = {
        "mime_type": "audio/wav",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": end_ms - start_ms,
    }
    return wav_bytes, meta


# Cache lives under editing/ so it's auto-cleaned by commit / cancel /
# idle-scanner's editor/editing/ teardown — no separate TTL needed.
_PREVIEW_CACHE_SUBDIR = "preview_cache"


def cache_preview_source_wav(
    project_dir: str | Path, segment_id: str,
) -> tuple[Path, dict[str, object]]:
    """Slice the source audio and persist the WAV to
    ``editor/editing/preview_cache/{segment_id}.wav``. Returns the
    (path, meta) pair.

    Overwrites any existing cache for the same segment — fresh edits
    to timing are picked up without explicit invalidation. Atomic via
    tmp-then-rename so a concurrent GET stream won't read a half-written
    file.
    """
    _ensure_editing_dir(project_dir)
    wav_bytes, meta = _slice_source_audio_bytes(project_dir, segment_id)
    cache_dir = _editing_dir(project_dir) / _PREVIEW_CACHE_SUBDIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    final_path = cache_dir / f"{segment_id}.wav"
    tmp_path = cache_dir / f"{segment_id}.wav.tmp"
    tmp_path.write_bytes(wav_bytes)
    tmp_path.replace(final_path)
    return final_path, meta


def preview_cache_path(
    project_dir: str | Path, segment_id: str,
) -> Path:
    """Return the expected cache path for this segment's preview WAV
    (file may or may not exist — caller must check)."""
    return (
        _editing_dir(project_dir) / _PREVIEW_CACHE_SUBDIR / f"{segment_id}.wav"
    )
