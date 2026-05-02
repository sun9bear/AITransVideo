"""Unit tests for SubtitleCue canonical dataclass (Task 1 of subtitle-generation-v2).

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §5.2
"""

from modules.subtitles.cue_models import SubtitleCue


def test_subtitle_cue_creation_with_all_fields():
    """Test creating a valid SubtitleCue with all fields populated."""
    cue = SubtitleCue(
        cue_id="cue_001",
        block_id="block_001",
        speaker_id="speaker_a",
        speaker_name="Alice",
        text="这是一段字幕文本",
        en_text="This is a subtitle text",
        start_ms=1000,
        end_ms=3000,
        source="semantic_block_v2",
    )

    assert cue.cue_id == "cue_001"
    assert cue.block_id == "block_001"
    assert cue.speaker_id == "speaker_a"
    assert cue.speaker_name == "Alice"
    assert cue.text == "这是一段字幕文本"
    assert cue.en_text == "This is a subtitle text"
    assert cue.start_ms == 1000
    assert cue.end_ms == 3000
    assert cue.source == "semantic_block_v2"
    assert cue.needs_review is False
    assert cue.review_reason is None


def test_subtitle_cue_speaker_name_none():
    """Test that speaker_name can be None."""
    cue = SubtitleCue(
        cue_id="cue_002",
        block_id="block_002",
        speaker_id="speaker_b",
        speaker_name=None,
        text="字幕文本",
        en_text="Subtitle text",
        start_ms=0,
        end_ms=2000,
        source="semantic_block_v2",
    )

    assert cue.speaker_name is None


def test_subtitle_cue_mixed_cjk_latin_digits():
    """Test that text and en_text accept mixed CJK + Latin + digit content, with whitespace stripped."""
    cue = SubtitleCue(
        cue_id="cue_005",
        block_id="block_005",
        speaker_id="speaker_e",
        speaker_name="Eve",
        text="  今天讲 LLM 的 token 数 1024 个  ",
        en_text="  Today we discuss LLM's token count of 1024 pieces  ",
        start_ms=300,
        end_ms=700,
        source="semantic_block_v2",
    )

    # __post_init__ should strip leading/trailing whitespace
    assert cue.text == "今天讲 LLM 的 token 数 1024 个"
    assert cue.en_text == "Today we discuss LLM's token count of 1024 pieces"


def test_subtitle_cue_with_needs_review_and_reason():
    """Test creating a SubtitleCue with needs_review=True and a review_reason."""
    cue = SubtitleCue(
        cue_id="cue_006",
        block_id="block_006",
        speaker_id="speaker_f",
        speaker_name="Frank",
        text="这是一个需要审查的长文本",
        en_text="This is a long text needing review",
        start_ms=400,
        end_ms=800,
        source="semantic_block_v2",
        needs_review=True,
        review_reason="long_unbreakable_text",
    )

    assert cue.needs_review is True
    assert cue.review_reason == "long_unbreakable_text"


def test_subtitle_cue_text_with_whitespace_stripped():
    """Test that text and en_text have leading/trailing whitespace stripped."""
    cue = SubtitleCue(
        cue_id="cue_007",
        block_id="block_007",
        speaker_id="speaker_g",
        speaker_name="Grace",
        text="  这是带空格的文本  ",
        en_text="  Text with spaces  ",
        start_ms=500,
        end_ms=900,
        source="semantic_block_v2",
    )

    # __post_init__ should strip whitespace
    assert cue.text == "这是带空格的文本"
    assert cue.en_text == "Text with spaces"
