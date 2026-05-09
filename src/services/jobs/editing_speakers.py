"""Editing-mode speakers registry. Persisted at
``<project_dir>/editor/editing/speakers.json``. Decoupled from baseline
``review_state.json``; merged back into baseline only at commit time."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from services._file_lock import file_lock
from services.jobs.editing_segments import EDITING_SUBDIR_NAME

logger = logging.getLogger(__name__)

__all__ = [
    "EditingSpeaker", "DisplayNameConflictError",
    "load_speakers", "load_baseline_speakers",
    "create_speaker", "next_speaker_id",
    "editing_speakers_path",
]

SPEAKERS_FILENAME = "speakers.json"
_PALETTE = (
    "#8B5CF6", "#06B6D4", "#10B981", "#F59E0B",
    "#EF4444", "#EC4899", "#6366F1", "#84CC16",
)


class DisplayNameConflictError(ValueError):
    """display_name 已存在（baseline 或 editing）。"""


@dataclass
class EditingSpeaker:
    speaker_id: str
    display_name: str
    color: str | None = None
    source: str = "editing"  # "baseline" | "editing"
    created_at: str = ""
    profile_status: str = "pending_segments"
    profile_error: str | None = None
    voice_profile: dict | None = None


def editing_speakers_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / EDITING_SUBDIR_NAME / SPEAKERS_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _color_for_id(speaker_id: str) -> str:
    digest = hashlib.sha1(speaker_id.encode("utf-8")).hexdigest()
    return _PALETTE[int(digest[:8], 16) % len(_PALETTE)]


def load_speakers(project_dir: str | Path) -> list[EditingSpeaker]:
    path = editing_speakers_path(project_dir)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "editing_speakers: speakers.json at %s is unreadable / corrupt; "
            "treating as empty",
            path,
        )
        return []
    return [EditingSpeaker(**sp) for sp in raw.get("speakers", [])]


def load_baseline_speakers(project_dir: str | Path) -> list[dict]:
    """Read baseline display_names from review_state.json (project root).

    Returns ``[{"speaker_id": "...", "display_name": "..."}, ...]``.
    Uses :class:`ReviewStateManager` so we share the project's canonical
    JSON-tolerance logic. Returns ``[]`` if the file is missing /
    malformed / has no speaker_review stage.
    """
    rs_path = Path(project_dir) / "review_state.json"
    if not rs_path.is_file():
        return []
    try:
        from services.review_state import ReviewStateManager, SPEAKER_REVIEW_STAGE
        manager = ReviewStateManager(rs_path)
        stage = manager.get_stage(SPEAKER_REVIEW_STAGE)
    except Exception as exc:
        logger.warning(
            "load_baseline_speakers: review_state.json at %s unreadable; "
            "returning [] (cause: %s)", rs_path, exc,
        )
        return []
    if stage is None:
        return []
    payload = stage.get("payload") or {}
    names = payload.get("speaker_names")
    if not isinstance(names, dict):
        return []
    return [
        {"speaker_id": str(sid), "display_name": str(dn)}
        for sid, dn in names.items()
    ]


def _save(project_dir: str | Path, speakers: list[EditingSpeaker]) -> None:
    path = editing_speakers_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "speakers": [asdict(s) for s in speakers],
        "updated_at": _now_iso(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def next_speaker_id(used: Iterable[str]) -> str:
    used_set = set(used)
    for letter in "abcdefghijklmnopqrstuvwxyz":
        cand = f"speaker_{letter}"
        if cand not in used_set:
            return cand
    while True:
        cand = f"speaker_{secrets.token_hex(4)}"
        if cand not in used_set:
            return cand


def create_speaker(
    project_dir: str | Path,
    *,
    display_name: str,
    baseline_speakers: list[dict[str, Any]],
) -> EditingSpeaker:
    """Raises DisplayNameConflictError on duplicate (trim + case-sensitive)."""
    norm_name = display_name.strip()
    if not norm_name:
        raise ValueError("display_name must be non-empty")

    with file_lock(editing_speakers_path(project_dir)):
        existing = load_speakers(project_dir)
        all_names = {sp.display_name for sp in existing}
        for bl in baseline_speakers:
            bl_name = (bl.get("display_name") or "").strip()
            if bl_name:
                all_names.add(bl_name)
        if norm_name in all_names:
            raise DisplayNameConflictError(
                f"display_name {norm_name!r} already exists"
            )

        used_ids = {sp.speaker_id for sp in existing}
        for bl in baseline_speakers:
            bid = bl.get("speaker_id")
            if bid:
                used_ids.add(bid)

        new_sp = EditingSpeaker(
            speaker_id=next_speaker_id(used_ids),
            display_name=norm_name,
            color=None,
            source="editing",
            created_at=_now_iso(),
            profile_status="pending_segments",
        )
        new_sp.color = _color_for_id(new_sp.speaker_id)
        existing.append(new_sp)
        _save(project_dir, existing)
        return new_sp
