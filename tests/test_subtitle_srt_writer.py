"""Tests for src/modules/subtitles/srt_writer.py (T7, subtitle-generation-v2).

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §T7, Phase 1a.

TDD: tests written before implementation.

Convention decisions locked in from reading EditorPackageWriter._write_srt_file:
  - Trailing separator: blocks joined with "\n\n", then final "\n" appended.
    So output ends in "\n", NOT "\n\n". Empty list → "".
  - Empty-block skip: cues with no displayable text after strip are skipped.
  - Bilingual order: en first, zh second (matches existing EditorPackageWriter
    which does f"{en_text}\n{zh_text}"). Spec draft said zh-first but existing
    project is en-first; T7 aligns with existing project to make T8 migration
    a straight swap.
  - Bilingual empty en_text: Option A — write zh-only content line (no blank
    second line), so empty-en bilingual cues look identical to zh-only output.
"""

import sys
from pathlib import Path

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from modules.subtitles.cue_models import SubtitleCue
from modules.subtitles.srt_writer import (
    _format_srt_time,
    _strip_trailing_subtitle_punct,
    write_bilingual_srt,
    write_en_srt,
    write_zh_srt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_cue(
    *,
    cue_id: str = "cue_001",
    block_id: str = "blk_001",
    speaker_id: str = "speaker_a",
    speaker_name: str | None = "Alice",
    text: str = "今天很好",
    en_text: str = "Today is good",
    start_ms: int = 0,
    end_ms: int = 1500,
    source: str = "test",
    needs_review: bool = False,
    review_reason: str | None = None,
) -> SubtitleCue:
    return SubtitleCue(
        cue_id=cue_id,
        block_id=block_id,
        speaker_id=speaker_id,
        speaker_name=speaker_name,
        text=text,
        en_text=en_text,
        start_ms=start_ms,
        end_ms=end_ms,
        source=source,
        needs_review=needs_review,
        review_reason=review_reason,
    )


# ---------------------------------------------------------------------------
# _format_srt_time unit tests
# ---------------------------------------------------------------------------


class TestFormatSrtTime:
    def test_zero(self):
        assert _format_srt_time(0) == "00:00:00,000"

    def test_sub_second(self):
        # 42 ms → zero-padded 3-digit milliseconds
        assert _format_srt_time(42) == "00:00:00,042"

    def test_exactly_one_second(self):
        assert _format_srt_time(1000) == "00:00:01,000"

    def test_1500ms(self):
        assert _format_srt_time(1500) == "00:00:01,500"

    def test_sub_second_precision(self):
        # 1234 ms → 1 second 234 ms
        assert _format_srt_time(1234) == "00:00:01,234"

    def test_cross_minute(self):
        assert _format_srt_time(65_000) == "00:01:05,000"

    def test_cross_hour(self):
        # 3661000 ms = 1h 1m 1s 0ms
        assert _format_srt_time(3_661_000) == "01:01:01,000"

    def test_cross_hour_with_ms(self):
        # 3700000 ms = 1h 1m 40s 0ms
        assert _format_srt_time(3_700_000) == "01:01:40,000"

    def test_hours_zero_padded(self):
        # 7200000 ms = 2h exactly
        assert _format_srt_time(7_200_000) == "02:00:00,000"

    def test_comma_not_period(self):
        # SRT spec uses comma, not period
        result = _format_srt_time(1500)
        assert "," in result
        assert "." not in result

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="non-negative"):
            _format_srt_time(-1)


# ---------------------------------------------------------------------------
# Scenario 1: Empty cues list → empty string
# ---------------------------------------------------------------------------


class TestEmptyCueList:
    def test_zh_empty_list(self):
        assert write_zh_srt([]) == ""

    def test_en_empty_list(self):
        assert write_en_srt([]) == ""

    def test_bilingual_empty_list(self):
        assert write_bilingual_srt([]) == ""


# ---------------------------------------------------------------------------
# Scenario 2: Single cue zh-only
# ---------------------------------------------------------------------------


