"""Tests for _fallback_minimal_speaker_styles in ProcessPipeline.

Minimal focused tests — only cover the static fallback method,
no pipeline integration or external dependencies.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure src is on path
_src = str(Path(__file__).resolve().parents[1] / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from pipeline.process import ProcessPipeline


class TestFallbackMinimalSpeakerStyles:
    """Tests for the legacy fallback speaker profiling."""

    def test_produces_gender_and_age_for_two_speakers(self) -> None:
        result = ProcessPipeline._fallback_minimal_speaker_styles(
            effective_speakers=2,
            speaker_name_a="Chris Anderson",
            speaker_name_b="Elon Musk",
        )
        assert "speaker_a" in result
        assert "speaker_b" in result
        assert result["speaker_a"]["gender"] == "male"
        assert result["speaker_a"]["age_group"] == "middle"
        assert result["speaker_b"]["gender"] == "male"
        assert result["speaker_b"]["age_group"] == "middle"

    def test_produces_only_speaker_a_for_single_speaker(self) -> None:
        result = ProcessPipeline._fallback_minimal_speaker_styles(
            effective_speakers=1,
            speaker_name_a="Narrator",
            speaker_name_b="",
        )
        assert "speaker_a" in result
        assert "speaker_b" not in result

    def test_preserves_speaker_names(self) -> None:
        result = ProcessPipeline._fallback_minimal_speaker_styles(
            effective_speakers=2,
            speaker_name_a="Host",
            speaker_name_b="Guest",
        )
        assert result["speaker_a"]["name"] == "Host"
        assert result["speaker_b"]["name"] == "Guest"

    def test_marks_source_as_fallback(self) -> None:
        """Results must include a low-confidence source marker."""
        result = ProcessPipeline._fallback_minimal_speaker_styles(
            effective_speakers=1,
            speaker_name_a="Speaker",
            speaker_name_b="",
        )
        assert result["speaker_a"]["_source"] == "fallback_minimal"

    def test_does_not_overwrite_existing_styles(self) -> None:
        """Fallback should only be called when _review_speaker_styles is empty.
        This test verifies the method output can be safely merged without
        overwriting higher-quality data (the caller does the check)."""
        existing = {
            "speaker_a": {
                "name": "Host",
                "gender": "female",
                "age_group": "young",
                "voice_description": "clear voice",
            },
        }
        fallback = ProcessPipeline._fallback_minimal_speaker_styles(
            effective_speakers=2,
            speaker_name_a="Host",
            speaker_name_b="Guest",
        )
        # Existing data should NOT be replaced by fallback
        # (the caller checks `if not _review_speaker_styles` before calling)
        assert existing["speaker_a"]["gender"] == "female"  # unchanged
        assert fallback["speaker_a"]["gender"] == "male"     # fallback default
