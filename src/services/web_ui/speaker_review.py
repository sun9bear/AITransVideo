from __future__ import annotations

import json
from pathlib import Path

from services.review_state import (
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_PENDING,
    SPEAKER_REVIEW_STAGE,
    TRANSLATION_REVIEW_STAGE,
    ReviewStateManager,
    utc_now_iso,
)

from .config_helpers import _normalize_optional_text
from .output_entries import _resolve_transcript_structured_path, _resolve_translation_segments_path
from .review_state_helpers import _load_review_stage_payload
from .segment_loader import _load_segment_items, _segment_item_sort_key


def _read_speaker_review_mappings(project_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    speaker_names, segment_speakers, _confirmations = _read_speaker_review_state(project_dir)
    return speaker_names, segment_speakers


def _read_speaker_review_state(
    project_dir: Path,
) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, object]]]:
    speaker_review_payload = _load_review_stage_payload(project_dir, SPEAKER_REVIEW_STAGE) or {}
    speaker_names = speaker_review_payload.get("speaker_names", {})
    segment_speakers = speaker_review_payload.get("segment_speakers", {})
    confirmations = speaker_review_payload.get("confirmations", {})
    if not isinstance(speaker_names, dict):
        speaker_names = {}
    if not isinstance(segment_speakers, dict):
        segment_speakers = {}
    if not isinstance(confirmations, dict):
        confirmations = {}
    return (
        {
            str(speaker_id): str(display_name)
            for speaker_id, display_name in speaker_names.items()
            if _normalize_optional_text(speaker_id) is not None
            and _normalize_optional_text(display_name) is not None
        },
        {
            str(segment_id): str(speaker_id)
            for segment_id, speaker_id in segment_speakers.items()
            if _normalize_optional_text(segment_id) is not None
            and _normalize_optional_text(speaker_id) is not None
        },
        {
            str(segment_id): {
                "speaker_confirmed": bool(raw_entry.get("speaker_confirmed")),
                "transcript_confirmed": bool(raw_entry.get("transcript_confirmed")),
                "updated_at": _normalize_optional_text(raw_entry.get("updated_at")) or "",
            }
            for segment_id, raw_entry in confirmations.items()
            if _normalize_optional_text(segment_id) is not None and isinstance(raw_entry, dict)
        },
    )


