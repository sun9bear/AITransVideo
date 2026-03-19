from core.models import SemanticBlock, SubtitleLine
from modules.chunking.semantic_block_builder import SemanticBlockBuilder
from modules.draft.caption_retiming import CaptionRetimer, CaptionRetimingConfig


def test_chunking_uses_downstream_text_priority_tts_then_literal_then_cn() -> None:
    builder = SemanticBlockBuilder()
    lines = [
        SubtitleLine(
            index=1,
            start_ms=0,
            end_ms=700,
            speaker_id="spk_1",
            speaker_name="Alice",
            en_text="a",
            cn_text="compat_a",
            literal_cn_text="literal_a",
        ),
        SubtitleLine(
            index=2,
            start_ms=800,
            end_ms=1_500,
            speaker_id="spk_1",
            speaker_name="Alice",
            en_text="b",
            cn_text="compat_b",
            literal_cn_text="literal_b",
            tts_cn_text="tts_b",
        ),
    ]

    blocks = builder.build(lines)

    assert len(blocks) == 1
    assert blocks[0].merged_literal_cn_text == "literal_aliteral_b"
    assert blocks[0].merged_tts_cn_text == "literal_atts_b"
    assert blocks[0].cn_line_texts == ["literal_a", "tts_b"]
    assert blocks[0].merged_cn_text == "literal_atts_b"
    assert blocks[0].get_preferred_cn_text_for_tts() == "literal_atts_b"


def test_chunking_leaves_block_tts_layer_empty_when_no_tts_text_exists() -> None:
    builder = SemanticBlockBuilder()
    lines = [
        SubtitleLine(1, 0, 700, "spk_1", "Alice", "a", "compat_a", literal_cn_text="literal_a"),
        SubtitleLine(2, 800, 1_500, "spk_1", "Alice", "b", "compat_b", literal_cn_text="literal_b"),
    ]

    blocks = builder.build(lines)

    assert len(blocks) == 1
    assert blocks[0].merged_literal_cn_text == "literal_aliteral_b"
    assert blocks[0].merged_tts_cn_text == ""
    assert blocks[0].merged_cn_text == "literal_aliteral_b"


def test_semantic_block_prefers_tts_then_literal_then_compat_cn_text() -> None:
    block = SemanticBlock(
        block_id="block_0002",
        speaker_id="speaker_1",
        speaker_name="Host",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="compat_block",
        merged_literal_cn_text="literal_block",
        merged_tts_cn_text="tts_block",
    )

    assert block.get_preferred_cn_text_for_tts() == "tts_block"

    block.merged_tts_cn_text = ""
    assert block.get_preferred_cn_text_for_tts() == "literal_block"

    block.merged_literal_cn_text = ""
    assert block.get_preferred_cn_text_for_tts() == "compat_block"
    assert block.get_preferred_cn_text_for_caption() == "compat_block"


def test_subtitle_line_prefers_tts_then_literal_then_compat_for_caption() -> None:
    line = SubtitleLine(
        index=1,
        start_ms=0,
        end_ms=1_000,
        speaker_id="speaker_1",
        speaker_name="Host",
        en_text="A",
        cn_text="compat_line",
        literal_cn_text="literal_line",
        tts_cn_text="tts_line",
    )

    assert line.get_preferred_cn_text_for_caption() == "tts_line"

    line.tts_cn_text = ""
    assert line.get_preferred_cn_text_for_caption() == "literal_line"

    line.literal_cn_text = ""
    assert line.get_preferred_cn_text_for_caption() == "compat_line"


def test_caption_retiming_fallback_prefers_tts_then_literal_then_cn_text() -> None:
    retimer = CaptionRetimer(CaptionRetimingConfig(min_caption_duration_ms=200))
    block = SemanticBlock(
        block_id="block_0003",
        speaker_id="speaker_1",
        speaker_name="Host",
        original_srt_indices=[1, 2, 3],
        first_start_ms=0,
        last_end_ms=1_800,
        target_duration_ms=1_800,
        merged_cn_text="unused",
        actual_audio_duration_ms=1_800,
    )
    source_lines = [
        SubtitleLine(1, 0, 500, "speaker_1", "Host", "A", "compat_a", literal_cn_text="literal_a"),
        SubtitleLine(2, 600, 1_100, "speaker_1", "Host", "B", "compat_b", literal_cn_text="literal_b", tts_cn_text="tts_b"),
        SubtitleLine(3, 1_200, 1_800, "speaker_1", "Host", "C", "compat_c"),
    ]

    captions = retimer.retime_block(block, source_lines)

    assert [caption.text for caption in captions] == ["literal_a", "tts_b", "compat_c"]
