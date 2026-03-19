from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


PROJECT_STATE_STAGE_ORDER = (
    "ingestion",
    "audio_preparation",
    "media_understanding",
    "translation",
    "alignment",
    "draft",
    "legacy_process_output",
)
PROJECT_STATE_STAGE_ORDER_INDEX = {
    stage_name: index for index, stage_name in enumerate(PROJECT_STATE_STAGE_ORDER)
}
PROJECT_STATE_STAGE_LABELS = {
    "ingestion": "Ingestion",
    "audio_preparation": "Audio Preparation",
    "media_understanding": "Media Understanding",
    "translation": "Translation",
    "alignment": "Alignment",
    "draft": "Draft",
    "legacy_process_output": "Legacy Process Output",
}
PROJECT_STATE_STATUS_LABELS = {
    "pending": "Pending",
    "running": "Running",
    "done": "Done",
    "failed": "Failed",
}


def build_empty_project_state_summary(*, state_path: str | None = None) -> dict[str, object]:
    return {
        "available": False,
        "path": state_path,
        "load_error": None,
        "project_id": None,
        "overall_status": None,
        "overall_status_label": None,
        "latest_stage_name": None,
        "latest_stage_label": None,
        "latest_stage_status": None,
        "latest_stage_status_label": None,
        "stage_count": 0,
        "completed_stage_count": 0,
        "running_stage_count": 0,
        "failed_stage_count": 0,
        "stages": [],
    }


def build_project_state_summary(
    state: dict[str, Any] | None,
    *,
    state_path: str | None = None,
) -> dict[str, object]:
    summary = build_empty_project_state_summary(state_path=state_path)
    raw_state = state if isinstance(state, dict) else {}
    raw_stages = raw_state.get("stages", {})
    if not isinstance(raw_stages, dict):
        raw_stages = {}

    ordered_stage_names = [name for name in PROJECT_STATE_STAGE_ORDER if name in raw_stages]
    ordered_stage_names.extend(sorted(name for name in raw_stages if name not in PROJECT_STATE_STAGE_ORDER_INDEX))

    stage_entries: list[dict[str, object]] = []
    for stage_name in ordered_stage_names:
        raw_stage = raw_stages.get(stage_name)
        if not isinstance(raw_stage, dict):
            continue
        payload = raw_stage.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        status = _normalize_optional_text(raw_stage.get("status")) or "pending"
        artifact_payload = payload.get("artifacts", {})
        if not isinstance(artifact_payload, dict):
            artifact_payload = {}
        artifact_count = _coerce_non_negative_int(artifact_payload.get("file_count"))
        error_message = _normalize_optional_text(raw_stage.get("error_message"))
        stage_entries.append(
            {
                "name": stage_name,
                "label": PROJECT_STATE_STAGE_LABELS.get(stage_name, stage_name),
                "status": status,
                "status_label": PROJECT_STATE_STATUS_LABELS.get(status, status),
                "execution_mode": _normalize_optional_text(payload.get("execution_mode")) or "",
                "artifact_count": artifact_count,
                "updated_at": _normalize_optional_text(raw_stage.get("updated_at")),
                "error_message": error_message,
                "summary": _build_project_state_stage_summary(
                    stage_name=stage_name,
                    payload=payload,
                    artifact_count=artifact_count,
                    error_message=error_message,
                ),
            }
        )

    completed_stage_count = sum(1 for entry in stage_entries if entry.get("status") == "done")
    running_stage_count = sum(1 for entry in stage_entries if entry.get("status") == "running")
    failed_stage_count = sum(1 for entry in stage_entries if entry.get("status") == "failed")
    latest_stage = max(stage_entries, key=_project_state_stage_sort_key, default=None)
    overall_status = _resolve_project_state_overall_status(
        running_stage_count=running_stage_count,
        failed_stage_count=failed_stage_count,
        completed_stage_count=completed_stage_count,
        stage_count=len(stage_entries),
    )

    summary.update(
        {
            "available": True,
            "project_id": _normalize_optional_text(raw_state.get("project_id")),
            "overall_status": overall_status,
            "overall_status_label": PROJECT_STATE_STATUS_LABELS.get(overall_status, overall_status),
            "latest_stage_name": latest_stage.get("name") if latest_stage else None,
            "latest_stage_label": latest_stage.get("label") if latest_stage else None,
            "latest_stage_status": latest_stage.get("status") if latest_stage else None,
            "latest_stage_status_label": latest_stage.get("status_label") if latest_stage else None,
            "stage_count": len(stage_entries),
            "completed_stage_count": completed_stage_count,
            "running_stage_count": running_stage_count,
            "failed_stage_count": failed_stage_count,
            "stages": stage_entries,
        }
    )
    return summary


