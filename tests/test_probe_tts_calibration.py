"""Tests for probe TTS calibration: segment selection, _build_probe_groups, and
calibrated _build_groups.
"""
from __future__ import annotations

from dataclasses import dataclass

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


# ---------------------------------------------------------------------------
# _select_probe_segments
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
        lines = [_make_line(1, 0, 5000), _make_line(2, 5000, 10000)]
        assert self._select(lines) == []

    def test_skips_first_and_last(self):
        lines = [
            _make_line(1, 0, 5000),       # first — skipped
            _make_line(2, 5000, 10000),    # middle, 5s — candidate
            _make_line(3, 10000, 15000),   # middle, 5s — candidate
            _make_line(4, 15000, 20000),   # middle, 5s — candidate
            _make_line(5, 20000, 25000),   # last — skipped
        ]
        result = self._select(lines)
        indices = [l.index for l in result]
        assert 1 not in indices
        assert 5 not in indices
        assert len(result) == 3

    def test_filters_by_duration(self):
        lines = [
            _make_line(1, 0, 1000),        # first
            _make_line(2, 1000, 2000),      # 1s — too short
            _make_line(3, 2000, 7000),      # 5s — good
            _make_line(4, 7000, 16000),     # 9s — too long
            _make_line(5, 16000, 21000),    # 5s — good
            _make_line(6, 21000, 26000),    # last
        ]
        result = self._select(lines)
        indices = [l.index for l in result]
        assert 2 not in indices  # too short
        assert 4 not in indices  # too long
        assert 3 in indices
        assert 5 in indices

    def test_per_speaker_limit(self):
        lines = [_make_line(0, 0, 1000)]  # first (skipped)
        # 6 lines for speaker_a, 6 for speaker_b — all 5s each
        for i in range(1, 13):
            spk = "speaker_a" if i <= 6 else "speaker_b"
            lines.append(_make_line(i, i * 5000, (i + 1) * 5000, speaker_id=spk))
        lines.append(_make_line(13, 65000, 70000))  # last (skipped)

        result = self._select(lines, per_speaker=3, max_total=10)
        # Each speaker should contribute at most 3
        count_a = sum(1 for l in result if l.speaker_id == "speaker_a")
        count_b = sum(1 for l in result if l.speaker_id == "speaker_b")
        assert count_a <= 3
        assert count_b <= 3
        assert len(result) <= 10

    def test_max_total_cap(self):
        lines = [_make_line(0, 0, 1000)]
        for i in range(1, 20):
            lines.append(_make_line(i, i * 5000, (i + 1) * 5000))
        lines.append(_make_line(20, 100000, 105000))

        result = self._select(lines, max_total=5)
        assert len(result) <= 5

    def test_min_total_backfill(self):
        # Only 2 candidates from 1 speaker, min_total=3 not met — backfill
        lines = [
            _make_line(1, 0, 1000),
            _make_line(2, 1000, 5000, speaker_id="a"),
            _make_line(3, 5000, 9000, speaker_id="a"),
            _make_line(4, 9000, 13000, speaker_id="a"),
            _make_line(5, 13000, 18000),
        ]
        result = self._select(lines, per_speaker=2, min_total=3)
        assert len(result) >= 3


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

    def test_default_uses_4_5(self):
        lines = [_make_line(1, 0, 10_000, source_text="Hello world this is a test")]
        groups = self._build(lines)
        # 10s * 4.5 = 45 base chars (before density adjustment)
        # With density ≈ 1.0 for median-speed segment, target_chars ≈ 45
        assert len(groups) == 1
        # Verify the target_chars is in the ballpark of 4.5 * 10 = 45
        target = groups[0]["target_chars"]
        assert 30 <= target <= 60  # allow density adjustment

    def test_calibrated_changes_target_chars(self):
        lines = [_make_line(1, 0, 10_000, source_text="Hello world this is a test")]
        groups_default = self._build(lines)
        groups_fast = self._build(lines, chars_per_second=6.0)
        # Faster TTS → more chars needed
        assert groups_fast[0]["target_chars"] > groups_default[0]["target_chars"]

    def test_per_speaker_override(self):
        lines = [
            _make_line(1, 0, 10_000, speaker_id="spk_a", source_text="Hello world this is"),
            _make_line(2, 10_000, 20_000, speaker_id="spk_b", source_text="Another test here now"),
        ]
        groups = self._build(
            lines,
            chars_per_second=4.5,
            chars_per_second_by_speaker={"spk_a": 6.0},
        )
        # spk_a should get higher target_chars due to 6.0 override
        # spk_b falls back to global 4.5
        assert groups[0]["target_chars"] > groups[1]["target_chars"]


# ---------------------------------------------------------------------------
# _estimate_dynamic_target_chars with chars_per_second
# ---------------------------------------------------------------------------
class TestEstimateDynamicTargetCharsCalibrated:
    @staticmethod
    def _estimate(**kwargs):
        from services.gemini.translator import _estimate_dynamic_target_chars
        return _estimate_dynamic_target_chars(**kwargs)

    def test_default_4_5(self):
        result = self._estimate(target_duration_ms=10_000, density_factor=1.0)
        assert result == 45  # 10s * 4.5

    def test_custom_chars_per_second(self):
        result = self._estimate(
            target_duration_ms=10_000,
            density_factor=1.0,
            chars_per_second=6.0,
        )
        assert result == 60  # 10s * 6.0

    def test_density_factor_applied(self):
        result = self._estimate(
            target_duration_ms=10_000,
            density_factor=0.8,
            chars_per_second=5.0,
        )
        assert result == 40  # 10s * 5.0 * 0.8
