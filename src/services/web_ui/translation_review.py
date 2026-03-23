from __future__ import annotations

import json
from pathlib import Path

from services.review_state import (
    REVIEW_STATUS_APPROVED,
    REVIEW_STATUS_PENDING,
    SPEAKER_REVIEW_STAGE,
    TRANSLATION_REVIEW_STAGE,
    ReviewStateManager,
)

from .config_helpers import _normalize_optional_text
from .output_entries import _resolve_translation_segments_path
from .review_state_helpers import _load_review_stage_payload
from .speaker_review import (
    _load_translation_segments_payload,
    _normalize_translation_review_submission,
)


def _write_approved_translation_review_to_segments(
    *,
    project_dir: Path,
    normalized_payload: dict[str, object],
) -> None:
    segments_path = project_dir / "translation" / "segments.json"
    translation_payload = _load_translation_segments_payload(project_dir)
    reviewed_segments = normalized_payload.get("segments", {})
    if not isinstance(reviewed_segments, dict):
        reviewed_segments = {}

    segments = translation_payload.get("segments", [])
    if isinstance(segments, list):
        for raw_segment in segments:
            if not isinstance(raw_segment, dict):
                continue
            segment_id = str(raw_segment.get("segment_id") or "").strip()
            reviewed_segment = reviewed_segments.get(segment_id)
            if not isinstance(reviewed_segment, dict):
                continue
            raw_segment["cn_text"] = _normalize_optional_text(reviewed_segment.get("cn_text")) or ""
            raw_segment["tts_cn_text"] = (
                _normalize_optional_text(reviewed_segment.get("tts_cn_text"))
                or raw_segment["cn_text"]
            )

    segments_path.write_text(
        json.dumps(translation_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _apply_segment_speakers_update_from_translation_review(
    *,
    project_dir: Path,
    segment_speakers_update: dict[str, str],
) -> None:
    """Apply speaker re-assignments made during translation review back to
    the speaker_review stage payload and the transcript file."""
    review_state_manager = ReviewStateManager(project_dir / "review_state.json")
    speaker_review_payload = _load_review_stage_payload(project_dir, SPEAKER_REVIEW_STAGE) or {}
    existing_segment_speakers = speaker_review_payload.get("segment_speakers", {})
    if not isinstance(existing_segment_speakers, dict):
        existing_segment_speakers = {}

    # Merge updates
    merged = {**existing_segment_speakers}
    for segment_id, speaker_id in segment_speakers_update.items():
        normalized_id = _normalize_optional_text(segment_id)
        normalized_speaker = _normalize_optional_text(speaker_id)
        if normalized_id and normalized_speaker:
            merged[normalized_id] = normalized_speaker

    speaker_review_payload["segment_speakers"] = merged
    review_state_manager.set_stage(
        SPEAKER_REVIEW_STAGE,
        status=speaker_review_payload.get("status", REVIEW_STATUS_APPROVED),
        payload=speaker_review_payload,
        activate=False,
    )

    # Also update transcript.json so downstream stages see the correct speaker
    transcript_path = project_dir / "transcript" / "transcript.json"
    if transcript_path.exists():
        try:
            transcript_payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            lines = transcript_payload.get("lines", [])
            if isinstance(lines, list):
                for raw_line in lines:
                    if not isinstance(raw_line, dict):
                        continue
                    seg_id = str(raw_line.get("index") or "").strip()
                    new_speaker = _normalize_optional_text(segment_speakers_update.get(seg_id))
                    if new_speaker:
                        raw_line["speaker_id"] = new_speaker
                transcript_path.write_text(
                    json.dumps(transcript_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except (OSError, json.JSONDecodeError):
            pass

    # Also update translation segments file
    segments_path = _resolve_translation_segments_path(project_dir)
    if segments_path.exists():
        try:
            seg_payload = json.loads(segments_path.read_text(encoding="utf-8"))
            segments_list = seg_payload.get("segments", [])
            if isinstance(segments_list, list):
                for item in segments_list:
                    if not isinstance(item, dict):
                        continue
                    seg_id = str(item.get("segment_id") or "").strip()
                    new_speaker = _normalize_optional_text(segment_speakers_update.get(seg_id))
                    if new_speaker:
                        item["speaker_id"] = new_speaker
                segments_path.write_text(
                    json.dumps(seg_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except (OSError, json.JSONDecodeError):
            pass


def _split_segment(
    *,
    project_dir: Path,
    segment_id: object,
    split_source_index: object,
    split_cn_index: object,
    speaker_a: object,
    speaker_b: object,
) -> dict[str, object]:
    """Split a segment into two at the given character indices.

    Updates both transcript.json and translation segments (segments.json).
    Re-numbers all subsequent segment IDs.
    """
    seg_id_str = str(segment_id or "").strip()
    if not seg_id_str:
        raise ValueError("segment_id is required")

    source_split = int(split_source_index) if split_source_index is not None else None
    cn_split = int(split_cn_index) if split_cn_index is not None else None
    speaker_a_id = _normalize_optional_text(speaker_a)
    speaker_b_id = _normalize_optional_text(speaker_b)

    # --- Update transcript.json ---
    transcript_path = project_dir / "transcript" / "transcript.json"
    transcript_updated = False
    if transcript_path.exists():
        try:
            transcript_payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            lines = transcript_payload.get("lines", [])
            if isinstance(lines, list):
                new_lines: list[dict[str, object]] = []
                for raw_line in lines:
                    if not isinstance(raw_line, dict):
                        new_lines.append(raw_line)
                        continue
                    line_index = str(raw_line.get("index") or "").strip()
                    if line_index == seg_id_str and source_split is not None:
                        text = str(raw_line.get("source_text") or raw_line.get("en_text") or "")
                        start_ms = int(raw_line.get("start_ms") or 0)
                        end_ms = int(raw_line.get("end_ms") or 0)
                        total_len = max(len(text), 1)
                        ratio = min(max(source_split / total_len, 0.05), 0.95)
                        mid_ms = start_ms + int((end_ms - start_ms) * ratio)

                        line_a = {**raw_line}
                        line_a["source_text"] = text[:source_split].strip()
                        if "en_text" in line_a:
                            line_a["en_text"] = text[:source_split].strip()
                        line_a["end_ms"] = mid_ms
                        if speaker_a_id:
                            line_a["speaker_id"] = speaker_a_id

                        line_b = {**raw_line}
                        line_b["source_text"] = text[source_split:].strip()
                        if "en_text" in line_b:
                            line_b["en_text"] = text[source_split:].strip()
                        line_b["start_ms"] = mid_ms
                        if speaker_b_id:
                            line_b["speaker_id"] = speaker_b_id

                        new_lines.append(line_a)
                        new_lines.append(line_b)
                        transcript_updated = True
                    else:
                        new_lines.append(raw_line)

                if transcript_updated:
                    # Re-number indices
                    for i, line in enumerate(new_lines):
                        if isinstance(line, dict):
                            line["index"] = i
                    transcript_payload["lines"] = new_lines
                    transcript_path.write_text(
                        json.dumps(transcript_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    # --- Update translation segments (segments.json) ---
    segments_path = _resolve_translation_segments_path(project_dir)
    segments_updated = False
    if segments_path.exists():
        try:
            seg_payload = json.loads(segments_path.read_text(encoding="utf-8"))
            segments_list = seg_payload.get("segments", [])
            if isinstance(segments_list, list):
                new_segments: list[dict[str, object]] = []
                for item in segments_list:
                    if not isinstance(item, dict):
                        new_segments.append(item)
                        continue
                    item_seg_id = str(item.get("segment_id") or "").strip()
                    if item_seg_id == seg_id_str:
                        source_text = str(item.get("source_text") or "")
                        cn_text = str(item.get("cn_text") or "")
                        tts_cn_text = str(item.get("tts_cn_text") or "")
                        start_ms = int(item.get("start_ms") or 0)
                        end_ms = int(item.get("end_ms") or 0)

                        src_split_pos = source_split if source_split is not None else len(source_text) // 2
                        cn_split_pos = cn_split if cn_split is not None else len(cn_text) // 2

                        total_src_len = max(len(source_text), 1)
                        ratio = min(max(src_split_pos / total_src_len, 0.05), 0.95)
                        mid_ms = start_ms + int((end_ms - start_ms) * ratio)
                        target_dur_a = mid_ms - start_ms
                        target_dur_b = end_ms - mid_ms

                        seg_a = {**item}
                        seg_a["source_text"] = source_text[:src_split_pos].strip()
                        seg_a["cn_text"] = cn_text[:cn_split_pos].strip()
                        seg_a["tts_cn_text"] = tts_cn_text[:cn_split_pos].strip()
                        seg_a["end_ms"] = mid_ms
                        seg_a["target_duration_ms"] = target_dur_a
                        seg_a["tts_audio_path"] = None
                        seg_a["aligned_audio_path"] = None
                        seg_a["actual_duration_ms"] = 0
                        seg_a["alignment_ratio"] = 0.0
                        seg_a["alignment_method"] = ""
                        seg_a["rewrite_count"] = 0
                        seg_a["needs_review"] = False
                        if speaker_a_id:
                            seg_a["speaker_id"] = speaker_a_id

                        seg_b = {**item}
                        seg_b["source_text"] = source_text[src_split_pos:].strip()
                        seg_b["cn_text"] = cn_text[cn_split_pos:].strip()
                        seg_b["tts_cn_text"] = tts_cn_text[cn_split_pos:].strip()
                        seg_b["start_ms"] = mid_ms
                        seg_b["target_duration_ms"] = target_dur_b
                        seg_b["tts_audio_path"] = None
                        seg_b["aligned_audio_path"] = None
                        seg_b["actual_duration_ms"] = 0
                        seg_b["alignment_ratio"] = 0.0
                        seg_b["alignment_method"] = ""
                        seg_b["rewrite_count"] = 0
                        seg_b["needs_review"] = False
                        if speaker_b_id:
                            seg_b["speaker_id"] = speaker_b_id

                        new_segments.append(seg_a)
                        new_segments.append(seg_b)
                        segments_updated = True
                    else:
                        new_segments.append(item)

                if segments_updated:
                    # Re-number segment_ids
                    for i, seg in enumerate(new_segments):
                        if isinstance(seg, dict):
                            seg["segment_id"] = i + 1
                    seg_payload["segments"] = new_segments
                    seg_payload["total_segments"] = len(new_segments)
                    segments_path.write_text(
                        json.dumps(seg_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    # Clear the translation_review stage payload completely so the UI
    # reloads from the updated segments.json instead of stale cached data.
    if transcript_updated or segments_updated:
        review_state_manager = ReviewStateManager(project_dir / "review_state.json")
        # Reset to empty payload - forces UI to read fresh from segments.json
        review_state_manager.set_stage(
            TRANSLATION_REVIEW_STAGE,
            status=REVIEW_STATUS_PENDING,
            payload={},
            activate=True,
        )

    return {
        "success": transcript_updated or segments_updated,
        "transcript_updated": transcript_updated,
        "segments_updated": segments_updated,
    }


def _save_translation_review_submission(
    *,
    project_dir: Path,
    translation_segments_payload: object,
    status: str,
) -> dict[str, object]:
    translation_payload = _load_translation_segments_payload(project_dir)
    normalized_payload = _normalize_translation_review_submission(
        translation_payload=translation_payload,
        translation_segments_payload=translation_segments_payload,
    )
    review_state_manager = ReviewStateManager(project_dir / "review_state.json")
    activate = status == REVIEW_STATUS_PENDING
    review_state_manager.set_stage(
        TRANSLATION_REVIEW_STAGE,
        status=status,
        payload=normalized_payload,
        activate=activate,
    )
    if status == REVIEW_STATUS_APPROVED:
        _write_approved_translation_review_to_segments(
            project_dir=project_dir,
            normalized_payload=normalized_payload,
        )
    return normalized_payload
