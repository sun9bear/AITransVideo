"""Bulk terminology replacement for the post-edit buffer.

The flow is intentionally two-step:

1. preview: compute affected segments, before/after text, speaker, and the
   voice metadata that the post-edit TTS bridge will use.
2. apply: re-compute the preview, verify it still matches the user's confirmed
   snapshot, then update text and mark only those segments text_dirty.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from services._file_lock import file_lock
from services.jobs.editing import EDITING_SUBDIR, EditingConflictError
from services.jobs.editing_segments import (
    SEGMENT_STATUS_TEXT_DIRTY,
    _atomic_write_json,
    _editing_lock_anchor,
    _segment_status_path,
    _segments_path,
    load_editing_segments,
    load_segment_status,
)
from services.jobs.editing_speakers import load_baseline_speakers, load_speakers
from services.jobs.editing_voice_map import load_voice_map

__all__ = [
    "apply_bulk_replace_terms",
    "preview_bulk_replace_terms",
]

_SUPPORTED_FIELDS = frozenset({"cn_text"})


def _normalise_request(
    *,
    find: str,
    replace: str,
    field: str,
) -> tuple[str, str, str]:
    field = str(field or "cn_text").strip() or "cn_text"
    if field not in _SUPPORTED_FIELDS:
        raise ValueError(f"unsupported bulk replace field: {field!r}")
    find = str(find or "")
    replace = str(replace or "")
    if not find:
        raise ValueError("find must be non-empty")
    if find == replace:
        raise ValueError("find and replace must be different")
    return find, replace, field


def _speaker_display_names(project_dir: str | Path) -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        for item in load_baseline_speakers(project_dir):
            sid = str(item.get("speaker_id") or "").strip()
            display = str(item.get("display_name") or "").strip()
            if sid and display:
                names[sid] = display
    except Exception:
        pass
    try:
        for speaker in load_speakers(project_dir):
            sid = str(speaker.speaker_id or "").strip()
            display = str(speaker.display_name or "").strip()
            if sid and display:
                names[sid] = display
    except Exception:
        pass
    return names


def _representative_voice(
    segments: list[dict[str, Any]],
    target_index: int,
    speaker_id: str,
) -> dict[str, str | None]:
    rep_voice_id: str | None = None
    rep_provider: str | None = None
    rep_model: str | None = None
    for index, candidate in enumerate(segments):
        if index == target_index or not isinstance(candidate, dict):
            continue
        if candidate.get("speaker_id") != speaker_id:
            continue
        if rep_voice_id is None:
            voice = candidate.get("voice_id")
            if isinstance(voice, str) and voice:
                rep_voice_id = voice
        if rep_provider is None:
            provider = candidate.get("tts_provider") or candidate.get("provider")
            if isinstance(provider, str) and provider:
                rep_provider = provider
        if rep_model is None:
            model = candidate.get("tts_model_key")
            if isinstance(model, str) and model:
                rep_model = model
        if rep_voice_id is not None and rep_provider is not None and rep_model is not None:
            break
    return {
        "voice_id": rep_voice_id,
        "provider": rep_provider,
        "tts_model_key": rep_model,
    }


def _voice_for_segment(
    *,
    segments: list[dict[str, Any]],
    index: int,
    segment: dict[str, Any],
    voice_map: dict[str, dict[str, Any]],
) -> dict[str, str | None]:
    sid = str(segment.get("segment_id") or "")
    override = voice_map.get(sid)
    if override:
        return {
            "provider": str(override.get("provider") or "") or None,
            "voice_id": str(override.get("voice_id") or "") or None,
            "tts_model_key": str(override.get("tts_model_key") or "") or None,
        }

    speaker_id = str(segment.get("speaker_id") or "")
    rep = _representative_voice(segments, index, speaker_id) if speaker_id else {}
    provider = (
        rep.get("provider")
        or segment.get("tts_provider")
        or segment.get("provider")
        or None
    )
    voice_id = rep.get("voice_id") or segment.get("voice_id") or None
    model = rep.get("tts_model_key") or segment.get("tts_model_key") or None
    return {
        "provider": str(provider) if provider else None,
        "voice_id": str(voice_id) if voice_id else None,
        "tts_model_key": str(model) if model else None,
    }


def _build_preview(
    project_dir: str | Path,
    *,
    segments: list[dict[str, Any]],
    find: str,
    replace: str,
    field: str,
) -> dict[str, Any]:
    voice_map = load_voice_map(project_dir)
    names = _speaker_display_names(project_dir)
    matches: list[dict[str, Any]] = []
    total_matches = 0

    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        sid_value = segment.get("segment_id")
        if sid_value is None:
            continue
        before_text = str(segment.get(field) or "")
        match_count = before_text.count(find)
        if match_count <= 0:
            continue
        after_text = before_text.replace(find, replace)
        speaker_id = str(segment.get("speaker_id") or "")
        voice = _voice_for_segment(
            segments=segments,
            index=index,
            segment=segment,
            voice_map=voice_map,
        )
        total_matches += match_count
        matches.append(
            {
                "segment_id": str(sid_value),
                "speaker_id": speaker_id,
                "speaker_display_name": names.get(speaker_id) or "",
                "provider": voice.get("provider"),
                "voice_id": voice.get("voice_id"),
                "tts_model_key": voice.get("tts_model_key"),
                "match_count": match_count,
                "before_text": before_text,
                "after_text": after_text,
                "start_ms": segment.get("start_ms"),
                "end_ms": segment.get("end_ms"),
            }
        )

    return {
        "field": field,
        "find": find,
        "replace": replace,
        "segment_count": len(matches),
        "total_matches": total_matches,
        "matches": matches,
    }


def preview_bulk_replace_terms(
    project_dir: str | Path,
    *,
    find: str,
    replace: str,
    field: str = "cn_text",
) -> dict[str, Any]:
    find, replace, field = _normalise_request(
        find=find,
        replace=replace,
        field=field,
    )
    segments = load_editing_segments(project_dir)
    return _build_preview(
        project_dir,
        segments=segments,
        find=find,
        replace=replace,
        field=field,
    )


def _normalise_expected_segment_ids(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("expected_segment_ids must be a list")
    return [str(item).strip() for item in value if str(item).strip()]


def _normalise_expected_before_texts(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("expected_matches must be a list")
    expected: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("expected_matches items must be objects")
        sid = str(item.get("segment_id") or "").strip()
        if not sid:
            raise ValueError("expected_matches items must include segment_id")
        if "before_text" not in item:
            raise ValueError("expected_matches items must include before_text")
        expected[sid] = str(item.get("before_text") or "")
    return expected


def apply_bulk_replace_terms(
    project_dir: str | Path,
    *,
    find: str,
    replace: str,
    field: str = "cn_text",
    expected_segment_ids: object = None,
    expected_total_matches: int | None = None,
    expected_matches: object = None,
) -> dict[str, Any]:
    find, replace, field = _normalise_request(
        find=find,
        replace=replace,
        field=field,
    )
    expected_ids = _normalise_expected_segment_ids(expected_segment_ids)
    expected_before_texts = _normalise_expected_before_texts(expected_matches)
    project_path = Path(project_dir)

    with file_lock(_editing_lock_anchor(project_path)):
        segments = load_editing_segments(project_path)
        preview = _build_preview(
            project_path,
            segments=segments,
            find=find,
            replace=replace,
            field=field,
        )
        current_ids = [str(item["segment_id"]) for item in preview["matches"]]
        if expected_ids is not None and current_ids != expected_ids:
            raise EditingConflictError(
                "bulk replace preview is stale; refresh the preview before applying"
            )
        if (
            expected_total_matches is not None
            and int(expected_total_matches) != int(preview["total_matches"])
        ):
            raise EditingConflictError(
                "bulk replace match count changed; refresh the preview before applying"
            )
        if expected_before_texts is not None:
            current_before_texts = {
                str(item["segment_id"]): str(item["before_text"])
                for item in preview["matches"]
            }
            if (
                set(expected_before_texts) != set(current_before_texts)
                or any(
                    expected_before_texts[sid] != current_before_texts[sid]
                    for sid in expected_before_texts
                )
            ):
                raise EditingConflictError(
                    "bulk replace matched text changed; refresh the preview before applying"
                )
        if not current_ids:
            return {
                **preview,
                "replaced_segment_ids": [],
                "segments": segments,
                "segment_status": load_segment_status(project_path),
            }

        by_id = {str(item["segment_id"]): item for item in preview["matches"]}
        next_segments: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                next_segments.append(segment)
                continue
            sid = str(segment.get("segment_id"))
            replacement = by_id.get(sid)
            if replacement is None:
                next_segments.append(segment)
                continue
            next_segment = dict(segment)
            next_segment[field] = replacement["after_text"]
            next_segments.append(next_segment)

        _atomic_write_json(_segments_path(project_path), next_segments)

        status = load_segment_status(project_path)
        for sid in current_ids:
            status[sid] = SEGMENT_STATUS_TEXT_DIRTY
            draft = project_path / EDITING_SUBDIR / "tts_segments_draft" / f"{sid}.wav"
            if draft.exists():
                draft.unlink()
        _atomic_write_json(_segment_status_path(project_path), status)

    return {
        **preview,
        "replaced_segment_ids": current_ids,
        "segments": next_segments,
        "segment_status": status,
    }
