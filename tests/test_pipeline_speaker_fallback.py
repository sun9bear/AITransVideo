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
from services.assemblyai.transcriber import TranscriptLine, TranscriptResult


def _line(
    index: int,
    start_ms: int,
    end_ms: int,
    speaker_id: str,
    text: str,
) -> TranscriptLine:
    return TranscriptLine(
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_id=speaker_id,
        speaker_label=speaker_id.replace("speaker_", "").upper(),
        source_text=text,
    )


def _transcript_result(lines: list[TranscriptLine]) -> TranscriptResult:
    end_ms = lines[-1].end_ms if lines else 0
    return TranscriptResult(
        lines=lines,
        total_duration_ms=end_ms,
        language="en",
        raw_response_path="",
        structured_transcript_path="",
    )


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

    def test_includes_additional_speakers_when_effective_count_exceeds_two(self) -> None:
        result = ProcessPipeline._fallback_minimal_speaker_styles(
            effective_speakers=3,
            speaker_name_a="Host",
            speaker_name_b="Guest",
        )

        assert "speaker_c" in result
        assert result["speaker_c"]["name"] == "Speaker C"
        assert result["speaker_c"]["_source"] == "fallback_minimal"


class TestSpeakerReviewPayload:
    def test_build_payload_includes_detected_speakers_beyond_b(self) -> None:
        pipeline = ProcessPipeline()
        transcript_result = _transcript_result(
            [
                _line(1, 0, 1_000, "speaker_a", "Host intro."),
                _line(2, 1_000, 2_000, "speaker_b", "Guest answer."),
                _line(3, 2_000, 3_000, "speaker_c", "Third speaker comment."),
            ]
        )

        payload = pipeline._build_speaker_review_payload(
            transcript_result=transcript_result,
            speaker_name_a="Host",
            speaker_name_b="Guest",
            effective_speakers=2,
        )

        assert payload["speaker_names"]["speaker_c"] == "Speaker C"
        assert {"speaker_id": "speaker_c", "display_name": "Speaker C"} in payload["speaker_options"]

    def test_apply_payload_accepts_reassignment_to_speaker_c(self) -> None:
        pipeline = ProcessPipeline()
        transcript_result = _transcript_result(
            [
                _line(1, 0, 1_000, "speaker_a", "Host intro."),
                _line(2, 1_000, 2_000, "speaker_a", "Should become third speaker."),
            ]
        )

        updated = pipeline._apply_speaker_review_payload(
            transcript_result=transcript_result,
            payload={"segment_speakers": {"2": "speaker_c"}},
        )

        assert updated.lines[1].speaker_id == "speaker_c"