class TestSingleCueZh:
    def test_basic_single_cue(self):
        cue = make_cue(text="今天很好", en_text="Today is good", start_ms=0, end_ms=1500)
        result = write_zh_srt([cue])
        expected = "1\n00:00:00,000 --> 00:00:01,500\n今天很好\n"
        assert result == expected

    def test_index_starts_at_1(self):
        cue = make_cue()
        result = write_zh_srt([cue])
        assert result.startswith("1\n")

    def test_single_cue_ends_with_single_newline(self):
        cue = make_cue()
        result = write_zh_srt([cue])
        assert result.endswith("\n")
        assert not result.endswith("\n\n")

    def test_metadata_not_in_output(self):
        cue = make_cue(
            cue_id="cue_xyz",
            needs_review=True,
            review_reason="some reason",
            speaker_id="speaker_a",
            speaker_name="Alice",
        )
        result = write_zh_srt([cue])
        assert "cue_xyz" not in result
        assert "needs_review" not in result
        assert "some reason" not in result
        assert "speaker_a" not in result
        assert "Alice" not in result


# ---------------------------------------------------------------------------
# Scenario 3: Single cue en-only
# ---------------------------------------------------------------------------


class TestSingleCueEn:
    def test_uses_en_text(self):
        cue = make_cue(text="今天很好", en_text="Today is good", start_ms=0, end_ms=1500)
        result = write_en_srt([cue])
        expected = "1\n00:00:00,000 --> 00:00:01,500\nToday is good\n"
        assert result == expected

    def test_does_not_contain_zh(self):
        cue = make_cue(text="今天很好", en_text="Today")
        result = write_en_srt([cue])
        assert "今天很好" not in result

    def test_empty_en_text_skipped(self):
        # Cue with no en_text is skipped (block is empty — same as zh-only skips empty text)
        cue = make_cue(en_text="")
        result = write_en_srt([cue])
        assert result == ""


# ---------------------------------------------------------------------------
# Scenario 4: Single cue bilingual — en first, zh second
# ---------------------------------------------------------------------------


class TestSingleCueBilingual:
    def test_two_content_lines(self):
        cue = make_cue(text="今天很好", en_text="Today is good", start_ms=0, end_ms=1500)
        result = write_bilingual_srt([cue])
        expected = "1\n00:00:00,000 --> 00:00:01,500\nToday is good\n今天很好\n"
        assert result == expected

    def test_en_first_zh_second(self):
        cue = make_cue(text="中文", en_text="English")
        result = write_bilingual_srt([cue])
        lines = result.splitlines()
        # Line 0: index, Line 1: timestamp, Line 2: en, Line 3: zh
        assert lines[2] == "English"
        assert lines[3] == "中文"


# ---------------------------------------------------------------------------
# Scenario 5: Multiple cues, sequential 1-based indices
# ---------------------------------------------------------------------------


class TestMultipleCues:
    def test_three_cues_sequential_indices(self):
        cues = [
            make_cue(cue_id="c1", text="第一句", en_text="First", start_ms=0, end_ms=1000),
            make_cue(cue_id="c2", text="第二句", en_text="Second", start_ms=1000, end_ms=2000),
            make_cue(cue_id="c3", text="第三句", en_text="Third", start_ms=2000, end_ms=3000),
        ]
        result = write_zh_srt(cues)
        blocks = result.strip().split("\n\n")
        assert len(blocks) == 3
        assert blocks[0].startswith("1\n")
        assert blocks[1].startswith("2\n")
        assert blocks[2].startswith("3\n")

    def test_blocks_separated_by_double_newline(self):
        cues = [
            make_cue(cue_id="c1", text="A", start_ms=0, end_ms=1000),
            make_cue(cue_id="c2", text="B", start_ms=1000, end_ms=2000),
        ]
        result = write_zh_srt(cues)
        assert "\n\n" in result

    def test_final_block_ends_with_single_newline(self):
        cues = [
            make_cue(cue_id="c1", text="A", start_ms=0, end_ms=1000),
            make_cue(cue_id="c2", text="B", start_ms=1000, end_ms=2000),
        ]
        result = write_zh_srt(cues)
        assert result.endswith("\n")
        assert not result.endswith("\n\n")


# ---------------------------------------------------------------------------
# Scenario 6: Time format uses comma, not period
# ---------------------------------------------------------------------------


class TestTimeFormat:
    def test_comma_separator(self):
        cue = make_cue(start_ms=0, end_ms=1500)
        result = write_zh_srt([cue])
        assert "00:00:01,500" in result
        assert "00:00:01.500" not in result

    def test_timestamp_line_format(self):
        cue = make_cue(start_ms=0, end_ms=1500)
        result = write_zh_srt([cue])
        lines = result.splitlines()
        assert lines[1] == "00:00:00,000 --> 00:00:01,500"


