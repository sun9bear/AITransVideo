"""Per-segment voice assignment buffer for the editing state (T1-6).

Plan ref: §3.5 / §7.3 / D22

Layout:
    project_dir/editor/editing/voice_map.json

Schema — dict keyed by segment_id. Presence of a key means "user explicitly
changed this segment's voice during this editing session". Absent keys mean
"keep whatever the baseline segments.json / speaker defaults say":

    {
      "seg_042": {
        "provider": "minimax",
        "voice_id": "male_1",
        "tts_model_key": "speech-2.8-hd"
      },
      "seg_088": {"provider": "cosyvoice", "voice_id": "voice_xyz"}
    }

Important decisions:

- **overwrite semantics** (plan H3): repeatedly changing the voice of the
  same segment only keeps the latest value; no history stack. Simpler to
  reason about and matches the UI (dropdown value IS the state).
- **segment_status coupling**: setting a voice flips
  ``segment_status[sid] = voice_dirty`` so the batch re-TTS scan picks it
  up. Clearing a voice demotes via
  ``compute_residual_segment_status`` so any surviving dirty source
  (text edit / draft wav) is preserved — only falls back to
  ``accepted`` when there's truly nothing left to re-render. At commit
  time the voice_map is gone and baseline audio wins for segments that
  ended up ``accepted``.
- **No baseline mutation** (§3.5 invariant): we NEVER touch ``segments.json``
  here. The commit flow (T1-9) reads voice_map + segments together and
  writes the merged result to the new project_dir.

Voice validation is deliberately minimal here — deep "is this voice_id
still available" logic lives in the frontend VoiceSelectionPanel and the
provider-specific selectors. We only enforce shape + non-empty strings.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from services._file_lock import file_lock
from services.jobs.editing import EDITING_SUBDIR, EditingConflictError
from services.jobs.editing_segments import (
    SEGMENT_STATUS_VOICE_DIRTY,
    _editing_lock_anchor,
    compute_residual_segment_status,
    mark_segment_status,
)
from services.jobs.input_validators import validate_segment_id

logger = logging.getLogger(__name__)

__all__ = [
    "VOICE_MAP_FILE",
    "VOICE_MAP_ENTRY_FIELDS",
    "clear_voice_override",
    "load_voice_map",
    "set_voice_override",
]

VOICE_MAP_FILE: str = f"{EDITING_SUBDIR}/voice_map.json"

# Fields every voice_map entry must have. Stored as a dict per-segment so
# future additions (e.g. rate/pitch override) don't require a schema bump.
VOICE_MAP_ENTRY_FIELDS: frozenset[str] = frozenset({"provider", "voice_id"})
VOICE_MAP_OPTIONAL_ENTRY_FIELDS: frozenset[str] = frozenset({"tts_model_key"})


def _normalize_tts_model_key(value: object) -> str:
    return str(value or "").strip()


def _voice_map_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / VOICE_MAP_FILE


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with open(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        tmp_path.replace(path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def load_voice_map(project_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Return the current voice_map dict. Missing file → {}."""
    path = _voice_map_path(project_dir)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise EditingConflictError(
            f"editing/voice_map.json is not an object (got {type(data).__name__})"
        )
    # Defensive normalisation: only keep entries that look well-formed.
    out: dict[str, dict[str, Any]] = {}
    for sid, entry in data.items():
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider", "")).strip()
        voice_id = str(entry.get("voice_id", "")).strip()
        if not provider or not voice_id:
            continue
        normalized: dict[str, Any] = {"provider": provider, "voice_id": voice_id}
        model_key = _normalize_tts_model_key(
            entry.get("tts_model_key") or entry.get("tts_model") or entry.get("model")
        )
        if model_key:
            normalized["tts_model_key"] = model_key
        out[str(sid)] = normalized
    return out


def set_voice_override(
    project_dir: str | Path,
    segment_id: str,
    *,
    provider: str,
    voice_id: str,
    tts_model_key: str | None = None,
) -> dict[str, Any]:
    """Record a user's voice change for ``segment_id`` and flag the segment
    ``voice_dirty``. Returns the updated voice_map entry."""
    validate_segment_id(segment_id)
    provider = str(provider).strip()
    voice_id = str(voice_id).strip()
    if not provider:
        # 2026-05-09 加日志: editing add-speaker plan 上线后碰到 400 排障——
        # 没有 provider 时记一行让我们能在 stdout 反查 payload。
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "set_voice_override: empty provider (segment_id=%r voice_id=%r)",
            segment_id, voice_id,
        )
        raise ValueError("provider must be non-empty")
    if not voice_id:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "set_voice_override: empty voice_id (segment_id=%r provider=%r)",
            segment_id, provider,
        )
        raise ValueError("voice_id must be non-empty")
    editing_dir = Path(project_dir) / EDITING_SUBDIR
    if not editing_dir.is_dir():
        raise EditingConflictError(
            f"editing dir does not exist: {editing_dir}; call enter_editing first"
        )

    # P0-5 (audit 2026-05-07): protect voice_map + segment_status as a
    # single logical unit. Shares the editing-state anchor with
    # patch_editing_segment / mark_segment_status (reentrant) so a
    # concurrent patch cannot interleave between the two writes.
    with file_lock(_editing_lock_anchor(project_dir)):
        voice_map = load_voice_map(project_dir)
        entry: dict[str, Any] = {"provider": provider, "voice_id": voice_id}
        model_key = _normalize_tts_model_key(tts_model_key)
        if model_key:
            entry["tts_model_key"] = model_key
        voice_map[segment_id] = entry
        _atomic_write_json(_voice_map_path(project_dir), voice_map)
        mark_segment_status(project_dir, segment_id, SEGMENT_STATUS_VOICE_DIRTY)
        return {"segment_id": segment_id, **entry}


def clear_voice_override(
    project_dir: str | Path,
    segment_id: str,
) -> dict[str, Any]:
    """Remove the voice override for ``segment_id`` (segment reverts to
    whatever the baseline says at commit time).

    Demotes segment_status via ``compute_residual_segment_status`` so a
    still-edited cn_text (text_dirty) or surviving draft wav (tts_dirty)
    is preserved — naive unconditional ``accepted`` would hide user
    edits from batch re-TTS and ship stale audio (Claude Code
    ultrareview #3 / CodeX P1).

    Idempotent — removing a segment with no override in the map succeeds.
    """
    validate_segment_id(segment_id)
    editing_dir = Path(project_dir) / EDITING_SUBDIR
    if not editing_dir.is_dir():
        raise EditingConflictError(
            f"editing dir does not exist: {editing_dir}; call enter_editing first"
        )

    # P0-5 (audit 2026-05-07): same anchor + reentrant lock semantics as
    # set_voice_override.
    with file_lock(_editing_lock_anchor(project_dir)):
        voice_map = load_voice_map(project_dir)
        voice_map.pop(segment_id, None)
        _atomic_write_json(_voice_map_path(project_dir), voice_map)
        residual = compute_residual_segment_status(
            project_dir, segment_id, assume_no_voice_override=True,
        )
        mark_segment_status(project_dir, segment_id, residual)
        return {"segment_id": segment_id, "cleared": True}
