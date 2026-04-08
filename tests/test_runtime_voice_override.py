"""Tests for _apply_runtime_voice_overrides with real DubbingSegment objects.

Regression: DubbingSegment is @dataclass(slots=True), so assigning an
attribute not in the slot list raises AttributeError.  tts_provider was
added to the slot list to fix this.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.gemini.translator import DubbingSegment


def _seg(sid: str = "speaker_a", voice_id: str = "") -> DubbingSegment:
    return DubbingSegment(
        segment_id=1,
        speaker_id=sid,
        display_name="Test",
        voice_id=voice_id,
        start_ms=0,
        end_ms=5000,
        target_duration_ms=5000,
        source_text="hello",
        cn_text="你好",
    )


def _get_override_fn():
    """Import the method from the pipeline class."""
    from pipeline.process import ProcessPipeline
    proc = ProcessPipeline.__new__(ProcessPipeline)
    return proc._apply_runtime_voice_overrides


class TestPerSpeakerProviderOverride:
    """P1 regression: tts_provider assignment must not crash on real DubbingSegment."""

    def test_sets_tts_provider_on_real_segment(self) -> None:
        seg = _seg("speaker_a")
        override = _get_override_fn()
        override(
            [seg],
            voice_id_a="longanwen_v3",
            display_name_a="Alice",
            voice_id_b=None,
            display_name_b="",
            speaker_voices={"speaker_a": "longanwen_v3"},
            speaker_providers={"speaker_a": "cosyvoice"},
        )
        assert seg.voice_id == "longanwen_v3"
        assert seg.tts_provider == "cosyvoice"

    def test_mixed_providers_two_speakers(self) -> None:
        seg_a = _seg("speaker_a")
        seg_b = _seg("speaker_b")
        override = _get_override_fn()
        override(
            [seg_a, seg_b],
            voice_id_a="longanwen_v3",
            display_name_a="Alice",
            voice_id_b="English_radiant_girl",
            display_name_b="Bob",
            speaker_voices={
                "speaker_a": "longanwen_v3",
                "speaker_b": "English_radiant_girl",
            },
            speaker_providers={
                "speaker_a": "cosyvoice",
                "speaker_b": "minimax",
            },
        )
        assert seg_a.voice_id == "longanwen_v3"
        assert seg_a.tts_provider == "cosyvoice"
        assert seg_b.voice_id == "English_radiant_girl"
        assert seg_b.tts_provider == "minimax"

    def test_no_provider_override_leaves_empty(self) -> None:
        """When speaker_providers is empty/None, tts_provider stays default ('')."""
        seg = _seg("speaker_a")
        override = _get_override_fn()
        override(
            [seg],
            voice_id_a="longanyang",
            display_name_a="Alice",
            voice_id_b=None,
            display_name_b="",
            speaker_voices={"speaker_a": "longanyang"},
        )
        assert seg.voice_id == "longanyang"
        assert seg.tts_provider == ""  # no override → stays default

    def test_tts_provider_field_exists_on_dataclass(self) -> None:
        """tts_provider is a real slot, not a dynamic attribute."""
        seg = _seg()
        assert hasattr(seg, "tts_provider")
        seg.tts_provider = "volcengine"
        assert seg.tts_provider == "volcengine"


class TestVoiceProviderCompat:
    """P2b: voice_id / tts_provider mismatch is rejected at approval time."""

    def test_cosyvoice_voice_with_volcengine_provider_rejected(self) -> None:
        from services.jobs.review_actions import _validate_voice_provider_compat
        with pytest.raises(ValueError, match="不是豆包音色"):
            _validate_voice_provider_compat("longanwen_v3", "volcengine")

    def test_volcengine_voice_with_cosyvoice_provider_rejected(self) -> None:
        from services.jobs.review_actions import _validate_voice_provider_compat
        with pytest.raises(ValueError, match="不是 CosyVoice 音色"):
            _validate_voice_provider_compat("ICL_zh_male_shenmi_v1_tob", "cosyvoice")

    def test_cosyvoice_voice_with_cosyvoice_provider_accepted(self) -> None:
        from services.jobs.review_actions import _validate_voice_provider_compat
        _validate_voice_provider_compat("longanwen_v3", "cosyvoice")  # no exception

    def test_volcengine_voice_with_volcengine_provider_accepted(self) -> None:
        from services.jobs.review_actions import _validate_voice_provider_compat
        _validate_voice_provider_compat("ICL_zh_male_shenmi_v1_tob", "volcengine")  # no exception

    def test_any_voice_with_minimax_provider_accepted(self) -> None:
        """MiniMax has heterogeneous IDs — no strict pattern check."""
        from services.jobs.review_actions import _validate_voice_provider_compat
        _validate_voice_provider_compat("Wise_Woman", "minimax")
        _validate_voice_provider_compat("moss_audio_abc123", "minimax")
        _validate_voice_provider_compat("longanwen_v3", "minimax")  # even CosyVoice IDs pass for MiniMax

    def test_empty_provider_skips_validation(self) -> None:
        """Empty provider (backward compat) always passes."""
        from services.jobs.review_actions import _validate_voice_provider_compat
        _validate_voice_provider_compat("anything", "")  # no call — guarded by caller
