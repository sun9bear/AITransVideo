import json
from pathlib import Path

from core.models import SemanticBlock, SubtitleLine
from modules.draft.caption_retiming import CaptionRetimer, CaptionRetimingConfig, RetimedCaption
from modules.draft.draft_writer import DraftWriter
from modules.draft.export_validator import JianyingExportValidator
from modules.draft.schema_validator import DraftSchemaValidator


def test_caption_retiming_scales_block_timings_proportionally() -> None:
    retimer = CaptionRetimer(CaptionRetimingConfig(min_caption_duration_ms=200))
    block = SemanticBlock(
        block_id="block_0001",
        speaker_id="speaker_1",
        speaker_name="Host",
        original_srt_indices=[1, 2],
        first_start_ms=0,
        last_end_ms=3_000,
        target_duration_ms=3_000,
        merged_cn_text="AB",
        final_cn_lines=["A", "B"],
        actual_audio_duration_ms=6_000,
    )
    source_lines = [
        SubtitleLine(1, 0, 1_000, "speaker_1", "Host", "A", "A"),
        SubtitleLine(2, 1_000, 3_000, "speaker_1", "Host", "B", "B"),
    ]

    captions = retimer.retime_block(block, source_lines)

    assert [(caption.start_ms, caption.end_ms) for caption in captions] == [
        (0, 2_000),
        (2_000, 6_000),
    ]


def test_caption_retiming_enforces_minimum_caption_duration() -> None:
    retimer = CaptionRetimer(CaptionRetimingConfig(min_caption_duration_ms=400))
    block = SemanticBlock(
        block_id="block_0002",
        speaker_id="speaker_1",
        speaker_name="Host",
        original_srt_indices=[1, 2],
        first_start_ms=0,
        last_end_ms=1_200,
        target_duration_ms=1_200,
        merged_cn_text="shortlong",
        final_cn_lines=["short", "long"],
        actual_audio_duration_ms=1_200,
    )
    source_lines = [
        SubtitleLine(1, 0, 100, "speaker_1", "Host", "A", "short"),
        SubtitleLine(2, 100, 1_200, "speaker_1", "Host", "B", "long"),
    ]

    captions = retimer.retime_block(block, source_lines)

    assert captions[0].end_ms - captions[0].start_ms >= 400
    assert captions[1].start_ms >= captions[0].end_ms
    assert captions[-1].end_ms == 1_200


def test_draft_writer_outputs_valid_scaffold_files(tmp_path: Path) -> None:
    source_audio_path = tmp_path / "aligned_block.wav"
    source_audio_path.write_bytes(b"RIFFdraft")
    block = SemanticBlock(
        block_id="block_0001",
        speaker_id="speaker_1",
        speaker_name="Host",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=900,
        target_duration_ms=900,
        merged_cn_text="CN:hello",
        actual_audio_duration_ms=900,
        aligned_audio_path=str(source_audio_path),
        final_cn_lines=["CN:hello"],
    )
    captions = [
        RetimedCaption(
            caption_id="block_0001_caption_01",
            block_id="block_0001",
            source_srt_index=1,
            speaker_id="speaker_1",
            speaker_name="Host",
            text="CN:hello",
            start_ms=0,
            end_ms=900,
        )
    ]
    writer = DraftWriter(output_root_dir=str(tmp_path / "output"))

    result = writer.write(
        project_id="draft_test_project",
        blocks=[block],
        captions=captions,
        stage_snapshot={"draft": {"status": "running"}},
    )

    draft_content = json.loads(Path(result.draft_content_path).read_text(encoding="utf-8"))
    draft_meta = json.loads(Path(result.draft_meta_info_path).read_text(encoding="utf-8"))

    assert Path(result.draft_content_path).exists()
    assert Path(result.draft_meta_info_path).exists()
    assert Path(result.material_dir, "block_0001.wav").exists()
    assert result.export_path is not None
    assert Path(result.export_path).exists()
    assert result.draft_project is not None
    assert result.export_project is not None
    assert result.export_validation_report is not None
    DraftSchemaValidator().validate(result.draft_project)
    JianyingExportValidator().validate(result.export_project)
    assert draft_content["project_id"] == "draft_test_project"
    assert draft_content["timeline"]["caption_tracks"][0]["items"][0]["text"] == "CN:hello"
    assert draft_meta["summary"] == {"block_count": 1, "caption_count": 1, "material_count": 1}
    assert draft_meta["summary"]["caption_count"] == result.draft_project.caption_count

    export_json = json.loads(Path(result.export_path).read_text(encoding="utf-8"))
    assert export_json["export_target"] == "jianying_like_export"
    assert export_json["timeline"]["audio_tracks"][0]["segments"][0]["material_id"] == "audio_material_0001"
    assert export_json["materials"]["audio_materials"][0]["material_id"] == "audio_material_0001"
    assert result.export_validation_report["validation_status"] == "passed"