# ---------------------------------------------------------------------------
# Scenario 7: Cross-hour timestamps
# ---------------------------------------------------------------------------


class TestCrossHour:
    def test_cross_hour_start(self):
        cue = make_cue(start_ms=3_661_000, end_ms=3_665_000)
        result = write_zh_srt([cue])
        assert "01:01:01,000" in result

    def test_cross_hour_end(self):
        cue = make_cue(start_ms=3_700_000, end_ms=3_705_000)
        result = write_zh_srt([cue])
        assert "01:01:40,000" in result


# ---------------------------------------------------------------------------
# Scenario 8: Zero start ms
# ---------------------------------------------------------------------------


class TestZeroStart:
    def test_zero_start_format(self):
        cue = make_cue(start_ms=0, end_ms=1000)
        result = write_zh_srt([cue])
        assert "00:00:00,000" in result


# ---------------------------------------------------------------------------
# Scenario 9: Sub-second precision (3-digit ms zero-padded)
# ---------------------------------------------------------------------------


class TestSubSecondPrecision:
    def test_42ms(self):
        cue = make_cue(start_ms=42, end_ms=1042)
        result = write_zh_srt([cue])
        assert "00:00:00,042" in result

    def test_1ms(self):
        cue = make_cue(start_ms=1, end_ms=1001)
        result = write_zh_srt([cue])
        assert "00:00:00,001" in result

    def test_999ms(self):
        cue = make_cue(start_ms=999, end_ms=1999)
        result = write_zh_srt([cue])
        assert "00:00:00,999" in result


# ---------------------------------------------------------------------------
# Scenario 10: Bilingual with empty en_text → zh-only content line (Option A)
# ---------------------------------------------------------------------------


class TestBilingualEmptyEn:
    def test_empty_en_produces_zh_only_line(self):
        cue = make_cue(text="只有中文", en_text="")
        result = write_bilingual_srt([cue])
        # Should still write the cue (zh is non-empty), but only one content line
        assert "只有中文" in result
        lines = result.splitlines()
        # Line 0: index, Line 1: timestamp, Line 2: zh only (no en line)
        assert lines[2] == "只有中文"
        assert len(lines) == 3  # index + timestamp + content (no blank line at end due to splitlines)

    def test_whitespace_en_also_produces_zh_only(self):
        cue = make_cue(text="只有中文", en_text="   ")
        result = write_bilingual_srt([cue])
        assert "只有中文" in result
        lines = result.splitlines()
        assert lines[2] == "只有中文"
        assert len(lines) == 3


# ---------------------------------------------------------------------------
# Scenario 11: Strip leading/trailing whitespace (defensive)
# ---------------------------------------------------------------------------


class TestWhitespaceStripping:
    def test_strip_zh_text(self):
        # SubtitleCue.__post_init__ already strips, but writer is defensive
        cue = make_cue(text="  中文  ")
        result = write_zh_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "中文"

    def test_strip_en_text(self):
        cue = make_cue(en_text="  English  ")
        result = write_en_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "English"

    def test_strip_bilingual_both(self):
        cue = make_cue(text="  中文  ", en_text="  English  ")
        result = write_bilingual_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "English"
        assert lines[3] == "中文"


# ---------------------------------------------------------------------------
# Scenario 12: CJK characters preserved verbatim (trailing punct stripped in SRT)
# ---------------------------------------------------------------------------


class TestCjkPreserved:
    def test_cjk_unchanged(self):
        # Trailing 。 is stripped in SRT output; internal ，is preserved.
        cue = make_cue(text="你好世界，这是一个测试。")
        result = write_zh_srt([cue])
        # Internal content should appear without trailing 。
        assert "你好世界，这是一个测试" in result
        # Trailing punct must NOT appear at end of content line
        lines = result.splitlines()
        assert lines[2] == "你好世界，这是一个测试"

    def test_full_width_preserved(self):
        cue = make_cue(text="（括号内容）")
        result = write_zh_srt([cue])
        # 「）」 is a closing bracket, not in trailing-punct strip set; preserved
        assert "（括号内容）" in result


# ---------------------------------------------------------------------------
# Scenario 13: Mixed CJK + Latin + digits preserved
# ---------------------------------------------------------------------------


