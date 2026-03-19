from core.models import SubtitleLine
from modules.chunking.semantic_block_builder import SemanticBlockBuilder, SemanticBlockBuilderConfig


def test_build_merges_lines_with_same_speaker_and_small_gap() -> None:
    builder = SemanticBlockBuilder(
        SemanticBlockBuilderConfig(
            max_gap_ms=1_000,
            hard_break_gap_ms=2_000,
            max_block_duration_ms=5_000,
            max_chars_per_block=20,
        )
    )
    lines = [
        SubtitleLine(1, 0, 800, "spk_1", "Alice", "hello", "你好"),
        SubtitleLine(2, 1_000, 1_800, "spk_1", "Alice", "world", "世界"),
    ]

    blocks = builder.build(lines)

    assert len(blocks) == 1
    assert blocks[0].original_srt_indices == [1, 2]
    assert blocks[0].target_duration_ms == 1_800
    assert blocks[0].merged_cn_text == "你好世界"


def test_build_splits_on_sentence_break_and_hard_break_gap() -> None:
    builder = SemanticBlockBuilder(
        SemanticBlockBuilderConfig(
            max_gap_ms=1_500,
            hard_break_gap_ms=2_000,
            max_block_duration_ms=8_000,
            max_chars_per_block=30,
            sentence_break_gap_ms=500,
        )
    )
    lines = [
        SubtitleLine(1, 0, 900, "spk_1", "Alice", "a", "第一句。"),
        SubtitleLine(2, 1_600, 2_200, "spk_1", "Alice", "b", "第二句"),
        SubtitleLine(3, 4_500, 5_000, "spk_1", "Alice", "c", "第三句"),
    ]

    blocks = builder.build(lines)

    assert len(blocks) == 3
    assert [block.original_srt_indices for block in blocks] == [[1], [2], [3]]


def test_build_splits_when_char_limit_would_be_exceeded() -> None:
    builder = SemanticBlockBuilder(
        SemanticBlockBuilderConfig(
            max_gap_ms=1_000,
            hard_break_gap_ms=2_000,
            max_block_duration_ms=8_000,
            max_chars_per_block=5,
        )
    )
    lines = [
        SubtitleLine(1, 0, 500, "spk_1", None, "a", "你好啊"),
        SubtitleLine(2, 700, 1_200, "spk_1", None, "b", "朋友们"),
    ]

    blocks = builder.build(lines)

    assert len(blocks) == 2
    assert blocks[0].merged_cn_text == "你好啊"
    assert blocks[1].merged_cn_text == "朋友们"
