import json

import pytest

from core.exceptions import DraftError
from modules.draft.export_validator import JianyingExportValidator
from modules.draft.jianying_adapter import JianyingExportAdapter, JianyingAdapterSkeleton
from modules.draft.schema import (
    DraftAudioTrack,
    DraftAudioTrackItem,
    DraftCaptionItem,
    DraftCaptionTrack,
    DraftMaterial,
    DraftProject,
    DraftTimeline,
)
from modules.draft.schema_validator import DraftSchemaValidator


def _make_draft_project() -> DraftProject:
    material = DraftMaterial(
        material_id="audio_material_0001",
        block_id="block_0001",
        source_audio_path="D:/demo/audio.wav",
        relative_material_path="materials/block_0001.wav",
        start_ms=0,
        duration_ms=1_200,
        speaker_id="speaker_1",
        speaker_name="Host",
    )
    audio_item = DraftAudioTrackItem(
        item_id="audio_item_0001",
        material_id=material.material_id,
        block_id=material.block_id,
        start_ms=0,
        duration_ms=1_200,
    )
    caption_item = DraftCaptionItem(
        item_id="caption_item_0001",
        caption_id="block_0001_caption_01",
        block_id="block_0001",
        source_srt_index=1,
        speaker_id="speaker_1",
        speaker_name="Host",
        text="CN:test",
        start_ms=0,
        end_ms=1_200,
    )
    return DraftProject(
        schema_version="jianying_draft_scaffold_v1",
        project_id="schema_demo",
        generated_at="2026-03-12T00:00:00+00:00",
        block_count=1,
        caption_count=1,
        material_count=1,
        timeline=DraftTimeline(
            duration_ms=1_200,
            audio_tracks=[DraftAudioTrack(track_id="audio_track_0001", items=[audio_item])],
            caption_tracks=[DraftCaptionTrack(track_id="caption_track_0001", items=[caption_item])],
        ),
        materials=[material],
        notes=["schema test"],
        stage_snapshot={"draft": {"status": "done"}},
    )


def test_draft_schema_model_construction_and_validation() -> None:
    project = _make_draft_project()

    DraftSchemaValidator().validate(project)

    assert project.timeline.duration_ms == 1_200
    assert project.block_count == 1
    assert project.caption_count == 1
    assert project.material_count == 1
    assert project.to_content_dict()["materials"][0]["material_id"] == "audio_material_0001"


def test_jianying_adapter_skeleton_outputs_valid_json_structure() -> None:
    project = _make_draft_project()
    adapter_output = JianyingAdapterSkeleton().adapt(project)
    validation_report = JianyingExportValidator().validate(adapter_output)

    serialized_output = json.dumps(adapter_output.to_dict(), ensure_ascii=False)

    assert "jianying_export_preview_v1" in serialized_output
    assert adapter_output.project_id == "schema_demo"
    assert adapter_output.timeline.duration_ms == 1_200
    assert len(adapter_output.timeline.audio_tracks) == 1
    assert len(adapter_output.timeline.caption_tracks) == 1
    assert "mapped_fields" in adapter_output.mapping_report
    assert "unmapped_fields" in adapter_output.mapping_report
    assert "assumptions" in adapter_output.mapping_report
    assert "future_mapping_plan" in adapter_output.mapping_report
    assert validation_report["validation_status"] == "passed"
    assert validation_report["track_counts"]["audio"] == 1


def test_export_adapter_maps_audio_and_caption_relationships() -> None:
    project = _make_draft_project()
    export = JianyingExportAdapter().adapt(project)

    audio_segment = export.timeline.audio_tracks[0].segments[0]
    caption_segment = export.timeline.caption_tracks[0].segments[0]

    assert audio_segment.material_id == export.audio_materials[0].material_id
    assert audio_segment.track_id == export.timeline.audio_tracks[0].track_id
    assert caption_segment.track_id == export.timeline.caption_tracks[0].track_id
    assert caption_segment.caption_id == "block_0001_caption_01"
    assert "compatibility review" in export.compatibility_notes[0]


def test_export_validator_rejects_inconsistent_audio_material_reference() -> None:
    project = _make_draft_project()
    export = JianyingExportAdapter().adapt(project)
    export.timeline.audio_tracks[0].segments[0].material_id = "missing_material"

    with pytest.raises(DraftError, match="missing material"):
        JianyingExportValidator().validate(export)
