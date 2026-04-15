"""Tests for probe TTS calibration: segment selection, _build_probe_groups, and
calibrated _build_groups.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Lightweight stub for TranscriptLine (avoids heavy imports)
# ---------------------------------------------------------------------------
@dataclass
class _FakeTranscriptLine:
    index: int
    start_ms: int
    end_ms: int
    speaker_id: str
    speaker_label: str
    source_text: str


def _make_line(
    index: int,
    start_ms: int,
    end_ms: int,
    speaker_id: str = "speaker_a",
    source_text: str = "Hello world",
) -> _FakeTranscriptLine:
    return _FakeTranscriptLine(
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_id=speaker_id,
        speaker_label=speaker_id,
        source_text=source_text,
    )


# Helper: generate text with N words
def _words(n: int) -> str:
    return " ".join(f"word{i}" for i in range(n))


# ---------------------------------------------------------------------------
# _count_source_words
# ---------------------------------------------------------------------------
class TestCountSourceWords:
    @staticmethod
    def _count(text: str) -> int:
        from pipeline.process import ProcessPipeline
        return ProcessPipeline._count_source_words(text)

    def test_empty(self):
        assert self._count("") == 0

    def test_english_words(self):
        assert self._count("Hello world foo bar") == 4

    def test_mixed_with_numbers(self):
        assert self._count("I have 3 cats and 10 dogs") == 7

    def test_contractions(self):
        assert self._count("I'm don't they'll") == 3

    def test_none_input(self):
        assert self._count(None) == 0


# ---------------------------------------------------------------------------
# _select_probe_segments (hybrid word count + duration)
# ---------------------------------------------------------------------------
class TestSelectProbeSegments:
    """Tests for ProcessPipeline._select_probe_segments (static method)."""

    @staticmethod
    def _select(lines, **kwargs):
        from pipeline.process import ProcessPipeline
        return ProcessPipeline._select_probe_segments(lines, **kwargs)

    def test_empty_lines_returns_empty(self):
        assert self._select([]) == []

    def test_too_few_lines_returns_empty(self):
        # Only 2 lines — first and last are excluded, so 0 candidates
        lines = [
            _make_line(1, 0, 5000, source_text=_words(50)),
            _make_line(2, 5000, 10000, source_text=_words(50)),
        ]
        assert self._select(lines) == []

    def test_skips_first_and_last(self):
        lines = [
            _make_line(1, 0, 5000, source_text=_words(50)),      # first — skipped
            _make_line(2, 5000, 10000, source_text=_words(50)),   # middle
            _make_line(3, 10000, 15000, source_text=_words(50)),  # middle
            _make_line(4, 15000, 20000, source_text=_words(50)),  # middle
            _make_line(5, 20000, 25000, source_text=_words(50)),  # last — skipped
        ]
        result = self._select(lines)
        indices = [l.index for l in result]
        assert 1 not in indices
        assert 5 not in indices
        assert len(result) == 3

    def test_filters_by_word_count(self):
        lines = [
            _make_line(1, 0, 5000, source_text=_words(50)),       # first
            _make_line(2, 5000, 10000, source_text=_words(5)),     # too few words
            _make_line(3, 10000, 15000, source_text=_words(50)),   # good
            _make_line(4, 15000, 20000, source_text=_words(150)),  # too many words
            _make_line(5, 20000, 25000, source_text=_words(50)),   # good
            _make_line(6, 25000, 30000, source_text=_words(50)),   # last
        ]
        result = self._select(lines)
        indices = [l.index for l in result]
        assert 2 not in indices  # too few words
        assert 4 not in indices  # too many words
        assert 3 in indices
        assert 5 in indices

    def test_filters_by_duration(self):
        lines = [
            _make_line(1, 0, 1000, source_text=_words(50)),         # first
            _make_line(2, 1000, 2000, source_text=_words(50)),      # 1s — too short
            _make_line(3, 2000, 7000, source_text=_words(50)),      # 5s — good
            _make_line(4, 7000, 72000, source_text=_words(50)),     # 65s — too long (>60s)
            _make_line(5, 72000, 77000, source_text=_words(50)),    # 5s — good
            _make_line(6, 77000, 82000, source_text=_words(50)),    # last
        ]
        result = self._select(lines)
        indices = [l.index for l in result]
        assert 2 not in indices  # too short
        assert 4 not in indices  # too long
        assert 3 in indices
        assert 5 in indices

    def test_per_speaker_limit(self):
        lines = [_make_line(0, 0, 1000, source_text=_words(50))]  # first (skipped)
        # 6 lines for speaker_a, 6 for speaker_b — all 5s, 50 words each
        for i in range(1, 13):
            spk = "speaker_a" if i <= 6 else "speaker_b"
            lines.append(_make_line(i, i * 5000, (i + 1) * 5000, speaker_id=spk, source_text=_words(50)))
        lines.append(_make_line(13, 65000, 70000, source_text=_words(50)))  # last (skipped)

        result = self._select(lines, per_speaker=3, max_total=10)
        count_a = sum(1 for l in result if l.speaker_id == "speaker_a")
        count_b = sum(1 for l in result if l.speaker_id == "speaker_b")
        assert count_a <= 3
        assert count_b <= 3
        assert len(result) <= 10

    def test_max_total_cap(self):
        lines = [_make_line(0, 0, 1000, source_text=_words(50))]
        for i in range(1, 20):
            lines.append(_make_line(i, i * 5000, (i + 1) * 5000, source_text=_words(50)))
        lines.append(_make_line(20, 100000, 105000, source_text=_words(50)))

        result = self._select(lines, max_total=5)
        assert len(result) <= 5

    def test_max_words_per_speaker(self):
        """Cumulative word count per speaker is capped at max_words_per_speaker."""
        lines = [_make_line(0, 0, 1000, source_text=_words(50))]  # first
        # 5 segments for speaker_a, each 80 words; max_words_per_speaker=200 → max 2
        for i in range(1, 6):
            lines.append(_make_line(i, i * 5000, (i + 1) * 5000, speaker_id="speaker_a", source_text=_words(80)))
        lines.append(_make_line(6, 30000, 35000, source_text=_words(50)))  # last

        result = self._select(lines, per_speaker=5, max_words_per_speaker=200)
        count_a = sum(1 for l in result if l.speaker_id == "speaker_a")
        total_words_a = sum(
            len(l.source_text.split()) for l in result if l.speaker_id == "speaker_a"
        )
        assert total_words_a <= 200
        assert count_a <= 2  # 2 * 80 = 160 < 200; 3 * 80 = 240 > 200

    def test_prefers_mid_length(self):
        """Segments near ideal mid (55 words) are preferred over edge cases."""
        lines = [_make_line(0, 0, 1000, source_text=_words(50))]  # first
        # Create segments with varying word counts
        lines.append(_make_line(1, 5000, 10000, source_text=_words(25)))   # far from 55
        lines.append(_make_line(2, 10000, 15000, source_text=_words(55)))  # exactly ideal
        lines.append(_make_line(3, 15000, 20000, source_text=_words(90)))  # far from 55
        lines.append(_make_line(4, 20000, 25000, source_text=_words(50)))  # close to ideal
        lines.append(_make_line(5, 25000, 30000, source_text=_words(50)))  # last

        result = self._select(lines, per_speaker=2)
        indices = [l.index for l in result]
        # Should pick index 2 (55 words) and index 4 (50 words) over 1 (25) and 3 (90)
        assert 2 in indices
        assert 4 in indices

    def test_progressive_fallback(self):
        """If a speaker has no candidates at min_words=20, fallback to lower thresholds."""
        lines = [
            _make_line(0, 0, 1000, source_text=_words(50)),        # first
            _make_line(1, 5000, 10000, speaker_id="a", source_text=_words(50)),  # speaker_a OK
            _make_line(2, 10000, 15000, speaker_id="b", source_text=_words(8)),  # speaker_b: 8 words (< 20)
            _make_line(3, 15000, 20000, source_text=_words(50)),   # last
        ]
        result = self._select(lines, min_words=20)
        # speaker_b should be picked up via fallback (min_words=5)
        spk_b = [l for l in result if l.speaker_id == "b"]
        assert len(spk_b) == 1

    def test_progressive_fallback_no_speaker_below_5_words(self):
        """If speaker has < 5 words, fallback cannot pick them."""
        lines = [
            _make_line(0, 0, 1000, source_text=_words(50)),
            _make_line(1, 5000, 10000, speaker_id="a", source_text=_words(50)),
            _make_line(2, 10000, 15000, speaker_id="b", source_text=_words(3)),  # only 3 words
            _make_line(3, 15000, 20000, source_text=_words(50)),
        ]
        result = self._select(lines, min_words=20)
        spk_b = [l for l in result if l.speaker_id == "b"]
        assert len(spk_b) == 0

    def test_result_sorted_by_original_order(self):
        """Selected segments should be sorted by their original order in lines."""
        lines = [_make_line(0, 0, 1000, source_text=_words(50))]  # first
        for i in range(1, 8):
            lines.append(_make_line(i, i * 5000, (i + 1) * 5000, source_text=_words(50)))
        lines.append(_make_line(8, 40000, 45000, source_text=_words(50)))  # last

        result = self._select(lines, per_speaker=5)
        indices = [l.index for l in result]
        assert indices == sorted(indices)

    def test_truncation_fallback_for_long_segment(self):
        """Speaker with only very long segments gets a truncated probe."""
        lines = [
            _make_line(0, 0, 1000, source_text=_words(50)),           # first
            _make_line(1, 5000, 10000, speaker_id="a", source_text=_words(50)),  # a: normal
            _make_line(2, 10000, 135000, speaker_id="b", source_text=_words(200)),  # b: 125s, 200 words — too long
            _make_line(3, 135000, 140000, source_text=_words(50)),     # last
        ]
        result = self._select(lines)
        spk_b = [l for l in result if l.speaker_id == "b"]
        assert len(spk_b) == 1
        # Text should be truncated (< 200 words)
        truncated_wc = len(spk_b[0].source_text.split())
        assert truncated_wc <= 80
        # Duration should be proportionally adjusted (not the full 125s)
        dur = spk_b[0].end_ms - spk_b[0].start_ms
        assert dur < 125000

    def test_truncation_preserves_sentence_boundary(self):
        """Truncated text should end at a sentence boundary when possible."""
        # Build text with sentence boundaries
        sentence1 = "The quick brown fox jumps over the lazy dog."
        sentence2 = "She sells seashells by the seashore near the beach."
        sentence3 = "A stitch in time saves nine and more beyond that."
        long_text = f"{sentence1} {sentence2} {sentence3} " + _words(150)
        lines = [
            _make_line(0, 0, 1000, source_text=_words(50)),
            _make_line(1, 5000, 10000, speaker_id="a", source_text=_words(50)),
            _make_line(2, 10000, 200000, speaker_id="b", source_text=long_text),  # very long
            _make_line(3, 200000, 205000, source_text=_words(50)),
        ]
        result = self._select(lines, truncate_words=30)
        spk_b = [l for l in result if l.speaker_id == "b"]
        assert len(spk_b) == 1
        text = spk_b[0].source_text
        # Should end at a sentence boundary
        assert text.rstrip().endswith((".", "?", "!", ",", ";"))

    def test_truncation_covers_speaker_at_boundary(self):
        """Speaker whose only segment is first or last line still gets a truncated probe."""
        lines = [
            _make_line(0, 0, 80000, speaker_id="intro", source_text=_words(120)),   # first — intro speaker
            _make_line(1, 80000, 85000, speaker_id="a", source_text=_words(50)),
            _make_line(2, 85000, 90000, speaker_id="a", source_text=_words(50)),
            _make_line(3, 90000, 150000, speaker_id="outro", source_text=_words(100)),  # last — outro speaker
        ]
        result = self._select(lines)
        # intro and outro speakers should get truncated probes
        intro = [l for l in result if l.speaker_id == "intro"]
        outro = [l for l in result if l.speaker_id == "outro"]
        assert len(intro) == 1, "intro speaker (first line) should get a truncated probe"
        assert len(outro) == 1, "outro speaker (last line) should get a truncated probe"

    def test_truncation_not_needed_within_60s(self):
        """50s segment should pass the relaxed 60s max_duration, no truncation."""
        lines = [
            _make_line(0, 0, 1000, source_text=_words(50)),
            _make_line(1, 5000, 10000, speaker_id="a", source_text=_words(50)),
            _make_line(2, 10000, 60000, speaker_id="b", source_text=_words(80)),  # 50s, 80 words — within limits
            _make_line(3, 60000, 65000, source_text=_words(50)),
        ]
        result = self._select(lines)
        spk_b = [l for l in result if l.speaker_id == "b"]
        assert len(spk_b) == 1
        # Should NOT be truncated — original text preserved
        assert len(spk_b[0].source_text.split()) == 80


# ---------------------------------------------------------------------------
# _truncate_at_sentence
# ---------------------------------------------------------------------------
class TestTruncateAtSentence:
    @staticmethod
    def _truncate(words, target):
        from pipeline.process import _truncate_at_sentence
        return _truncate_at_sentence(words, target)

    def test_short_text_no_truncation(self):
        words = ["hello", "world."]
        assert self._truncate(words, 10) == "hello world."

    def test_truncates_at_period(self):
        words = "The quick fox. She sells seashells. More words here now".split()
        result = self._truncate(words, 8)
        assert result.endswith(".")
        assert len(result.split()) <= 8

    def test_truncates_at_comma_fallback(self):
        words = "one two three four five six, seven eight nine ten eleven".split()
        result = self._truncate(words, 8)
        assert result.endswith(",")

    def test_hard_cut_no_punctuation(self):
        words = [f"w{i}" for i in range(20)]
        result = self._truncate(words, 10)
        assert len(result.split()) == 10


# ---------------------------------------------------------------------------
# _refine_truncated_probe
# ---------------------------------------------------------------------------
class TestRefineTruncatedProbe:
    @staticmethod
    def _refine(line, raw_words, **kwargs):
        from pipeline.process import ProcessPipeline
        return ProcessPipeline._refine_truncated_probe(line, raw_words, **kwargs)

    def test_refines_with_word_timestamps(self):
        # Simulate a 120s segment with word-level timestamps
        raw_words = []
        for i in range(200):
            raw_words.append({"text": f"word{i}." if i == 49 else f"word{i}", "start": i * 600, "end": i * 600 + 500})
        line = _make_line(1, 0, 120000, speaker_id="b", source_text=" ".join(f"word{i}" for i in range(200)))
        result = self._refine(line, raw_words, target_words=80)
        # Should truncate at word49 (has period) which is at ~50 words
        assert result.end_ms == raw_words[49]["end"]  # precise timestamp
        assert "word49." in result.source_text

    def test_no_refinement_when_few_words(self):
        raw_words = [{"text": "hi", "start": 0, "end": 500}]
        line = _make_line(1, 0, 5000, source_text="hi")
        result = self._refine(line, raw_words)
        assert result.source_text == "hi"  # unchanged

    def test_minimum_duration_enforced(self):
        raw_words = [{"text": f"w{i}.", "start": i * 100, "end": i * 100 + 80} for i in range(20)]
        line = _make_line(1, 0, 50000, source_text=" ".join(f"w{i}" for i in range(20)))
        result = self._refine(line, raw_words, target_words=10, min_duration_ms=5000)
        assert result.end_ms - result.start_ms >= 5000


# ---------------------------------------------------------------------------
# _normalize_preview_text
# ---------------------------------------------------------------------------
class TestNormalizePreviewText:
    @staticmethod
    def _normalize(text):
        from services.jobs.review_actions import _normalize_preview_text
        return _normalize_preview_text(text)

    @staticmethod
    def _default_text():
        from services.jobs.review_actions import _PREVIEW_SAMPLE_TEXT
        return _PREVIEW_SAMPLE_TEXT

    def test_none_returns_default(self):
        assert self._normalize(None) == self._default_text()

    def test_empty_returns_default(self):
        assert self._normalize("") == self._default_text()

    def test_short_text_returns_default(self):
        assert self._normalize("太短了") == self._default_text()

    def test_text_within_limit_returned_as_is(self):
        text = "这是一段合适长度的测试文本，用来验证试听功能是否正常工作。"
        assert self._normalize(text) == text

    def test_long_text_truncated_at_punctuation(self):
        # Build text longer than 80 chars with punctuation
        text = "第一段话在这里结束。" * 3 + "第二段话也很长，" * 5 + "超出限制了。"
        result = self._normalize(text)
        assert len(result) <= 80
        # Should end at a punctuation mark
        assert result[-1] in ("。", "，", "、", "；", ",", " ")

    def test_long_text_no_punctuation_hard_cut(self):
        text = "这" * 100  # No punctuation at all
        result = self._normalize(text)
        assert len(result) == 80

    def test_whitespace_stripped(self):
        text = "  这是一段有空格的文本内容测试  "
        result = self._normalize(text)
        assert not result.startswith(" ")
        assert not result.endswith(" ")


# ---------------------------------------------------------------------------
# Probe cache: fingerprint + save/load
# ---------------------------------------------------------------------------
class TestProbeCache:
    @staticmethod
    def _fingerprint(lines, **kwargs):
        from pipeline.process import ProcessPipeline
        return ProcessPipeline._build_probe_fingerprint(lines, **kwargs)

    @staticmethod
    def _save(cache_path, segments, fingerprint):
        from pipeline.process import ProcessPipeline
        return ProcessPipeline._save_probe_cache(cache_path, segments, fingerprint)

    @staticmethod
    def _load(cache_path, expected_fingerprint):
        from pipeline.process import ProcessPipeline
        return ProcessPipeline._load_probe_cache(cache_path, expected_fingerprint)

    def test_fingerprint_deterministic(self):
        lines = [_make_line(1, 0, 5000, source_text="hello"), _make_line(2, 5000, 10000, source_text="world")]
        fp1 = self._fingerprint(lines, model_name="gemini", glossary=None, video_title="t", youtube_url="u")
        fp2 = self._fingerprint(lines, model_name="gemini", glossary=None, video_title="t", youtube_url="u")
        assert fp1 == fp2

    def test_fingerprint_changes_with_model(self):
        lines = [_make_line(1, 0, 5000, source_text="hello")]
        fp1 = self._fingerprint(lines, model_name="gemini", glossary=None, video_title="t", youtube_url="u")
        fp2 = self._fingerprint(lines, model_name="deepseek", glossary=None, video_title="t", youtube_url="u")
        assert fp1 != fp2

    def test_fingerprint_changes_with_glossary(self):
        lines = [_make_line(1, 0, 5000, source_text="hello")]
        fp1 = self._fingerprint(lines, model_name="m", glossary=None, video_title="t", youtube_url="u")
        fp2 = self._fingerprint(lines, model_name="m", glossary={"AI": "人工智能"}, video_title="t", youtube_url="u")
        assert fp1 != fp2

    def test_fingerprint_changes_with_duration(self):
        """Timestamp changes must invalidate cache (probe uses target_duration_seconds)."""
        lines_v1 = [_make_line(1, 0, 5000, source_text="hello")]
        lines_v2 = [_make_line(1, 0, 6000, source_text="hello")]  # same text, different end_ms
        fp1 = self._fingerprint(lines_v1, model_name="m", glossary=None, video_title="t", youtube_url="u")
        fp2 = self._fingerprint(lines_v2, model_name="m", glossary=None, video_title="t", youtube_url="u")
        assert fp1 != fp2

    def test_save_and_load_round_trip(self, tmp_path):
        from services.gemini.translator import DubbingSegment
        seg = DubbingSegment(
            segment_id=1, speaker_id="a", display_name="a", voice_id="",
            source_text="hello", cn_text="你好",
            start_ms=0, end_ms=5000, target_duration_ms=5000,
        )
        fp = "abc123"
        cache_path = tmp_path / "translation" / "_probe_segments.json"
        self._save(cache_path, [seg], fp)

        loaded = self._load(cache_path, fp)
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0].segment_id == 1
        assert loaded[0].cn_text == "你好"
        assert loaded[0].speaker_id == "a"

    def test_load_fingerprint_mismatch_returns_none(self, tmp_path):
        from services.gemini.translator import DubbingSegment
        seg = DubbingSegment(
            segment_id=1, speaker_id="a", display_name="a", voice_id="",
            source_text="hello", cn_text="你好",
            start_ms=0, end_ms=5000, target_duration_ms=5000,
        )
        cache_path = tmp_path / "_probe_segments.json"
        self._save(cache_path, [seg], "fp_v1")

        loaded = self._load(cache_path, "fp_v2")
        assert loaded is None

    def test_load_missing_file_returns_none(self, tmp_path):
        loaded = self._load(tmp_path / "nonexistent.json", "fp")
        assert loaded is None

    def test_load_corrupt_json_returns_none(self, tmp_path):
        cache_path = tmp_path / "_probe.json"
        cache_path.write_text("not json at all", encoding="utf-8")
        loaded = self._load(cache_path, "fp")
        assert loaded is None


# ---------------------------------------------------------------------------
# _build_probe_groups
# ---------------------------------------------------------------------------
class TestBuildProbeGroups:
    """Tests for the probe group builder."""

    @staticmethod
    def _build(lines):
        from services.gemini.translator import _build_probe_groups
        return _build_probe_groups(lines)

    def test_empty(self):
        assert self._build([]) == []

    def test_fields_present(self):
        lines = [_make_line(1, 0, 5000, source_text="Hello")]
        groups = self._build(lines)
        assert len(groups) == 1
        g = groups[0]
        assert g["segment_id"] == 1
        assert g["speaker_id"] == "speaker_a"
        assert g["target_duration_seconds"] == 5.0
        assert g["source_text"] == "Hello"
        # Must NOT contain char estimate fields
        assert "min_chars" not in g
        assert "max_chars" not in g
        assert "target_chars" not in g
        assert "dynamic_target_chars" not in g
        assert "density_factor" not in g


# ---------------------------------------------------------------------------
# Calibrated _build_groups
# ---------------------------------------------------------------------------
class TestBuildGroupsWithCalibration:
    """Tests that _build_groups uses calibrated chars_per_second."""

    @staticmethod
    def _build(lines, **kwargs):
        from services.gemini.translator import _build_groups
        return _build_groups(lines, max_segment_duration_ms=45_000, **kwargs)

    def test_target_chars_uses_natural_chinese_length(self):
        # Plan C: target_chars = source_word_count × 1.8.
        # "Hello world this is a test" = 6 words → 6 × 1.8 = 11 chars.
        lines = [_make_line(1, 0, 10_000, source_text="Hello world this is a test")]
        groups = self._build(lines)
        assert len(groups) == 1
        assert groups[0]["target_chars"] == 11

    def test_voice_cps_no_longer_changes_target_chars(self):
        # Plan C: voice_cps does NOT influence target_chars (the previous
        # behaviour caused same-content / different-voice to get wildly
        # different char budgets, which then forced pre-TTS rewrite).
        lines = [_make_line(1, 0, 10_000, source_text="Hello world this is a test")]
        groups_slow = self._build(lines, chars_per_second=3.0)
        groups_default = self._build(lines)
        groups_fast = self._build(lines, chars_per_second=6.0)
        assert groups_slow[0]["target_chars"] == groups_default[0]["target_chars"] == groups_fast[0]["target_chars"]

    def test_per_speaker_cps_no_longer_changes_target_chars(self):
        # Plan C: even per-speaker cps overrides do NOT change target_chars
        # for matching content. (Voice info is now passed to the LLM as a
        # reference field, not used for char budget computation.)
        lines = [
            _make_line(1, 0, 10_000, speaker_id="spk_a", source_text="Hello world this is"),
            _make_line(2, 10_000, 20_000, speaker_id="spk_b", source_text="Another test here too"),
        ]
        groups = self._build(
            lines,
            chars_per_second=4.5,
            chars_per_second_by_speaker={"spk_a": 6.0},
        )
        # Both speakers have 4 source words → 4 × 1.8 = 7 chars each.
        assert groups[0]["target_chars"] == 7
        assert groups[1]["target_chars"] == 7


# ---------------------------------------------------------------------------
# Plan C (2026-04-15): target_chars decoupled from voice_cps.
#
# Old contract: target_chars = duration × voice_cps × density_factor.
# Problem: same source content produced wildly different char budgets
# depending on the chosen voice (e.g. 71-word slow Munger segment got
# 95 chars under cartoon_elder 4.10 cps but density 0.75 cap, which then
# triggered pre-TTS rewrite to expand back to 130 chars — a wasted LLM call).
#
# New contract:
#   target_chars = source_word_count × 1.8        (natural Chinese length)
#   density_factor returns 1.0 always              (deprecated mechanism)
#   voice_cps is passed to LLM as REFERENCE only   (not used for char budget)
# ---------------------------------------------------------------------------
class TestDensityFactorAlwaysOne:
    """Plan C: density_factor must always return 1.0; deprecated mechanism."""

    @staticmethod
    def _density(**kwargs):
        from services.gemini.translator import _estimate_density_factor
        return _estimate_density_factor(**kwargs)

    def test_slow_segment_returns_one(self):
        # Slow Munger segment: source_wps 2.31 vs reference 3.07 → old code
        # would return 0.75. Plan C: must return 1.0.
        df, _src = self._density(
            source_words_per_second=2.31,
            reference_words_per_second=3.07,
            reference_source="speaker",
        )
        assert df == 1.0

    def test_fast_segment_returns_one(self):
        df, _src = self._density(
            source_words_per_second=5.0,
            reference_words_per_second=3.0,
            reference_source="speaker",
        )
        assert df == 1.0

    def test_normal_segment_returns_one(self):
        df, _src = self._density(
            source_words_per_second=3.0,
            reference_words_per_second=3.0,
            reference_source="speaker",
        )
        assert df == 1.0

    def test_zero_inputs_still_return_one(self):
        df, _src = self._density(
            source_words_per_second=0.0,
            reference_words_per_second=0.0,
            reference_source="global",
        )
        assert df == 1.0


class TestTargetCharsBasedOnSourceWordCount:
    """Plan C: target_chars = source_word_count × 1.8, INDEPENDENT of voice_cps."""

    @staticmethod
    def _estimate(**kwargs):
        from services.gemini.translator import _estimate_dynamic_target_chars
        return _estimate_dynamic_target_chars(**kwargs)

    def test_uses_source_word_count_x_1_8(self):
        # 71 English words → 128 Chinese chars (rounded)
        result = self._estimate(
            target_duration_ms=30_700,
            density_factor=1.0,
            chars_per_second=4.10,
            source_word_count=71,
        )
        assert result == 128  # round(71 * 1.8)

    def test_voice_cps_does_not_change_target_chars(self):
        # Same source, three different voices → identical char budget.
        slow = self._estimate(
            target_duration_ms=30_700, density_factor=1.0,
            chars_per_second=3.04, source_word_count=71,
        )
        mid = self._estimate(
            target_duration_ms=30_700, density_factor=1.0,
            chars_per_second=4.10, source_word_count=71,
        )
        fast = self._estimate(
            target_duration_ms=30_700, density_factor=1.0,
            chars_per_second=5.50, source_word_count=71,
        )
        assert slow == mid == fast == 128

    def test_density_factor_still_multiplied_when_provided(self):
        # If a caller passes a non-1 density (e.g. legacy callsite), it's still
        # honored mathematically — but production callers will get 1.0 from
        # _estimate_density_factor, so this is just a contract guarantee.
        result = self._estimate(
            target_duration_ms=10_000, density_factor=0.5,
            chars_per_second=4.10, source_word_count=20,
        )
        assert result == 18  # round(20 * 1.8 * 0.5)

    def test_zero_source_words_falls_back_to_duration_estimate(self):
        # Defensive: when source_word_count is unknown / zero (e.g. probe
        # path with empty text), fall back to legacy duration × cps so the
        # caller doesn't blow up with target_chars=0.
        result = self._estimate(
            target_duration_ms=10_000, density_factor=1.0,
            chars_per_second=4.50, source_word_count=0,
        )
        assert result == 45  # 10s * 4.5 (legacy fallback)

    def test_min_one_char(self):
        # Ultra-short source still yields at least 1 char.
        result = self._estimate(
            target_duration_ms=500, density_factor=1.0,
            chars_per_second=4.10, source_word_count=0,
        )
        assert result >= 1


class TestMungerScenarioPlanC:
    """Plan C end-to-end: Munger segment_029 should NOT trigger pre-TTS rewrite."""

    @staticmethod
    def _build(lines, **kwargs):
        from services.gemini.translator import _build_groups
        return _build_groups(lines, max_segment_duration_ms=45_000, **kwargs)

    def test_munger_slow_segment_target_chars_independent_of_voice(self):
        # Simulate Munger segment 029: 71 words / 30.7s → natural 128 chars.
        # Should be the same whether we pass cartoon_elder (4.10) or
        # storyteller (3.04) cps.
        lines = [_make_line(1, 0, 30_700, source_text=_words(71))]

        for cps in (3.04, 4.10, 5.50):
            groups = self._build(lines, chars_per_second=cps)
            assert groups[0]["target_chars"] == 128, (
                f"voice cps {cps} should not affect target_chars under Plan C"
            )
            # min/max range tracks target_chars (default ±15%)
            assert groups[0]["min_chars"] == 108  # round(128 * 0.85) = 108
            assert groups[0]["max_chars"] == 147  # round(128 * 1.15) = 147

    def test_pre_tts_estimate_aligns_with_target_under_plan_c(self):
        # Under Plan C, LLM is told to write ~128 chars.  At cartoon_elder
        # 4.10 cps that's TTS 31.2s vs target 30.7s → only +1.6% drift,
        # well below the 25% undershoot / 20% overshoot pre-rewrite thresholds.
        lines = [_make_line(1, 0, 30_700, source_text=_words(71))]
        groups = self._build(lines, chars_per_second=4.10)
        target_chars = groups[0]["target_chars"]
        # Estimated TTS ms when LLM writes exactly target_chars:
        estimated_tts_ms = int(target_chars / 4.10 * 1000)
        # Acceptable drift band: well within pre-rewrite thresholds.
        drift_pct = abs(estimated_tts_ms - 30_700) / 30_700
        assert drift_pct < 0.10, f"drift {drift_pct*100:.1f}% should be < 10%"