class TestMixedContent:
    def test_mixed_preserved(self):
        cue = make_cue(text="第3段 Hello World 2026")
        result = write_zh_srt([cue])
        assert "第3段 Hello World 2026" in result


# ---------------------------------------------------------------------------
# Scenario 14: Newline in cue.text replaced with space
# ---------------------------------------------------------------------------


class TestNewlineReplacement:
    def test_newline_in_zh_text_replaced(self):
        cue = make_cue(text="line1\nline2")
        result = write_zh_srt([cue])
        lines = result.splitlines()
        # The content line should have a space, not two separate lines
        assert lines[2] == "line1 line2"
        # Ensure only one content line (not two)
        assert len(lines) == 3

    def test_newline_in_en_text_replaced(self):
        cue = make_cue(en_text="part1\npart2")
        result = write_en_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "part1 part2"

    def test_bilingual_newlines_in_both(self):
        cue = make_cue(text="一\n二", en_text="one\ntwo")
        result = write_bilingual_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "one two"
        assert lines[3] == "一 二"


# ---------------------------------------------------------------------------
# Scenario 15: Bilingual with both zh and en → en line 1, zh line 2
# ---------------------------------------------------------------------------


class TestBilingualBothPresent:
    def test_structure(self):
        cue = make_cue(text="大家好", en_text="Hello everyone", start_ms=500, end_ms=2000)
        result = write_bilingual_srt([cue])
        expected = "1\n00:00:00,500 --> 00:00:02,000\nHello everyone\n大家好\n"
        assert result == expected

    def test_multiple_bilingual_cues(self):
        cues = [
            make_cue(cue_id="c1", text="第一", en_text="First", start_ms=0, end_ms=1000),
            make_cue(cue_id="c2", text="第二", en_text="Second", start_ms=1000, end_ms=2000),
        ]
        result = write_bilingual_srt(cues)
        blocks = result.strip().split("\n\n")
        assert len(blocks) == 2
        b1_lines = blocks[0].splitlines()
        assert b1_lines[2] == "First"
        assert b1_lines[3] == "第一"
        b2_lines = blocks[1].splitlines()
        assert b2_lines[2] == "Second"
        assert b2_lines[3] == "第二"


# ---------------------------------------------------------------------------
# Scenario 16: Full SRT string verbatim check (integration)
# ---------------------------------------------------------------------------


class TestFullSrtOutput:
    def test_zh_verbatim_two_cues(self):
        cues = [
            make_cue(cue_id="c1", text="今天很好", start_ms=0, end_ms=1500),
            make_cue(cue_id="c2", text="明天也好", start_ms=1500, end_ms=3500),
        ]
        result = write_zh_srt(cues)
        expected = (
            "1\n"
            "00:00:00,000 --> 00:00:01,500\n"
            "今天很好\n"
            "\n"
            "2\n"
            "00:00:01,500 --> 00:00:03,500\n"
            "明天也好\n"
        )
        assert result == expected

    def test_en_verbatim_two_cues(self):
        cues = [
            make_cue(cue_id="c1", en_text="Today is good", start_ms=0, end_ms=1500),
            make_cue(cue_id="c2", en_text="Tomorrow too", start_ms=1500, end_ms=3500),
        ]
        result = write_en_srt(cues)
        expected = (
            "1\n"
            "00:00:00,000 --> 00:00:01,500\n"
            "Today is good\n"
            "\n"
            "2\n"
            "00:00:01,500 --> 00:00:03,500\n"
            "Tomorrow too\n"
        )
        assert result == expected

    def test_bilingual_verbatim_two_cues(self):
        cues = [
            make_cue(cue_id="c1", text="今天很好", en_text="Today is good", start_ms=0, end_ms=1500),
            make_cue(cue_id="c2", text="明天也好", en_text="Tomorrow too", start_ms=1500, end_ms=3500),
        ]
        result = write_bilingual_srt(cues)
        expected = (
            "1\n"
            "00:00:00,000 --> 00:00:01,500\n"
            "Today is good\n"
            "今天很好\n"
            "\n"
            "2\n"
            "00:00:01,500 --> 00:00:03,500\n"
            "Tomorrow too\n"
            "明天也好\n"
        )
        assert result == expected


# ---------------------------------------------------------------------------
# Scenario 17: _strip_trailing_subtitle_punct unit tests
# ---------------------------------------------------------------------------


