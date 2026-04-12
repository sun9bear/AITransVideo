"""Tests for unified cn_text text model (post three-layer migration)."""
from core.models import SemanticBlock, SubtitleLine
from modules.chunking.semantic_block_builder import SemanticBlockBuilder
from modules.draft.caption_retiming import CaptionRetimer, CaptionRetimingConfig


def test_subtitle_line_has_single_cn_text_field() -> None:
    line = SubtitleLine(
        index=1,
        start_ms=0,
        end_ms=1_000,
        speaker_id="speaker_1",
        speaker_name="Host",
        en_text="Hello",
        cn_text="你好",
    )
    assert line.cn_text == "你好"
    # Old fields should not exist
    assert not hasattr(line, "tts_cn_text")
    assert not hasattr(line, "literal_cn_text")


def test_semantic_block_has_single_merged_cn_text_field() -> None:
    block = SemanticBlock(
        block_id="block_0001",
        speaker_id="speaker_1",
        speaker_name="Host",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="你好世界",
    )
    assert block.merged_cn_text == "你好世界"
    assert not hasattr(block, "merged_tts_cn_text")
    assert not hasattr(block, "merged_literal_cn_text")


def test_chunking_merges_cn_text_from_lines() -> None:
    builder = SemanticBlockBuilder()
    lines = [
        SubtitleLine(1, 0, 700, "spk_1", "Alice", "a", "中文甲"),
        SubtitleLine(2, 800, 1_500, "spk_1", "Alice", "b", "中文乙"),
    ]

    blocks = builder.build(lines)

    assert len(blocks) == 1
    assert blocks[0].merged_cn_text == "中文甲中文乙"
    assert blocks[0].cn_line_texts == ["中文甲", "中文乙"]


def test_caption_retiming_uses_cn_text() -> None:
    retimer = CaptionRetimer(CaptionRetimingConfig(min_caption_duration_ms=200))
    block = SemanticBlock(
        block_id="block_0001",
        speaker_id="speaker_1",
        speaker_name="Host",
        original_srt_indices=[1, 2],
        first_start_ms=0,
        last_end_ms=1_200,
        target_duration_ms=1_200,
        merged_cn_text="unused",
        actual_audio_duration_ms=1_200,
    )
    source_lines = [
        SubtitleLine(1, 0, 500, "speaker_1", "Host", "A", "中文甲"),
        SubtitleLine(2, 600, 1_200, "speaker_1", "Host", "B", "中文乙"),
    ]

    captions = retimer.retime_block(block, source_lines)
    assert [caption.text for caption in captions] == ["中文甲", "中文乙"]
