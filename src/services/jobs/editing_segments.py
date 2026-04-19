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
    """
    segments = load_editing_segments(project_dir)
    status = load_segment_status(project_dir)
    return {
        "segments": segments,
        "segment_status": status,
        "total": len(segments),
    }


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
        updated[key] = value
        applied[key] = value

    if not applied:
        raise ValueError(
            "patch body contained no patchable fields; allowed: "
            f"{sorted(PATCHABLE_SEGMENT_FIELDS)}"
        )

    segments[index] = updated
    _atomic_write_json(_segments_path(project_dir), segments)

    # Any patch that touched cn_text invalidates the existing TTS; mark dirty.
    # translation_confirmed / rewrite_requested flips alone don't change the
    # rendered audio so they stay at whatever the previous status was.
    if "cn_text" in applied:
        mark_segment_status(project_dir, segment_id, SEGMENT_STATUS_TEXT_DIRTY)

    return updated


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
