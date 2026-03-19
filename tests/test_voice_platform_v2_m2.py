import json
from pathlib import Path

import pytest

from core.models import SemanticBlock
from services.cache_manager import CacheManager
from services.tts_provider import OpenAICompatibleTTSProvider, RealTTSProviderConfig
from services.voice_clone import VoiceCloneConfig
from services.voice_registry import VoiceRegistry, VoiceResolver


def _build_block(*, speaker_id: str = "speaker_host") -> SemanticBlock:
    return SemanticBlock(
        block_id="block_v2_m2",
        speaker_id=speaker_id,
        speaker_name="Host" if speaker_id else None,
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="V2 M2 test block",
    )


def test_voice_registry_loads_legacy_records_and_rewrites_runtime_metadata(tmp_path: Path) -> None:
    registry_path = tmp_path / "voice_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "speakers": {
                    "speaker_host": {
                        "speaker_name": "Host",
                        "default_voice_id": "legacy_clone_001",
                        "default_voice_type": "cloned",
                        "voices": [
                            {
                                "voice_id": "legacy_clone_001",
                                "voice_type": "cloned",
                                "provider": "minimax_voice_clone",
                                "label": "Legacy Clone",
                                "created_at": "2026-03-14T00:00:00+00:00",
                            }
                        ],
                    }
                },
                "project_defaults": {
                    "default_builtin_voice": {
                        "voice_id": "project_builtin_001",
                        "voice_type": "builtin",
                        "provider": "minimax_tts",
                        "label": "Project Builtin",
                        "created_at": "2026-03-14T00:01:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    registry = VoiceRegistry(str(registry_path))
    profile = registry.get_speaker_profile("speaker_host")
    project_default = registry.get_project_default_builtin_voice()

    assert profile is not None
    assert profile.voices[0].provider == "minimax_voice_clone"
    assert profile.voices[0].tts_provider == "minimax_tts"
    assert profile.voices[0].platform == "minimax_domestic"
    assert project_default is not None
    assert project_default.provider == "minimax_tts"
    assert project_default.tts_provider == "minimax_tts"
    assert project_default.platform == "minimax_domestic"

    registry.save(registry.load())
    rewritten_payload = json.loads(registry_path.read_text(encoding="utf-8"))

    speaker_voice_payload = rewritten_payload["speakers"]["speaker_host"]["voices"][0]
    project_default_payload = rewritten_payload["project_defaults"]["default_builtin_voice"]
    assert speaker_voice_payload["provider"] == "minimax_voice_clone"
    assert speaker_voice_payload["tts_provider"] == "minimax_tts"
    assert speaker_voice_payload["platform"] == "minimax_domestic"
    assert project_default_payload["provider"] == "minimax_tts"
    assert project_default_payload["tts_provider"] == "minimax_tts"
    assert project_default_payload["platform"] == "minimax_domestic"


def test_voice_resolver_filters_by_tts_provider_and_platform(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    resolver = VoiceResolver(registry)
    registry.register_voice(
        "speaker_host",
        speaker_name="Host",
        voice_id="clone_global_001",
        voice_type="cloned",
        provider="minimax_voice_clone",
        tts_provider="minimax_tts",
        platform="minimax_global",
        label="Host Global Clone",
        created_at="2026-03-14T00:05:00+00:00",
        set_default=True,
    )
    registry.set_project_default_builtin_voice(
        voice_id="project_domestic_001",
        provider="minimax_tts",
        tts_provider="minimax_tts",
        platform="minimax_domestic",
        label="Project Domestic Builtin",
        created_at="2026-03-14T00:06:00+00:00",
    )

    compatible_resolution = resolver.resolve(
        "speaker_host",
        tts_provider="minimax_tts",
        platform="minimax_domestic",
    )
    incompatible_resolution = resolver.resolve(
        "speaker_host",
        tts_provider="openai_compatible_tts",
        platform="default",
    )

    assert compatible_resolution.status == "resolved"
    assert compatible_resolution.source == "project_default_builtin"
    assert compatible_resolution.voice_id == "project_domestic_001"
    assert compatible_resolution.tts_provider == "minimax_tts"
    assert compatible_resolution.platform == "minimax_domestic"
    assert incompatible_resolution.status == "unresolved"
    assert incompatible_resolution.voice_id is None


def test_real_tts_provider_keeps_env_voice_id_fallback_when_registry_binding_is_incompatible(
    tmp_path: Path,
) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    registry.register_voice(
        "speaker_host",
        speaker_name="Host",
        voice_id="clone_global_001",
        voice_type="cloned",
        provider="minimax_voice_clone",
        tts_provider="minimax_tts",
        platform="minimax_global",
        label="Host Global Clone",
        created_at="2026-03-14T00:10:00+00:00",
        set_default=True,
    )
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="minimax_tts",
            tts_provider="minimax_tts",
            platform="minimax_domestic",
            model_name="speech-02-turbo",
            base_url="https://api.minimaxi.com/v1",
            api_key="secret",
            voice_id="vt_domestic_env_001",
            voice_registry_path=str(tmp_path / "voice_registry.json"),
            api_protocol="minimax_t2a_v2",
        ),
    )

    resolved_voice = provider.resolve_block_voice(_build_block())
    cache_context = provider.get_cache_context()
    runtime_context = provider.get_block_runtime_context(_build_block())

    assert resolved_voice.resolved is True
    assert resolved_voice.voice_id == "vt_domestic_env_001"
    assert resolved_voice.source == "env_fallback"
    assert cache_context["tts_provider"] == "minimax_tts"
    assert cache_context["platform"] == "minimax_domestic"
    assert runtime_context["version_context"]["tts_provider"] == "minimax_tts"
    assert runtime_context["version_context"]["platform"] == "minimax_domestic"


def test_real_tts_provider_prefers_api_key_env_var_over_legacy_file_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "tts": {
                    "enabled": True,
                    "provider_name": "minimax_tts",
                    "tts_provider": "minimax_tts",
                    "platform": "minimax_domestic",
                    "model_name": "speech-2.8-turbo",
                    "base_url": "https://api.minimaxi.com",
                    "api_key": "legacy-file-secret",
                    "api_key_env_var": "AUTODUB_TTS_API_KEY_ALT",
                    "api_protocol": "minimax_t2a_v2",
                    "voice_id": "config_voice_001",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("AUTODUB_TTS_API_KEY", raising=False)
    monkeypatch.setenv("AUTODUB_TTS_API_KEY_ALT", "env-priority-secret")

    config = RealTTSProviderConfig.from_env(config_path=config_path)

    assert config.resolved_api_key() == "env-priority-secret"
    assert config.api_key_source == "process:AUTODUB_TTS_API_KEY_ALT"


def test_voice_clone_config_prefers_api_key_env_var_over_legacy_file_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "voice_clone": {
                    "enabled": True,
                    "base_url": "https://clone.example/v1",
                    "model_name": "clone-model",
                    "api_key": "legacy-clone-secret",
                    "api_key_env_var": "AUTODUB_TTS_CLONE_API_KEY_ALT",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("AUTODUB_TTS_CLONE_API_KEY", raising=False)
    monkeypatch.delenv("AUTODUB_TTS_API_KEY", raising=False)
    monkeypatch.setenv("AUTODUB_TTS_CLONE_API_KEY_ALT", "env-clone-secret")

    config = VoiceCloneConfig.from_env(config_path=config_path)
    summary = config.build_diagnostic_summary()

    assert config.resolved_api_key() == "env-clone-secret"
    assert config.api_key_source == "process:AUTODUB_TTS_CLONE_API_KEY_ALT"
    assert summary["api_key_source"] == "process:AUTODUB_TTS_CLONE_API_KEY_ALT"


def test_tts_platform_dimensions_change_cache_key(tmp_path: Path) -> None:
    cache_manager = CacheManager(str(tmp_path / "project_cache.json"))
    block = _build_block()

    domestic_hash = cache_manager.build_tts_hash(
        block,
        provider_name="minimax_tts",
        voice_name="vt_voice_001",
        model_name="speech-02-turbo",
        version_context={
            "api_protocol": "minimax_t2a_v2",
            "tts_provider": "minimax_tts",
            "platform": "minimax_domestic",
        },
    )
    global_hash = cache_manager.build_tts_hash(
        block,
        provider_name="minimax_tts",
        voice_name="vt_voice_001",
        model_name="speech-02-turbo",
        version_context={
            "api_protocol": "minimax_t2a_v2",
            "tts_provider": "minimax_tts",
            "platform": "minimax_global",
        },
    )

    assert domestic_hash != global_hash
