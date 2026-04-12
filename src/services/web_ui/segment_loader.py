from __future__ import annotations

import json
from pathlib import Path

from .output_entries import _resolve_translation_segments_path


def _normalize_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _resolve_project_file_path(project_dir: Path, raw_path: object) -> str | None:
    normalized_path = _normalize_optional_text(raw_path)
    if normalized_path is None:
        return None
    candidate_path = Path(normalized_path).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = (project_dir / candidate_path).resolve(strict=False)
    else:
        candidate_path = candidate_path.resolve(strict=False)
    if not candidate_path.exists() or not candidate_path.is_file():
        return None
    return str(candidate_path)


def _load_segment_items(project_dir: Path) -> list[dict[str, object]]:
    segments_path = _resolve_translation_segments_path(project_dir)
    if not segments_path.exists():
        return []
    try:
        payload = json.loads(segments_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    segments = payload.get("segments", [])
    if not isinstance(segments, list):
        return []

    segment_items: list[dict[str, object]] = []
    for item in segments:
        if not isinstance(item, dict):
            continue
        segment_id = item.get("segment_id")
        segment_items.append(
            {
                "segment_id": int(segment_id) if isinstance(segment_id, int) else segment_id,
                "speaker_id": _normalize_optional_text(item.get("speaker_id")) or "",
                "display_name": _normalize_optional_text(item.get("display_name")) or "Unknown speaker",
                "source_text": _normalize_optional_text(item.get("source_text")) or "",
                "cn_text": _normalize_optional_text(item.get("cn_text")) or "",
                "tts_audio_path": _resolve_project_file_path(project_dir, item.get("tts_audio_path")),
                "aligned_audio_path": _resolve_project_file_path(project_dir, item.get("aligned_audio_path")),
                "alignment_method": _normalize_optional_text(item.get("alignment_method")) or "",
                "rewrite_count": int(item.get("rewrite_count") or 0),
                "needs_review": bool(item.get("needs_review")),
                "speaker_confirmed": False,
                "transcript_confirmed": False,
                "translation_confirmed": False,
                "rewrite_requested": False,
                "review_updated_at": "",
                "start_ms": int(item.get("start_ms") or 0),
                "end_ms": int(item.get("end_ms") or 0),
                "actual_duration_ms": int(item.get("actual_duration_ms") or 0),
                "target_duration_ms": int(item.get("target_duration_ms") or 0),
            }
        )
        segment_items[-1]["has_audio_preview"] = bool(
            segment_items[-1].get("tts_audio_path") or segment_items[-1].get("aligned_audio_path")
        )
    segment_items.sort(key=_segment_item_sort_key)
    return segment_items


def _segment_item_sort_key(item: dict[str, object]) -> int:
    try:
        return int(item.get("segment_id") or 0)
    except (TypeError, ValueError):
        return 0


def _build_segment_speaker_options(items: list[dict[str, object]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    options: list[dict[str, str]] = []
    for item in items:
        speaker_id = str(item.get("speaker_id") or "").strip()
        display_name = str(item.get("display_name") or "").strip()
        key = (speaker_id, display_name)
        if not speaker_id or key in seen:
            continue
        seen.add(key)
        options.append(
            {
                "value": speaker_id,
                "label": display_name or speaker_id,
            }
        )
    options.sort(key=lambda item: item["label"].lower())
    return options
