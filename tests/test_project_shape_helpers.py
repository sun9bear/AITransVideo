from pathlib import Path

import pytest

from modules.workflow.project_shape_helpers import (
    build_canonical_source_info,
    build_core_media_artifact_entries,
    build_editor_artifact_entries,
)


def test_build_canonical_source_info_normalizes_optional_fields() -> None:
    source_info = build_canonical_source_info(
        source_kind="  local_audio  ",
        locator="  demo.wav  ",
        source_path="  D:/demo.wav  ",
        metadata={"title": "Demo"},
        authoritative_input_used=False,
        authoritative_path_kind="  local_audio  ",
        authoritative_flow="  local_audio -> transcript  ",
        source_input_hash="  hash123  ",
    )

    assert source_info == {
        "source_kind": "local_audio",
        "locator": "demo.wav",
        "source_path": "D:/demo.wav",
        "metadata": {"title": "Demo"},
        "authoritative_input_used": False,
        "authoritative_path_kind": "local_audio",
        "authoritative_flow": "local_audio -> transcript",
        "source_input_hash": "hash123",
    }


def test_build_canonical_source_info_requires_source_kind() -> None:
    with pytest.raises(ValueError, match="source_kind is required"):
        build_canonical_source_info(source_kind="   ")


def test_build_core_media_artifact_entries_skips_empty_values() -> None:
    artifact_entries = build_core_media_artifact_entries(
        source_original_audio="",
        source_original_video=Path("input/original.mp4"),
        working_speech_for_asr="input/speech.wav",
        working_ambient_audio=None,
        media_transcript_raw=" ",
        media_transcript_structured="transcript/transcript.json",
        translation_segments="translation/segments.json",
    )

    assert artifact_entries == [
        ("source.original_video", Path("input/original.mp4")),
        ("working.speech_for_asr", "input/speech.wav"),
        ("media.transcript_structured", "transcript/transcript.json"),
        ("translation.segments", "translation/segments.json"),
    ]


def test_build_editor_artifact_entries_skips_empty_values() -> None:
    artifact_entries = build_editor_artifact_entries(
        editor_draft_dir="output/draft",
        editor_draft_content="",
        editor_draft_meta="output/meta_info.json",
        editor_material_dir=None,
        editor_export_json=Path("output/export.json"),
    )

    assert artifact_entries == [
        ("editor.draft_dir", "output/draft"),
        ("editor.draft_meta", "output/meta_info.json"),
        ("editor.export_json", Path("output/export.json")),
    ]
