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
    "compute_residual_segment_status",
    "load_editing_segments",
    "load_segment_status",
    "patch_editing_segment",
    "revert_text_changes_to_audio_baseline",
    "split_editing_segment",
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

        # Time split is proportional to the source-character position. We lack
        # word-level timing in editing mode (it's an alignment detail), so a
        # uniform speaking-rate assumption is the honest approximation — the
        # downstream re-alignment will re-anchor using the new TTS waveforms.
        start_ms = int(original.get("start_ms", 0) or 0)
        end_ms = int(original.get("end_ms", start_ms) or start_ms)
        ratio = split_source_index / len(source_text) if source_text else 0.5
        mid_ms = start_ms + int(round((end_ms - start_ms) * ratio))
        # P0-8 (audit 2026-05-07): refuse splits that would produce a
        # zero-duration half. This happens when (end_ms - start_ms) is very
        # small or when ratio rounds to 0/1 — the resulting half can break
        # downstream alignment math (TTS would have to fit text into 0 ms).
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
        )
        seg_b = dict(original)
        seg_b.update(
            segment_id=new_id_b,
            source_text=source_text[split_source_index:],
            cn_text=cn_text[split_cn_index:],
            speaker_id=str(speaker_b).strip() or original.get("speaker_id"),
            start_ms=mid_ms,
            end_ms=end_ms,
        )

        original_speaker = original.get("speaker_id")
        if seg_a.get("speaker_id") != original_speaker:
            _propagate_speaker_change(
                segments=segments,
                index=index,
                updated=seg_a,
                new_speaker_id=str(seg_a.get("speaker_id") or ""),
            )
        if seg_b.get("speaker_id") != original_speaker:
            _propagate_speaker_change(
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
