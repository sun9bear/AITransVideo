"""Tests for _build_voice_selection_review_payload — no NameError, provider branches work."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class FakeLine:
    index: int = 0
    speaker_id: str = "speaker_a"
    start_ms: int = 0
    end_ms: int = 5000
    source_text: str = "Hello"


@dataclass
class FakeSegment:
    speaker_id: str = "speaker_a"
    gender: str = "female"
    age_group: str = "middle"
    persona_style: str = "warm"
    energy_level: str = "medium"


@dataclass
class FakeTranscriptResult:
    lines: list = field(default_factory=lambda: [FakeLine()])


@dataclass
class FakeTranslationResult:
    segments: list = field(default_factory=lambda: [FakeSegment()])


@pytest.fixture(autouse=True)
def _mock_network():
    """Block all Gateway + provider HTTP calls."""
    with patch("services.tts.voice_reranker.requests.get", side_effect=ConnectionError("test")), \
         patch("services.tts.cosyvoice_voice_catalog._fetch_cosyvoice_from_gateway", side_effect=ConnectionError("test")), \
         patch("services.tts.volcengine_voice_catalog._fetch_from_gateway", side_effect=ConnectionError("test")):
        yield


def _get_pipeline_class():
    """Import the pipeline class lazily to avoid import-time side effects."""
    from pipeline.process import ProcessPipeline
    return ProcessPipeline


class TestPayloadVolcengineStudio:
    """VolcEngine + Studio should not NameError on service_mode."""

    def test_volcengine_studio_builds_payload(self) -> None:
        proc = _get_pipeline_class().__new__(_get_pipeline_class())
        payload = proc._build_voice_selection_review_payload(
            transcript_result=FakeTranscriptResult(),
            translation_result=FakeTranslationResult(),
            tts_provider="volcengine",
            service_mode="studio",
            source_audio_path="/tmp/src.wav",
            effective_speakers=1,
            speaker_names={"speaker_a": "Alice"},
        )
        assert payload["tts_provider"] == "volcengine"
        assert len(payload["speakers"]) == 1
        assert isinstance(payload["available_voices"], list)

    def test_volcengine_express_builds_payload(self) -> None:
        proc = _get_pipeline_class().__new__(_get_pipeline_class())
        payload = proc._build_voice_selection_review_payload(
            transcript_result=FakeTranscriptResult(),
            translation_result=FakeTranslationResult(),
            tts_provider="volcengine",
            service_mode="express",
            source_audio_path="/tmp/src.wav",
            effective_speakers=1,
            speaker_names={"speaker_a": "Alice"},
        )
        assert payload["tts_provider"] == "volcengine"


class TestPayloadCosyvoice:
    """CosyVoice branch should work and populate available_voices."""

    def test_cosyvoice_builds_payload(self) -> None:
        proc = _get_pipeline_class().__new__(_get_pipeline_class())
        payload = proc._build_voice_selection_review_payload(
            transcript_result=FakeTranscriptResult(),
            translation_result=FakeTranslationResult(),
            tts_provider="cosyvoice",
            service_mode="studio",
            source_audio_path="/tmp/src.wav",
            effective_speakers=1,
            speaker_names={"speaker_a": "Alice"},
        )
        assert payload["tts_provider"] == "cosyvoice"
        assert isinstance(payload["available_voices"], list)
        # Static fallback should still provide voice options
        assert len(payload["available_voices"]) > 0


class TestPayloadMinimax:
    """MiniMax should populate available_voices from static catalog."""

    def test_minimax_builds_payload(self) -> None:
        proc = _get_pipeline_class().__new__(_get_pipeline_class())
        payload = proc._build_voice_selection_review_payload(
            transcript_result=FakeTranscriptResult(),
            translation_result=FakeTranslationResult(),
            tts_provider="minimax",
            service_mode="studio",
            source_audio_path="/tmp/src.wav",
            effective_speakers=1,
            speaker_names={"speaker_a": "Alice"},
        )
        assert payload["tts_provider"] == "minimax"
        assert isinstance(payload["available_voices"], list)
        assert len(payload["available_voices"]) > 0
        # All should be Chinese voices
        for v in payload["available_voices"]:
            assert v["provider"] == "minimax"


class TestSpeakerNameMap:
    def test_additional_speaker_names_survive_merged_map(self) -> None:
        from pipeline.process import _merge_speaker_name_map

        proc = _get_pipeline_class().__new__(_get_pipeline_class())
        transcript_result = FakeTranscriptResult(
            lines=[
                FakeLine(index=1, speaker_id="speaker_a", source_text="Host line"),
                FakeLine(index=2, speaker_id="speaker_c", source_text="Third speaker line"),
            ]
        )
        translation_result = FakeTranslationResult(
            segments=[
                FakeSegment(speaker_id="speaker_a"),
                FakeSegment(speaker_id="speaker_c"),
            ]
        )

        payload = proc._build_voice_selection_review_payload(
            transcript_result=transcript_result,
            translation_result=translation_result,
            tts_provider="minimax",
            service_mode="studio",
            source_audio_path="/tmp/src.wav",
            effective_speakers=3,
            speaker_names=_merge_speaker_name_map(
                {"speaker_c": "Charlie"},
                "Alice",
                "Bob",
            ),
        )

        speaker_map = {speaker["speaker_id"]: speaker["speaker_name"] for speaker in payload["speakers"]}
        assert speaker_map["speaker_a"] == "Alice"
        assert speaker_map["speaker_c"] == "Charlie"
