import json
from pathlib import Path

import pytest

from services.voice_asset import (
    DEFAULT_VOICE_VERIFICATION_SAMPLE_TEXT,
    VoiceAssetVerificationConfigurationError,
    VoiceAssetVerificationResult,
    VoiceAssetVerificationRuntimeError,
    VoiceAssetVerifier,
)
import services.tts_provider as tts_provider_module
from core.exceptions import TTSProviderTimeoutError


def _clear_tts_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AUTODUB_TTS_MODE",
        "AUTODUB_TTS_ENABLED",
        "AUTODUB_TTS_PROVIDER_NAME",
        "AUTODUB_TTS_MODEL_NAME",
        "AUTODUB_TTS_BASE_URL",
        "AUTODUB_TTS_API_KEY",
        "AUTODUB_TTS_API_PROTOCOL",
        "AUTODUB_TTS_VOICE_ID",
        "AUTODUB_TTS_VOICE_REGISTRY_PATH",
        "AUTODUB_TTS_CLONE_ENABLED",
        "AUTODUB_TTS_CLONE_BASE_URL",
        "AUTODUB_TTS_CLONE_MODEL_NAME",
        "AUTODUB_TTS_CLONE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_voice_asset_verifier_successfully_synthesizes_preview_audio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_tts_env(monkeypatch)
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "paths": {"voice_verification_root": "voice_bank/verification_audio"},
                "tts": {
                    "enabled": True,
                    "provider_name": "minimax_tts",
                    "model_name": "speech-2.8-turbo",
                    "base_url": "https://api.minimaxi.com",
                    "api_key": "secret",
                    "api_protocol": "minimax_t2a_v2",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tts_provider_module.OpenAICompatibleTTSProvider,
        "_post_tts_request",
        lambda self, payload: (b"RIFFdemo", "audio/wav"),
    )

    verifier = VoiceAssetVerifier.from_env(config_path=config_path)
    result = verifier.verify_voice(
        speaker_id="speaker_host",
        voice_id="clone_host_001",
    )

    assert isinstance(result, VoiceAssetVerificationResult)
    assert result.voice_id == "clone_host_001"
    assert result.sample_text == DEFAULT_VOICE_VERIFICATION_SAMPLE_TEXT
    assert Path(result.output_path).exists()
    assert "voice_bank" in result.output_path


def test_voice_asset_verifier_surfaces_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_tts_env(monkeypatch)
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "voice_clone": {
                    "enabled": True,
                    "base_url": "https://api.minimaxi.com",
                    "model_name": "speech-2.8-turbo",
                    "api_key": "secret",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tts_provider_module.OpenAICompatibleTTSProvider,
        "_post_tts_request",
        lambda self, payload: (_ for _ in ()).throw(TTSProviderTimeoutError("provider timed out")),
    )

    verifier = VoiceAssetVerifier.from_env(config_path=config_path)

    with pytest.raises(VoiceAssetVerificationRuntimeError, match="provider timed out"):
        verifier.verify_voice(
            speaker_id="speaker_host",
            voice_id="clone_host_001",
        )


def test_voice_asset_verifier_requires_minimax_protocol_for_voice_id_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_tts_env(monkeypatch)
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "tts": {
                    "enabled": True,
                    "provider_name": "openai_compatible_tts",
                    "model_name": "tts-model",
                    "base_url": "https://example.test/v1",
                    "api_key": "secret",
                    "api_protocol": "audio_speech_v1",
                }
            }
        ),
        encoding="utf-8",
    )
    verifier = VoiceAssetVerifier.from_env(config_path=config_path)
    verifier.config.api_protocol = "audio_speech_v1"

    with pytest.raises(VoiceAssetVerificationConfigurationError, match="requires MiniMax-compatible"):
        verifier.verify_voice(
            speaker_id="speaker_host",
            voice_id="clone_host_001",
        )