def _apply_speaker_review_overrides(
    items: list[dict[str, object]],
    *,
    speaker_names: dict[str, str],
    segment_speakers: dict[str, str],
    confirmations: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    confirmations = confirmations or {}
    for item in items:
        segment_id = str(item.get("segment_id") or "").strip()
        reviewed_speaker_id = _normalize_optional_text(segment_speakers.get(segment_id))
        if reviewed_speaker_id is not None:
            item["speaker_id"] = reviewed_speaker_id
        reviewed_display_name = _normalize_optional_text(speaker_names.get(str(item.get("speaker_id") or "")))
        if reviewed_display_name is not None:
            item["display_name"] = reviewed_display_name
        confirmation_entry = confirmations.get(segment_id)
        if isinstance(confirmation_entry, dict):
            item["speaker_confirmed"] = bool(confirmation_entry.get("speaker_confirmed"))
            item["transcript_confirmed"] = bool(confirmation_entry.get("transcript_confirmed"))
            item["review_updated_at"] = _normalize_optional_text(confirmation_entry.get("updated_at")) or ""
    return items


def _load_transcript_review_items(project_dir: Path) -> list[dict[str, object]]:
    segment_items = _load_segment_items(project_dir)
    speaker_names, segment_speakers, confirmations = _read_speaker_review_state(project_dir)
    if segment_items:
        return _apply_speaker_review_overrides(
            segment_items,
            speaker_names=speaker_names,
            segment_speakers=segment_speakers,
            confirmations=confirmations,
        )

    transcript_path = _resolve_transcript_structured_path(project_dir)
    if not transcript_path.exists():
        return []
    try:
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    lines = payload.get("lines", [])
    if not isinstance(lines, list):
        return []

    transcript_items: list[dict[str, object]] = []
    for raw_line in lines:
        if not isinstance(raw_line, dict):
            continue
        raw_segment_id = raw_line.get("index")
        segment_id = int(raw_segment_id) if isinstance(raw_segment_id, int) else raw_segment_id
        speaker_id = _normalize_optional_text(segment_speakers.get(str(segment_id))) or _normalize_optional_text(
            raw_line.get("speaker_id")
        ) or "speaker_a"
        display_name = _normalize_optional_text(speaker_names.get(speaker_id)) or speaker_id
        start_ms = int(raw_line.get("start_ms") or 0)
        end_ms = int(raw_line.get("end_ms") or 0)
        transcript_items.append(
            {
                "segment_id": segment_id,
                "speaker_id": speaker_id,
                "display_name": display_name,
                "source_text": _normalize_optional_text(raw_line.get("source_text")) or "",
                "cn_text": "",
                "tts_audio_path": None,
                "aligned_audio_path": None,
                "alignment_method": "",
                "rewrite_count": 0,
                "needs_review": False,
                "speaker_confirmed": False,
                "transcript_confirmed": False,
                "translation_confirmed": False,
                "rewrite_requested": False,
                "review_updated_at": "",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "actual_duration_ms": max(0, end_ms - start_ms),
                "target_duration_ms": max(0, end_ms - start_ms),
                "has_audio_preview": False,
            }
        )
    transcript_items.sort(key=_segment_item_sort_key)
    return transcript_items


def _load_translation_review_items(project_dir: Path) -> list[dict[str, object]]:
    segment_items = _load_segment_items(project_dir)
    if not segment_items:
        return []

    speaker_names, segment_speakers, speaker_confirmations = _read_speaker_review_state(project_dir)
    translation_review_payload = _load_review_stage_payload(project_dir, TRANSLATION_REVIEW_STAGE) or {}
    translation_segments = translation_review_payload.get("segments", {})
    if not isinstance(translation_segments, dict):
        translation_segments = {}

    _apply_speaker_review_overrides(
        segment_items,
        speaker_names=speaker_names,
        segment_speakers=segment_speakers,
        confirmations=speaker_confirmations,
    )

    for item in segment_items:
        translation_override = translation_segments.get(str(item.get("segment_id")))
        if isinstance(translation_override, dict):
            cn_text = _normalize_optional_text(translation_override.get("cn_text"))
            if cn_text is not None:
                item["cn_text"] = cn_text
            item["translation_confirmed"] = bool(translation_override.get("translation_confirmed"))
            item["rewrite_requested"] = bool(translation_override.get("rewrite_requested"))
            item["review_updated_at"] = _normalize_optional_text(translation_override.get("updated_at")) or ""
    return segment_items


def _load_transcript_payload(project_dir: Path) -> dict[str, object]:
    transcript_path = _resolve_transcript_structured_path(project_dir)
    if not transcript_path.exists():
        raise ValueError("\u5f53\u524d\u9879\u76ee\u8fd8\u6ca1\u6709\u53ef\u786e\u8ba4\u7684 transcript.json\u3002")
    try:
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"\u65e0\u6cd5\u8bfb\u53d6 transcript.json\uff1a{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("transcript.json \u7ed3\u6784\u65e0\u6548\u3002")
    lines = payload.get("lines")
    if not isinstance(lines, list):
        raise ValueError("transcript.json \u7f3a\u5c11 lines \u5217\u8868\u3002")
    return payload


def _load_translation_segments_payload(project_dir: Path) -> dict[str, object]:
    segments_path = _resolve_translation_segments_path(project_dir)
    if not segments_path.exists():
        raise ValueError("Current project does not have translation/segments.json yet.")
    try:
        payload = json.loads(segments_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read segments.json: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("segments.json has an invalid structure.")
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise ValueError("segments.json is missing the segments list.")
    return payload


def _normalize_translation_review_submission(
    *,
    translation_payload: dict[str, object],
    translation_segments_payload: object,
) -> dict[str, object]:
    segments = translation_payload.get("segments", [])
    if not isinstance(segments, list):
        raise ValueError("segments.json is missing the segments list.")

    submitted_segments = translation_segments_payload if isinstance(translation_segments_payload, dict) else {}
    normalized_segments: dict[str, dict[str, object]] = {}
    for raw_segment in segments:
        if not isinstance(raw_segment, dict):
            continue
        raw_segment_id = raw_segment.get("segment_id")
        segment_id = str(raw_segment_id or "").strip()
        if not segment_id:
            continue

        submitted_entry = submitted_segments.get(segment_id, {})
        if not isinstance(submitted_entry, dict):
            submitted_entry = {}

        cn_text = (
            _normalize_optional_text(submitted_entry.get("cn_text"))
            or _normalize_optional_text(raw_segment.get("cn_text"))
            or ""
        )

        normalized_segments[segment_id] = {
            "segment_id": raw_segment_id,
            "speaker_id": _normalize_optional_text(raw_segment.get("speaker_id")) or "",
            "display_name": _normalize_optional_text(raw_segment.get("display_name")) or "",
            "source_text": _normalize_optional_text(raw_segment.get("source_text")) or "",
            "cn_text": cn_text,
            "target_duration_ms": int(raw_segment.get("target_duration_ms") or 0),
            "rewrite_count": int(raw_segment.get("rewrite_count") or 0),
            "needs_review": bool(raw_segment.get("needs_review")),
            "translation_confirmed": bool(submitted_entry.get("translation_confirmed")),
            "rewrite_requested": bool(submitted_entry.get("rewrite_requested")),
            "updated_at": _normalize_optional_text(submitted_entry.get("updated_at")) or utc_now_iso(),
        }

    return {
        "segments": normalized_segments,
        "segment_count": len(normalized_segments),
    }


def _normalize_speaker_review_submission(
    *,
    transcript_payload: dict[str, object],
    speaker_names_payload: object,
    segment_speakers_payload: object,
    review_confirmations_payload: object,
) -> dict[str, object]:
    lines = transcript_payload.get("lines", [])
    if not isinstance(lines, list):
        raise ValueError("transcript.json \u7f3a\u5c11 lines \u5217\u8868\u3002")

    discovered_speaker_ids: list[str] = []
    discovered_segment_ids: set[str] = set()
    for raw_line in lines:
        if not isinstance(raw_line, dict):
            continue
        segment_id = str(raw_line.get("index") or "").strip()
        speaker_id = _normalize_optional_text(raw_line.get("speaker_id")) or "speaker_a"
        if segment_id:
            discovered_segment_ids.add(segment_id)
        if speaker_id not in discovered_speaker_ids:
            discovered_speaker_ids.append(speaker_id)

    if not discovered_speaker_ids:
        discovered_speaker_ids = ["speaker_a"]

    normalized_speaker_names: dict[str, str] = {}
    if isinstance(speaker_names_payload, dict):
        for speaker_id in discovered_speaker_ids:
            normalized_speaker_names[speaker_id] = (
                _normalize_optional_text(speaker_names_payload.get(speaker_id)) or speaker_id
            )
    else:
        for speaker_id in discovered_speaker_ids:
            normalized_speaker_names[speaker_id] = speaker_id

    normalized_segment_speakers: dict[str, str] = {}
    if isinstance(segment_speakers_payload, dict):
        for segment_id, speaker_id in segment_speakers_payload.items():
            normalized_segment_id = str(segment_id or "").strip()
            normalized_speaker_id = _normalize_optional_text(speaker_id)
            if (
                normalized_segment_id
                and normalized_segment_id in discovered_segment_ids
                and normalized_speaker_id in discovered_speaker_ids
            ):
                normalized_segment_speakers[normalized_segment_id] = normalized_speaker_id

    for raw_line in lines:
        if not isinstance(raw_line, dict):
            continue
        segment_id = str(raw_line.get("index") or "").strip()
        speaker_id = _normalize_optional_text(raw_line.get("speaker_id")) or "speaker_a"
        if segment_id and segment_id not in normalized_segment_speakers:
            normalized_segment_speakers[segment_id] = speaker_id

    normalized_confirmations: dict[str, dict[str, object]] = {}
    if isinstance(review_confirmations_payload, dict):
        for segment_id, raw_entry in review_confirmations_payload.items():
            normalized_segment_id = str(segment_id or "").strip()
            if not normalized_segment_id or normalized_segment_id not in discovered_segment_ids:
                continue
            if not isinstance(raw_entry, dict):
                continue
            normalized_confirmations[normalized_segment_id] = {
                "speaker_confirmed": bool(raw_entry.get("speaker_confirmed")),
                "transcript_confirmed": bool(raw_entry.get("transcript_confirmed")),
                "updated_at": _normalize_optional_text(raw_entry.get("updated_at")) or utc_now_iso(),
            }

    return {
        "speaker_names": normalized_speaker_names,
        "speaker_options": [
            {"speaker_id": speaker_id, "display_name": normalized_speaker_names[speaker_id]}
            for speaker_id in discovered_speaker_ids
        ],
        "segment_speakers": normalized_segment_speakers,
        "confirmations": normalized_confirmations,
        "segment_count": len(discovered_segment_ids),
    }


def _write_approved_speaker_review_to_transcript(
    *,
    project_dir: Path,
    normalized_payload: dict[str, object],
) -> None:
    transcript_path = project_dir / "transcript" / "transcript.json"
    transcript_payload = _load_transcript_payload(project_dir)
    segment_speakers = normalized_payload.get("segment_speakers", {})
    speaker_names = normalized_payload.get("speaker_names", {})
    if not isinstance(segment_speakers, dict):
        segment_speakers = {}
    if not isinstance(speaker_names, dict):
        speaker_names = {}
    lines = transcript_payload.get("lines", [])
    if isinstance(lines, list):
        for raw_line in lines:
            if not isinstance(raw_line, dict):
                continue
            segment_id = str(raw_line.get("index") or "").strip()
            reviewed_speaker_id = _normalize_optional_text(segment_speakers.get(segment_id))
            if reviewed_speaker_id is not None:
                raw_line["speaker_id"] = reviewed_speaker_id
            reviewed_speaker_name = _normalize_optional_text(speaker_names.get(str(raw_line.get("speaker_id") or "")))
            if reviewed_speaker_name is not None:
                raw_line["speaker_name"] = reviewed_speaker_name
    transcript_path.write_text(
        json.dumps(transcript_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _save_speaker_review_submission(
    *,
    project_dir: Path,
    speaker_names_payload: object,
    segment_speakers_payload: object,
    review_confirmations_payload: object,
    status: str,
) -> dict[str, object]:
    transcript_payload = _load_transcript_payload(project_dir)
    normalized_payload = _normalize_speaker_review_submission(
        transcript_payload=transcript_payload,
        speaker_names_payload=speaker_names_payload,
        segment_speakers_payload=segment_speakers_payload,
        review_confirmations_payload=review_confirmations_payload,
    )
    review_state_manager = ReviewStateManager(project_dir / "review_state.json")
    activate = status == REVIEW_STATUS_PENDING
    review_state_manager.set_stage(
        SPEAKER_REVIEW_STAGE,
        status=status,
        payload=normalized_payload,
        activate=activate,
    )
    if status == REVIEW_STATUS_APPROVED:
        _write_approved_speaker_review_to_transcript(
            project_dir=project_dir,
            normalized_payload=normalized_payload,
        )
    return normalized_payload
