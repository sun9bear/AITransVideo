from __future__ import annotations

from os import PathLike
from typing import Mapping


ArtifactEntry = tuple[str, str | PathLike[str]]


def build_canonical_source_info(
    *,
    source_kind: str,
    locator: str | None = None,
    source_path: str | None = None,
    metadata: Mapping[str, object] | None = None,
    authoritative_input_used: bool | None = None,
    authoritative_path_kind: str | None = None,
    authoritative_flow: str | None = None,
    source_input_hash: str | None = None,
) -> dict[str, object]:
    normalized_source_kind = _normalize_optional_text(source_kind)
    if normalized_source_kind is None:
        raise ValueError("source_kind is required")

    source_info: dict[str, object] = {
        "source_kind": normalized_source_kind,
    }
    normalized_locator = _normalize_optional_text(locator)
    normalized_source_path = _normalize_optional_text(source_path)
    normalized_authoritative_path_kind = _normalize_optional_text(authoritative_path_kind)
    normalized_authoritative_flow = _normalize_optional_text(authoritative_flow)
    normalized_source_input_hash = _normalize_optional_text(source_input_hash)
    normalized_metadata = dict(metadata) if isinstance(metadata, Mapping) and metadata else None

    if normalized_locator is not None:
        source_info["locator"] = normalized_locator
    if normalized_source_path is not None:
        source_info["source_path"] = normalized_source_path
    if normalized_metadata is not None:
        source_info["metadata"] = normalized_metadata
    if isinstance(authoritative_input_used, bool):
        source_info["authoritative_input_used"] = authoritative_input_used
    if normalized_authoritative_path_kind is not None:
        source_info["authoritative_path_kind"] = normalized_authoritative_path_kind
    if normalized_authoritative_flow is not None:
        source_info["authoritative_flow"] = normalized_authoritative_flow
    if normalized_source_input_hash is not None:
        source_info["source_input_hash"] = normalized_source_input_hash
    return source_info


def build_core_media_artifact_entries(
    *,
    source_original_audio: str | PathLike[str] | None = None,
    source_original_video: str | PathLike[str] | None = None,
    working_speech_for_asr: str | PathLike[str] | None = None,
    working_ambient_audio: str | PathLike[str] | None = None,
    media_transcript_raw: str | PathLike[str] | None = None,
    media_transcript_structured: str | PathLike[str] | None = None,
    translation_segments: str | PathLike[str] | None = None,
) -> list[ArtifactEntry]:
    artifact_entries: list[ArtifactEntry] = []
    _append_artifact_entry(artifact_entries, "source.original_audio", source_original_audio)
    _append_artifact_entry(artifact_entries, "source.original_video", source_original_video)
    _append_artifact_entry(artifact_entries, "working.speech_for_asr", working_speech_for_asr)
    _append_artifact_entry(artifact_entries, "working.ambient_audio", working_ambient_audio)
    _append_artifact_entry(artifact_entries, "media.transcript_raw", media_transcript_raw)
    _append_artifact_entry(
        artifact_entries,
        "media.transcript_structured",
        media_transcript_structured,
    )
    _append_artifact_entry(artifact_entries, "translation.segments", translation_segments)
    return artifact_entries


def build_editor_artifact_entries(
    *,
    editor_draft_dir: str | PathLike[str] | None = None,
    editor_draft_content: str | PathLike[str] | None = None,
    editor_draft_meta: str | PathLike[str] | None = None,
    editor_material_dir: str | PathLike[str] | None = None,
    editor_export_json: str | PathLike[str] | None = None,
) -> list[ArtifactEntry]:
    artifact_entries: list[ArtifactEntry] = []
    _append_artifact_entry(artifact_entries, "editor.draft_dir", editor_draft_dir)
    _append_artifact_entry(artifact_entries, "editor.draft_content", editor_draft_content)
    _append_artifact_entry(artifact_entries, "editor.draft_meta", editor_draft_meta)
    _append_artifact_entry(artifact_entries, "editor.material_dir", editor_material_dir)
    _append_artifact_entry(artifact_entries, "editor.export_json", editor_export_json)
    return artifact_entries


def _append_artifact_entry(
    artifact_entries: list[ArtifactEntry],
    key: str,
    value: object,
) -> None:
    normalized_value = _normalize_artifact_value(value)
    if normalized_value is None:
        return
    artifact_entries.append((key, normalized_value))


def _normalize_artifact_value(value: object) -> str | PathLike[str] | None:
    if isinstance(value, str):
        normalized_value = value.strip()
        return normalized_value or None
    if isinstance(value, PathLike):
        return value
    return None


def _normalize_optional_text(value: object) -> str | None:
    if isinstance(value, str):
        normalized_value = value.strip()
        return normalized_value or None
    return None