def build_stage_execution_summary(stage_snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    raw_stages = stage_snapshot if isinstance(stage_snapshot, dict) else {}
    project_state_summary = build_project_state_summary({"stages": raw_stages})
    summarized_stages = {
        str(stage_entry.get("name")): stage_entry
        for stage_entry in project_state_summary.get("stages", [])
        if isinstance(stage_entry, dict) and _normalize_optional_text(stage_entry.get("name")) is not None
    }

    stage_execution_summary: dict[str, dict[str, Any]] = {}
    for stage_name, stage_data in raw_stages.items():
        if not isinstance(stage_data, dict):
            continue
        payload = stage_data.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        summarized_stage = summarized_stages.get(stage_name, {})
        artifact_payload = payload.get("artifacts", {})
        if not isinstance(artifact_payload, dict):
            artifact_payload = {}
        stage_execution_summary[stage_name] = {
            "label": summarized_stage.get("label"),
            "status": stage_data.get("status"),
            "status_label": summarized_stage.get("status_label"),
            "summary": summarized_stage.get("summary"),
            "execution_mode": payload.get("execution_mode"),
            "cache_hit_batches": payload.get("cache_hit_batches"),
            "cache_hit_blocks": payload.get("cache_hit_blocks"),
            "skipped": payload.get("skipped"),
            "authoritative_input_used": payload.get("authoritative_input_used"),
            "authoritative_path_kind": payload.get("authoritative_path_kind"),
            "authoritative_flow": payload.get("authoritative_flow"),
            "transcript_extraction_used": payload.get("transcript_extraction_used"),
            "attributed_transcript_normalized": payload.get("attributed_transcript_normalized"),
            "subtitle_line_bridge_applied": payload.get("subtitle_line_bridge_applied"),
            "literal_text_layer_produced": payload.get("literal_text_layer_produced"),
            "tts_text_layer_produced": payload.get("tts_text_layer_produced"),
            "text_layer_summary": payload.get("text_layer_summary"),
            "artifact_count": artifact_payload.get("file_count"),
        }
    return stage_execution_summary


def _build_project_state_stage_summary(
    *,
    stage_name: str,
    payload: dict[str, object],
    artifact_count: int,
    error_message: str | None,
) -> str:
    summary_parts: list[str] = []
    execution_mode = _normalize_optional_text(payload.get("execution_mode"))
    if execution_mode is not None:
        summary_parts.append(execution_mode)

    if stage_name == "media_understanding":
        line_count = _coerce_non_negative_int(payload.get("line_count"))
        speaker_count = _coerce_non_negative_int(payload.get("speaker_count"))
        if line_count > 0:
            summary_parts.append(f"{line_count} lines")
        if speaker_count > 0:
            summary_parts.append(f"{speaker_count} speakers")
    elif stage_name == "translation":
        segment_count = _coerce_non_negative_int(payload.get("segment_count"))
        if segment_count > 0:
            summary_parts.append(f"{segment_count} segments")
    elif stage_name == "alignment":
        block_count = _coerce_non_negative_int(payload.get("block_count"))
        needs_review_count = _coerce_non_negative_int(payload.get("needs_review_count"))
        if block_count > 0:
            summary_parts.append(f"{block_count} blocks")
        if needs_review_count > 0:
            summary_parts.append(f"{needs_review_count} needs review")
    elif stage_name == "legacy_process_output":
        segment_count = _coerce_non_negative_int(payload.get("segment_count"))
        needs_review_count = _coerce_non_negative_int(payload.get("needs_review_count"))
        if segment_count > 0:
            summary_parts.append(f"{segment_count} segments")
        if needs_review_count > 0:
            summary_parts.append(f"{needs_review_count} needs review")

    if artifact_count > 0:
        summary_parts.append(f"{artifact_count} artifacts")
    if error_message:
        summary_parts.append(f"error: {error_message}")
    return " | ".join(summary_parts)


def _project_state_stage_sort_key(stage_entry: dict[str, object]) -> tuple[datetime, int]:
    updated_at = _parse_iso_datetime(_normalize_optional_text(stage_entry.get("updated_at")))
    if updated_at is None:
        updated_at = datetime.min.replace(tzinfo=UTC)
    stage_name = _normalize_optional_text(stage_entry.get("name")) or ""
    return (updated_at, PROJECT_STATE_STAGE_ORDER_INDEX.get(stage_name, len(PROJECT_STATE_STAGE_ORDER_INDEX)))


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _resolve_project_state_overall_status(
    *,
    running_stage_count: int,
    failed_stage_count: int,
    completed_stage_count: int,
    stage_count: int,
) -> str | None:
    if running_stage_count > 0:
        return "running"
    if failed_stage_count > 0:
        return "failed"
    if stage_count > 0 and completed_stage_count == stage_count:
        return "done"
    if stage_count > 0:
        return "pending"
    return None


def _coerce_non_negative_int(value: object) -> int:
    try:
        normalized = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, normalized)


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
