"""Tests for voice_match_resolver — shared voice matching entry point (B3)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.tts.voice_match_types import VoiceMatchRequest, VoiceMatchResult
from services.tts.voice_match_resolver import (
    UnsupportedProviderError,
    resolve_voice_match,
)


# ===================================================================
# Manual mode
# ===================================================================

class TestManualMode:
    def test_manual_mode_returns_explicit_voice(self) -> None:
        req = VoiceMatchRequest(
            tts_provider="volcengine",
            resource_id="seed-tts-2.0",
            mode="manual",
            explicit_voice_id="zh_female_vv_uranus_bigtts",
        )
        result = resolve_voice_match(req)

        assert result.voice_id == "zh_female_vv_uranus_bigtts"
        assert result.match_reason == "manual_selection"
        assert result.match_score == 1.0
        assert result.match_confidence == "high"

    def test_manual_mode_works_for_any_provider(self) -> None:
        """Manual mode should work regardless of provider name."""
        req = VoiceMatchRequest(
            tts_provider="any_unknown_provider",
            mode="manual",
            explicit_voice_id="some_voice",
        )
        result = resolve_voice_match(req)
        assert result.voice_id == "some_voice"

    def test_manual_mode_without_voice_id_falls_through(self) -> None:
        """Manual mode with no explicit_voice_id should NOT bypass — falls to provider dispatch."""
        req = VoiceMatchRequest(
            tts_provider="unknown_provider",
            mode="manual",
            explicit_voice_id=None,
        )
        # No explicit voice → not caught by manual bypass → hits provider dispatch → error
        with pytest.raises(UnsupportedProviderError):
            resolve_voice_match(req)


# ===================================================================
# VolcEngine dispatch
# ===================================================================

class TestVolcEngineDispatch:
    def test_volcengine_dispatches_to_selector(self) -> None:
        """volcengine provider dispatches to _dispatch_volcengine → volcengine_voice_selector."""
        import services.tts.voice_match_resolver as resolver_mod

        fake_result = VoiceMatchResult(
            voice_id="zh_male_test_moon_bigtts",
            match_reason="test",
            match_score=0.80,
            match_confidence="medium",
            backup_voices=(),
        )

        with patch.object(
            resolver_mod, "_dispatch_volcengine", return_value=fake_result,
        ) as mock_dispatch:
            req = VoiceMatchRequest(
                tts_provider="volcengine",
                resource_id="seed-tts-1.0",
                mode="auto",
                gender="male",
                age_group="middle",
                persona_style="serious",
            )
            result = resolve_voice_match(req)

        assert result.voice_id == "zh_male_test_moon_bigtts"
        mock_dispatch.assert_called_once_with(req)

    def test_volcengine_passes_resource_id_through_request(self) -> None:
        """resource_id is carried in the request object passed to _dispatch_volcengine."""
        import services.tts.voice_match_resolver as resolver_mod

        fake_result = VoiceMatchResult(
            voice_id="test_voice",
            match_reason="test",
            match_score=0.5,
            match_confidence="low",
        )

        with patch.object(
            resolver_mod, "_dispatch_volcengine", return_value=fake_result,
        ) as mock_dispatch:
            req = VoiceMatchRequest(
                tts_provider="volcengine",
                resource_id="seed-tts-2.0",
                mode="auto",
                gender="female",
            )
            resolve_voice_match(req)

        passed_req = mock_dispatch.call_args[0][0]
        assert passed_req.resource_id == "seed-tts-2.0"


# ===================================================================
# Unknown provider
# ===================================================================

class TestUnknownProvider:
    def test_unknown_provider_raises(self) -> None:
        req = VoiceMatchRequest(
            tts_provider="nonexistent_provider",
            mode="auto",
            gender="male",
        )
        with pytest.raises(UnsupportedProviderError, match="nonexistent_provider"):
            resolve_voice_match(req)

    def test_cosyvoice_dispatches_to_selector(self) -> None:
        """CosyVoice provider dispatches to _dispatch_cosyvoice → cosyvoice_voice_selector."""
        import services.tts.voice_match_resolver as resolver_mod

        fake_result = VoiceMatchResult(
            voice_id="longanyang",
            match_reason="test",
            match_score=0.60,
            match_confidence="medium",
            backup_voices=(),
        )

        with patch.object(
            resolver_mod, "_dispatch_cosyvoice", return_value=fake_result,
        ) as mock_dispatch:
            req = VoiceMatchRequest(
                tts_provider="cosyvoice",
                mode="auto",
                gender="female",
                age_group="middle",
                persona_style="warm",
            )
            result = resolve_voice_match(req)

        assert result.voice_id == "longanyang"
        mock_dispatch.assert_called_once_with(req)


# ===================================================================
# VoiceMatchRequest / VoiceMatchResult structure
# ===================================================================

class TestTypes:
    def test_request_defaults(self) -> None:
        req = VoiceMatchRequest(tts_provider="volcengine")
        assert req.mode == "auto"
        assert req.resource_id is None
        assert req.gender is None
        assert req.explicit_voice_id is None

    def test_result_defaults(self) -> None:
        result = VoiceMatchResult(
            voice_id="v",
            match_reason="r",
            match_score=0.5,
            match_confidence="low",
        )
        assert result.backup_voices == ()

    def test_request_is_frozen(self) -> None:
        req = VoiceMatchRequest(tts_provider="volcengine")
        with pytest.raises(AttributeError):
            req.tts_provider = "other"  # type: ignore[misc]

    def test_result_is_frozen(self) -> None:
        result = VoiceMatchResult(
            voice_id="v", match_reason="r", match_score=0.5, match_confidence="low",
        )
        with pytest.raises(AttributeError):
            result.voice_id = "other"  # type: ignore[misc]