class TestStripTrailingSubtitlePunct:
    """Unit tests for the _strip_trailing_subtitle_punct helper."""

    def test_trailing_cjk_comma_stripped(self):
        assert _strip_trailing_subtitle_punct("今天很好，") == "今天很好"

    def test_trailing_ascii_comma_stripped(self):
        assert _strip_trailing_subtitle_punct("today,") == "today"

    def test_trailing_cjk_period_stripped(self):
        assert _strip_trailing_subtitle_punct("明天更好。") == "明天更好"

    def test_trailing_ascii_period_stripped(self):
        assert _strip_trailing_subtitle_punct("hello.") == "hello"

    def test_trailing_cjk_exclamation_stripped(self):
        assert _strip_trailing_subtitle_punct("真好！") == "真好"

    def test_trailing_cjk_question_stripped(self):
        assert _strip_trailing_subtitle_punct("是吗？") == "是吗"

    def test_trailing_ascii_question_exclamation_multi_punct(self):
        # '?!' — both stripped iteratively
        assert _strip_trailing_subtitle_punct("真的吗?!") == "真的吗"

    def test_trailing_cjk_ideographic_comma_stripped(self):
        assert _strip_trailing_subtitle_punct("第一、") == "第一"

    def test_trailing_emdash_stripped(self):
        # —— (two U+2014): both stripped by rstrip since — is in the strip set
        assert _strip_trailing_subtitle_punct("说到——") == "说到"

    def test_trailing_ellipsis_stripped(self):
        # …… (two U+2026): both stripped
        assert _strip_trailing_subtitle_punct("也许……") == "也许"

    def test_internal_punct_preserved(self):
        # Internal comma must NOT be stripped
        assert _strip_trailing_subtitle_punct("今天，我们来看") == "今天，我们来看"

    def test_mixed_cn_en_trailing_comma(self):
        assert _strip_trailing_subtitle_punct("hello 你好,") == "hello 你好"

    def test_no_trailing_punct_unchanged(self):
        assert _strip_trailing_subtitle_punct("今天很好") == "今天很好"

    def test_empty_string(self):
        assert _strip_trailing_subtitle_punct("") == ""

    def test_only_punct_becomes_empty(self):
        # Text that is entirely trailing punct → empty string
        assert _strip_trailing_subtitle_punct("。") == ""

    def test_trailing_whitespace_stripped(self):
        assert _strip_trailing_subtitle_punct("今天  ") == "今天"


# ---------------------------------------------------------------------------
# Scenario 18: Trailing-punct strip applied in SRT writer functions
# ---------------------------------------------------------------------------


class TestTrailingPunctInSrtOutput:
    """Verify that write_zh_srt / write_en_srt / write_bilingual_srt all strip
    trailing punct from displayed cue text without altering cue.text itself."""

    def test_zh_srt_trailing_cjk_period_stripped(self):
        cue = make_cue(text="今天很好。")
        result = write_zh_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "今天很好"

    def test_zh_srt_trailing_ideographic_comma_stripped(self):
        cue = make_cue(text="第一、")
        result = write_zh_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "第一"

    def test_zh_srt_trailing_emdash_stripped(self):
        cue = make_cue(text="而且——")
        result = write_zh_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "而且"

    def test_zh_srt_trailing_ellipsis_stripped(self):
        cue = make_cue(text="只是……")
        result = write_zh_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "只是"

    def test_en_srt_trailing_period_stripped(self):
        cue = make_cue(en_text="Hello world.")
        result = write_en_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "Hello world"

    def test_en_srt_trailing_question_exclamation_stripped(self):
        cue = make_cue(en_text="Really?!")
        result = write_en_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "Really"

    def test_bilingual_both_trailing_stripped(self):
        cue = make_cue(text="今天好。", en_text="Today good.")
        result = write_bilingual_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "Today good"
        assert lines[3] == "今天好"

    def test_internal_punct_preserved_in_srt(self):
        # Comma in the middle of text must not be stripped
        cue = make_cue(text="今天，我们来看")
        result = write_zh_srt([cue])
        lines = result.splitlines()
        assert lines[2] == "今天，我们来看"

    def test_cue_text_field_unchanged(self):
        # The data-layer cue.text must be untouched after SRT serialization
        original_text = "今天很好。"
        cue = make_cue(text=original_text)
        write_zh_srt([cue])
        assert cue.text == original_text
