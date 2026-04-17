"""Shared helpers for materials pack resolution, used by both the legacy
synchronous ``materials_api`` endpoint and the new async executor.

Keeps item→artifact mapping, manifest-index loading, path-safety checks,
and size-limit logic in one place. Pure functions, no I/O side effects
beyond reading files.
"""

from __future__ import annotations

import json
from pathlib import Path

# Map from user-facing item keys to artifact keys in manifest.
ITEM_TO_ARTIFACT_KEYS: dict[str, list[str]] = {
    "source_video": ["source.original_video"],
    "dubbed_video": ["publish.dubbed_video"],
    "dubbed_audio": ["editor.dubbed_audio_complete"],
    "segments": ["editor.segments_dir"],
    "subtitles": ["editor.subtitles", "editor.subtitles_en", "editor.subtitles_bilingual"],
}

MAX_ZIP_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB


def load_artifact_index(project_dir: Path) -> dict[str, str]:
    """Read manifest.json and return its artifact_index dict (or empty)."""
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    index = payload.get("artifact_index")
    return index if isinstance(index, dict) else {}


def resolve_artifact_path(
    project_dir: Path,
    artifact_index: dict[str, str],
    key: str,
) -> Path | None:
    """Resolve an artifact key to an absolute path, rejecting traversal."""
    raw = artifact_index.get(key)
    if not raw or not isinstance(raw, str):
        return None
    p = Path(raw.strip())
    if not p.is_absolute():
        p = (project_dir / p).resolve(strict=False)
    else:
        p = p.resolve(strict=False)
    try:
        p.relative_to(project_dir.resolve(strict=False))
    except ValueError:
        return None
    return p if p.exists() else None


def collect_files_for_items(
    *,
    project_dir: Path,
    artifact_index: dict[str, str],
    item_list: list[str],
) -> tuple[list[tuple[str, Path]], int]:
    """Enumerate files to be packed.

    Returns ``(files, total_size)`` where files is a list of
    ``(arcname, absolute_path)`` tuples ready for zip writing.
    """
    files: list[tuple[str, Path]] = []
    total_size = 0

    for item_key in item_list:
        artifact_keys = ITEM_TO_ARTIFACT_KEYS.get(item_key)
        if not artifact_keys:
            continue
        for ak in artifact_keys:
            if ak == "editor.segments_dir":
                resolved = resolve_artifact_path(project_dir, artifact_index, ak)
                if resolved and resolved.is_dir():
                    for wav_file in sorted(resolved.rglob("*.wav")):
                        rel = wav_file.relative_to(resolved)
                        arcname = f"segments/{rel}"
                        size = wav_file.stat().st_size
                        total_size += size
                        files.append((arcname, wav_file))
            else:
                resolved = resolve_artifact_path(project_dir, artifact_index, ak)
                if resolved and resolved.is_file():
                    arcname = resolved.name
                    size = resolved.stat().st_size
                    total_size += size
                    files.append((arcname, resolved))
    return files, total_size
