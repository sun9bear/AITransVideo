from __future__ import annotations

from typing import Any

from services.manifest_reader import load_manifest_payload


def build_empty_source_context_summary() -> dict[str, object]:
    return {
        "source_kind": None,
        "locator": None,
        "video_title": None,
    }


def build_source_context_summary(
    *,
    manifest_path: object | None = None,
    manifest_payload: dict[str, Any] | None = None,
    stage_snapshot: dict[str, Any] | None = None,
    run_context: dict[str, Any] | None = None,
    fallback_locator: object | None = None,
) -> dict[str, object]:
    context = (
        extract_source_context_from_manifest_payload(manifest_payload)
        if isinstance(manifest_payload, dict)
        else read_source_context_from_manifest_path(manifest_path)
    )
    raw_stage_snapshot = stage_snapshot if isinstance(stage_snapshot, dict) else {}
    media_payload = raw_stage_snapshot.get("media_understanding", {}).get("payload", {})
    if context["source_kind"] is None and isinstance(media_payload, dict):
        context["source_kind"] = _normalize_optional_text(media_payload.get("source_kind"))
    if context["locator"] is None:
        context["locator"] = _normalize_optional_text(fallback_locator)
    if context["locator"] is None and isinstance(run_context, dict):
        context["locator"] = _normalize_optional_text(run_context.get("input_path"))
    return context


def read_source_context_from_manifest_path(manifest_path: object) -> dict[str, object]:
    normalized_manifest_path = _normalize_optional_text(manifest_path)
    if normalized_manifest_path is None:
        return build_empty_source_context_summary()
    payload = load_manifest_payload(manifest_path=normalized_manifest_path)
    if payload is None:
        return build_empty_source_context_summary()
    return extract_source_context_from_manifest_payload(payload)


def extract_source_context_from_manifest_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return build_empty_source_context_summary()
    source_info = payload.get("source_info")
    if not isinstance(source_info, dict):
        return build_empty_source_context_summary()
    metadata = source_info.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "source_kind": _normalize_optional_text(source_info.get("source_kind")),
        "locator": (
            _normalize_optional_text(source_info.get("locator"))
            or _normalize_optional_text(source_info.get("source_url"))
            or _normalize_optional_text(source_info.get("url"))
        ),
        "video_title": (
            _normalize_optional_text(metadata.get("video_title"))
            or _normalize_optional_text(metadata.get("title"))
            or _normalize_optional_text(source_info.get("title"))
        ),
    }


def _normalize_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized
