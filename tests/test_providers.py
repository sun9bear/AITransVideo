import base64
from http.client import IncompleteRead
import json
import os
from pathlib import Path
from urllib import error

import pytest

from core.exceptions import (
    IngestionError,
    TTSConfigurationError,
    TTSInvalidAudioPayloadError,
    TTSError,
    TTSOutputFileWriteError,
    TTSProviderNetworkError,
    TTSProviderResponseFormatError,
    TTSProviderUnavailableError,
    TranslationConfigurationError,
    TranslationError,
    TranslationProviderLineCountError,
    TranslationProviderOutputError,
    TranslationProviderResponseFormatError,
    TranslationProviderUnavailableError,
    TTSProviderTimeoutError,
)
from core.models import SemanticBlock, SubtitleLine
from modules.ingestion.models import SubtitleSeed
from modules.ingestion.providers import (
    MemorySubtitleProvider,
    SubtitleSourceProvider,
    YouTubeSubtitleProviderSkeleton,
)
from modules.translation.providers import (
    OpenAICompatibleTranslationProvider,
    RealTranslationProviderConfig,
    RemoteTranslationProviderSkeleton,
    TranslationProvider,
    TranslationProviderSelectionConfig,
    classify_translation_error,
    resolve_translation_provider,
)
import modules.translation.providers as translation_provider_module
from modules.translation.router import TranslationChunkRouter, TranslationRouterConfig
from modules.translation.translator import MockTranslator
from modules.translation.translator import TranslationPipeline
from services.tts_provider import (
    OpenAICompatibleTTSProvider,
    RealTTSProviderConfig,
    RemoteTTSProviderSkeleton,
    TTSProvider,
    TTSProviderSelectionConfig,
    build_tts_block_runtime_context,
    classify_tts_error,
    resolve_tts_provider,
)
import services.tts_provider as tts_provider_module
from services.tts_service import MockTTSService
from services.voice_clone import VoiceCloneConfig
import services.voice_clone as voice_clone_module
from services.voice_registry import VoiceRegistry


def _build_env_readers(*, persisted: dict[str, str] | None = None):
    persisted = persisted or {}
    return lambda: iter(
        [
            ("process", os.getenv),
            ("user", lambda key: persisted.get(key)),
            ("machine", lambda key: None),
        ]
    )


def test_mock_providers_match_runtime_protocols(tmp_path: Path) -> None:
    ingestion_provider = MemorySubtitleProvider(
        [
            SubtitleSeed(
                index=1,
                start_ms=0,
                end_ms=1_000,
                en_text="Hello",
                speaker_id="speaker_1",
                speaker_name="Host",
            )
        ]
    )
    translation_provider = MockTranslator()
    tts_provider = MockTTSService(output_dir=str(tmp_path))

    assert isinstance(ingestion_provider, SubtitleSourceProvider)
    assert isinstance(translation_provider, TranslationProvider)
    assert isinstance(tts_provider, TTSProvider)
    assert ingestion_provider.load_subtitles()[0].speaker_id == "speaker_1"


def test_provider_skeletons_raise_clear_errors() -> None:
    with pytest.raises(IngestionError, match="skeleton is not connected"):
        YouTubeSubtitleProviderSkeleton("https://youtube.example/video").load_subtitles()

    with pytest.raises(TranslationError, match="skeleton is not connected"):
        RemoteTranslationProviderSkeleton().translate_batch([])

    with pytest.raises(TTSError, match="skeleton is not connected"):
        RemoteTTSProviderSkeleton().synthesize(
            SemanticBlock(
                block_id="block_provider",
                speaker_id="speaker_1",
                speaker_name="Host",
                original_srt_indices=[1],
                first_start_ms=0,
                last_end_ms=1_000,
                target_duration_ms=1_000,
                merged_cn_text="测试",
            )
        )


def test_real_translation_provider_requires_explicit_configuration() -> None:
    with pytest.raises(TranslationConfigurationError, match="model_name is required"):
        OpenAICompatibleTranslationProvider(
            RealTranslationProviderConfig(
                enabled=True,
                provider_name="openai_compatible",
                model_name=None,
                base_url="https://example.test/v1",
                api_key="secret",
            )
        )


def test_real_translation_provider_config_reads_autodub_local_json(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "translation": {
                    "enabled": True,
                    "mode": "real",
                    "provider_name": "openai_compatible",
                    "model_name": "gpt-4.1-mini",
                    "target_language": "zh-CN",
                    "base_url": "https://translation.example/v1",
                    "api_key": "config-secret",
                    "timeout_seconds": 45,
                    "provider_variant": "translation_via_config",
                }
            }
        ),
        encoding="utf-8",
    )

    selection = TranslationProviderSelectionConfig.from_env(config_path=config_path)

    assert selection.mode == "real"
    assert selection.real.enabled is True
    assert selection.real.model_name == "gpt-4.1-mini"
    assert selection.real.base_url == "https://translation.example/v1"
    assert selection.real.resolved_api_key() == "config-secret"
    assert selection.real.provider_variant == "translation_via_config"
    assert selection.real.build_diagnostic_summary()["config_source"] == "autodub.local.json"
    assert selection.real.build_diagnostic_summary()["api_key_source_type"] == "legacy_file"


def test_real_translation_provider_env_overrides_autodub_local_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "translation": {
                    "enabled": True,
                    "mode": "mock",
                    "model_name": "config-model",
                    "base_url": "https://config.example/v1",
                    "api_key": "config-secret",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTODUB_TRANSLATION_MODE", "real")
    monkeypatch.setenv("AUTODUB_TRANSLATION_MODEL_NAME", "env-model")
    monkeypatch.setenv("AUTODUB_TRANSLATION_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("AUTODUB_TRANSLATION_API_KEY", "env-secret")

    selection = TranslationProviderSelectionConfig.from_env(config_path=config_path)

    assert selection.mode == "real"
    assert selection.real.model_name == "env-model"
    assert selection.real.base_url == "https://env.example/v1"
    assert selection.real.resolved_api_key() == "env-secret"
    assert selection.real.build_diagnostic_summary()["config_source"] == "env"
    assert selection.real.build_diagnostic_summary()["api_key_source_type"] == "direct_env"


def test_real_translation_provider_reports_api_key_env_var_source_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "translation": {
                    "enabled": True,
                    "mode": "real",
                    "provider_name": "openai_compatible",
                    "model_name": "gpt-4.1-mini",
                    "base_url": "https://translation.example/v1",
                    "api_key_env_var": "AUTODUB_TRANSLATION_API_KEY_ALT",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTODUB_TRANSLATION_API_KEY_ALT", "env-secret")

    selection = TranslationProviderSelectionConfig.from_env(config_path=config_path)

    assert selection.real.resolved_api_key() == "env-secret"
    assert selection.real.build_diagnostic_summary()["api_key_source_type"] == "api_key_env_var"


def test_real_translation_provider_reports_persisted_env_source_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "translation": {
                    "enabled": True,
                    "mode": "real",
                    "provider_name": "openai_compatible",
                    "model_name": "gpt-4.1-mini",
                    "base_url": "https://translation.example/v1",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        translation_provider_module.config_loader,
        "iter_env_readers",
        _build_env_readers(persisted={"AUTODUB_TRANSLATION_API_KEY": "persisted-secret"}),
    )

    selection = TranslationProviderSelectionConfig.from_env(config_path=config_path)

    assert selection.real.resolved_api_key() == "persisted-secret"
    assert selection.real.build_diagnostic_summary()["api_key_source_type"] == "persisted_env"


def test_real_translation_provider_reports_missing_api_key_source_type(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "translation": {
                    "enabled": True,
                    "mode": "real",
                    "provider_name": "openai_compatible",
                    "model_name": "gpt-4.1-mini",
                    "base_url": "https://translation.example/v1",
                }
            }
        ),
        encoding="utf-8",
    )

    selection = TranslationProviderSelectionConfig.from_env(config_path=config_path)

    assert selection.real.resolved_api_key() is None
    assert selection.real.build_diagnostic_summary()["api_key_source_type"] == "missing"


def test_real_translation_provider_can_explicitly_fallback_to_mock() -> None:
    binding = resolve_translation_provider(
        TranslationProviderSelectionConfig(
            mode="real",
            real=RealTranslationProviderConfig(
                enabled=True,
                provider_name="openai_compatible",
                model_name=None,
                base_url="https://example.test/v1",
                api_key="secret",
                fallback_to_mock=True,
            ),
        ),
        mock_provider=MockTranslator(),
    )

    assert isinstance(binding.provider, TranslationProvider)
    assert binding.provider_name == "mock_translator"
    assert binding.mode == "mock_fallback"
    assert binding.fallback_applied is True


def test_translation_provider_selection_switches_between_mock_and_real() -> None:
    mock_binding = resolve_translation_provider(
        TranslationProviderSelectionConfig(mode="mock"),
        mock_provider=MockTranslator(),
    )
    real_binding = resolve_translation_provider(
        TranslationProviderSelectionConfig(
            mode="real",
            real=RealTranslationProviderConfig(
                enabled=True,
                provider_name="openai_compatible",
                model_name="demo-model",
                base_url="https://example.test/v1",
                api_key="secret",
            ),
        ),
        mock_provider=MockTranslator(),
    )

    assert mock_binding.mode == "mock"
    assert mock_binding.provider_name == "mock_translator"
    assert real_binding.mode == "real"
    assert real_binding.provider_name == "openai_compatible"
    assert real_binding.model_name == "demo-model"
    assert real_binding.version_context["request_contract"] == "subtitle_line_batch_v2"
    assert real_binding.version_context["output_contract"] == "translated_lines_line_level_v2"
    assert real_binding.version_context["line_count_policy"] == "strict_match_required"


def test_real_translation_provider_builds_line_batch_request_payload() -> None:
    provider = OpenAICompatibleTranslationProvider(
        RealTranslationProviderConfig(
            enabled=True,
            provider_name="openai_compatible",
            model_name="demo-model",
            base_url="https://example.test/v1",
            api_key="secret",
        )
    )
    payload = provider._build_request_payload(
        [
            SubtitleLine(1, 0, 800, "speaker_host", "Host", "Hello", ""),
            SubtitleLine(2, 900, 1_700, "speaker_host", "Host", "World", ""),
        ]
    )

    assert payload["model"] == "demo-model"
    assert payload["response_format"] == {"type": "json_object"}
    messages = payload["messages"]
    assert isinstance(messages, list)
    assert "line batch" in str(messages[0]["content"]).lower()
    assert "Do not summarize." in str(messages[0]["content"])
    assert "Do not omit." in str(messages[0]["content"])
    assert "Do not add speaker names" in str(messages[0]["content"])

    user_payload = json.loads(str(messages[1]["content"]))
    assert user_payload["target_language"] == "zh-CN"
    assert user_payload["line_count"] == 2
    assert user_payload["input_contract"]["type"] == "line_batch"
    assert user_payload["input_contract"]["preserve_line_count"] is True
    assert user_payload["lines"] == [
        {"index": 1, "text": "Hello"},
        {"index": 2, "text": "World"},
    ]


def test_real_translation_provider_rejects_invalid_provider_output() -> None:
    provider = OpenAICompatibleTranslationProvider(
        RealTranslationProviderConfig(
            enabled=True,
            provider_name="openai_compatible",
            model_name="demo-model",
            base_url="https://example.test/v1",
            api_key="secret",
        )
    )

    with pytest.raises(TranslationProviderOutputError, match="translated_lines list"):
        provider._parse_translation_list('{"unexpected":"value"}')


def test_real_translation_provider_parses_common_wrapped_shapes() -> None:
    provider = OpenAICompatibleTranslationProvider(
        RealTranslationProviderConfig(
            enabled=True,
            provider_name="openai_compatible",
            model_name="demo-model",
            base_url="https://example.test/v1",
            api_key="secret",
        )
    )

    content_from_parts = provider._extract_assistant_content(
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": '```json\n{"output":{"translated_lines":["A","B"]}}\n```'}
                        ]
                    }
                }
            ]
        }
    )
    content_from_dict_message = provider._extract_assistant_content(
        {
            "choices": [
                {
                    "message": {
                        "content": {
                            "result": {
                                "translated_lines": [
                                    {"index": 1, "text": "A"},
                                    {"index": 2, "text": "B"},
                                ]
                            }
                        }
                    }
                }
            ]
        }
    )
    parsed_lines = provider._parse_translation_list('{"data":{"translations":["X","Y"]}}')
    parsed_wrapped_structured_lines = provider._parse_translation_list(
        '{"response":{"content":{"translated_lines":[{"index":1,"text":"A"},{"index":2,"text":"B"}]}}}'
    )
    parsed_double_wrapped_lines = provider._parse_translation_list('"{\\"translated_lines\\":[\\"M\\",\\"N\\"]}"')

    assert '"translated_lines"' in content_from_parts
    assert '"translated_lines"' in content_from_dict_message
    assert parsed_lines == ["X", "Y"]
    assert parsed_wrapped_structured_lines == ["A", "B"]
    assert parsed_double_wrapped_lines == ["M", "N"]


def test_real_translation_provider_accepts_structured_line_output_and_pipeline_writes_literal_cn_text() -> None:
    provider = OpenAICompatibleTranslationProvider(
        RealTranslationProviderConfig(
            enabled=True,
            provider_name="openai_compatible",
            model_name="demo-model",
            base_url="https://example.test/v1",
            api_key="secret",
        )
    )
    source_lines = [
        SubtitleLine(1, 0, 800, "speaker_host", "Host", "Hello", ""),
        SubtitleLine(2, 900, 1_700, "speaker_host", "Host", "World", ""),
    ]

    provider._post_chat_completion = lambda payload: {  # type: ignore[method-assign]
        "choices": [
            {
                "message": {
                    "content": {
                        "translated_lines": [
                            {"index": 1, "text": "```text\nCN:Hello\n```"},
                            {"index": 2, "text": "**CN:World**"},
                        ]
                    }
                }
            }
        ]
    }

    pipeline = TranslationPipeline(
        router=TranslationChunkRouter(TranslationRouterConfig(batch_size=2, max_batch_size=2)),
        translator=provider,
    )
    translated_lines = pipeline.translate_lines(source_lines)

    assert [line.literal_cn_text for line in translated_lines] == ["CN:Hello", "CN:World"]
    assert [line.cn_text for line in translated_lines] == ["CN:Hello", "CN:World"]
    assert [line.tts_cn_text for line in translated_lines] == ["", ""]


def test_real_translation_provider_rejects_line_count_mismatch() -> None:
    provider = OpenAICompatibleTranslationProvider(
        RealTranslationProviderConfig(
            enabled=True,
            provider_name="openai_compatible",
            model_name="demo-model",
            base_url="https://example.test/v1",
            api_key="secret",
        )
    )
    source_lines = [
        SubtitleLine(1, 0, 800, "speaker_host", "Host", "Hello", ""),
        SubtitleLine(2, 900, 1_700, "speaker_host", "Host", "World", ""),
    ]

    provider._post_chat_completion = lambda payload: {  # type: ignore[method-assign]
        "choices": [
            {
                "message": {
                    "content": '{"translated_lines":[{"index":1,"text":"CN:Hello"}]}',
                }
            }
        ]
    }

    with pytest.raises(TranslationProviderLineCountError, match="expected 2, got 1") as exc_info:
        provider.translate_batch(source_lines)

    assert classify_translation_error(exc_info.value) == {
        "error_type": "provider_output_line_count_mismatch",
        "retry_candidate": False,
    }


def test_real_translation_provider_rejects_explanatory_prefix_before_json() -> None:
    provider = OpenAICompatibleTranslationProvider(
        RealTranslationProviderConfig(
            enabled=True,
            provider_name="openai_compatible",
            model_name="demo-model",
            base_url="https://example.test/v1",
            api_key="secret",
        )
    )

    with pytest.raises(TranslationProviderOutputError, match="JSON only without explanatory prefix"):
        provider._parse_translation_list('Here is the translation:\n{"translated_lines":["A"]}')


def test_real_translation_provider_classifies_runtime_errors() -> None:
    unavailable = classify_translation_error(
        TranslationProviderUnavailableError("provider down")
    )
    bad_format = classify_translation_error(
        TranslationProviderResponseFormatError("bad envelope")
    )

    assert unavailable == {"error_type": "provider_unavailable", "retry_candidate": True}
    assert bad_format == {"error_type": "invalid_provider_response_format", "retry_candidate": False}


def test_real_translation_provider_retries_retry_candidate_errors_before_success() -> None:
    provider = OpenAICompatibleTranslationProvider(
        RealTranslationProviderConfig(
            enabled=True,
            provider_name="openai_compatible",
            model_name="demo-model",
            base_url="https://example.test/v1",
            api_key="secret",
            max_retries=2,
            retry_backoff_seconds=0.0,
        )
    )
    source_lines = [
        SubtitleLine(1, 0, 800, "speaker_host", "Host", "Hello", ""),
        SubtitleLine(2, 900, 1_700, "speaker_host", "Host", "World", ""),
    ]
    call_count = {"value": 0}

    def flaky_post(payload: dict[str, object]) -> dict[str, object]:
        del payload
        call_count["value"] += 1
        if call_count["value"] < 3:
            raise TranslationProviderUnavailableError("provider unavailable")
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"translated_lines":["CN:Hello","CN:World"]}',
                    }
                }
            ]
        }

    provider.reset_retry_report()
    provider._post_chat_completion = flaky_post  # type: ignore[method-assign]

    translated = provider.translate_batch(source_lines)
    retry_report = provider.get_retry_report()

    assert translated == ["CN:Hello", "CN:World"]
    assert call_count["value"] == 3
    assert retry_report["retry_attempted"] is True
    assert retry_report["retry_count"] == 2
    assert retry_report["retry_candidate"] is True
    assert retry_report["final_error_type"] is None
    assert retry_report["final_error_message"] is None


def test_real_translation_provider_does_not_retry_non_retry_candidate_errors() -> None:
    provider = OpenAICompatibleTranslationProvider(
        RealTranslationProviderConfig(
            enabled=True,
            provider_name="openai_compatible",
            model_name="demo-model",
            base_url="https://example.test/v1",
            api_key="secret",
            max_retries=2,
            retry_backoff_seconds=0.0,
        )
    )
    source_lines = [SubtitleLine(1, 0, 800, "speaker_host", "Host", "Hello", "")]
    call_count = {"value": 0}

    def malformed_post(payload: dict[str, object]) -> dict[str, object]:
        del payload
        call_count["value"] += 1
        return {}

    provider.reset_retry_report()
    provider._post_chat_completion = malformed_post  # type: ignore[method-assign]

    with pytest.raises(TranslationProviderResponseFormatError, match="missing choices"):
        provider.translate_batch(source_lines)

    retry_report = provider.get_retry_report()
    assert call_count["value"] == 1
    assert retry_report["retry_attempted"] is False
    assert retry_report["retry_count"] == 0
    assert retry_report["retry_candidate"] is False
    assert retry_report["final_error_type"] == "invalid_provider_response_format"
    assert retry_report["final_error_message"] == "Translation provider response is missing choices."


def test_real_translation_provider_surfaces_retry_exhaustion() -> None:
    provider = OpenAICompatibleTranslationProvider(
        RealTranslationProviderConfig(
            enabled=True,
            provider_name="openai_compatible",
            model_name="demo-model",
            base_url="https://example.test/v1",
            api_key="secret",
            max_retries=2,
            retry_backoff_seconds=0.0,
        )
    )
    source_lines = [SubtitleLine(1, 0, 800, "speaker_host", "Host", "Hello", "")]
    call_count = {"value": 0}

    def always_unavailable(payload: dict[str, object]) -> dict[str, object]:
        del payload
        call_count["value"] += 1
        raise TranslationProviderUnavailableError("provider unavailable")

    provider.reset_retry_report()
    provider._post_chat_completion = always_unavailable  # type: ignore[method-assign]

    with pytest.raises(TranslationProviderUnavailableError, match="provider unavailable"):
        provider.translate_batch(source_lines)

    retry_report = provider.get_retry_report()
    assert call_count["value"] == 3
    assert retry_report["retry_attempted"] is True
    assert retry_report["retry_count"] == 2
    assert retry_report["retry_candidate"] is True
    assert retry_report["final_error_type"] == "provider_unavailable"
    assert retry_report["final_error_message"] == "provider unavailable"


def test_real_tts_provider_requires_explicit_configuration() -> None:
    with pytest.raises(TTSConfigurationError, match="model_name is required"):
        OpenAICompatibleTTSProvider(
            output_dir="D:/tmp/tts",
            config=RealTTSProviderConfig(
                enabled=True,
                provider_name="openai_compatible_tts",
                model_name=None,
                base_url="https://example.test/v1",
                api_key="secret",
            ),
        )


def test_real_tts_provider_can_explicitly_fallback_to_mock(tmp_path: Path) -> None:
    binding = resolve_tts_provider(
        TTSProviderSelectionConfig(
            mode="real",
            real=RealTTSProviderConfig(
                enabled=True,
                provider_name="openai_compatible_tts",
                model_name=None,
                base_url="https://example.test/v1",
                api_key="secret",
                fallback_to_mock=True,
            ),
        ),
        mock_provider=MockTTSService(output_dir=str(tmp_path / "audio")),
        output_dir=str(tmp_path / "audio"),
    )

    assert binding.provider_name == "mock_tts"
    assert binding.mode == "mock_fallback"
    assert binding.fallback_applied is True


def test_tts_provider_selection_switches_between_mock_and_real(tmp_path: Path) -> None:
    mock_tts = MockTTSService(output_dir=str(tmp_path / "audio"))
    mock_binding = resolve_tts_provider(
        TTSProviderSelectionConfig(mode="mock"),
        mock_provider=mock_tts,
        output_dir=str(tmp_path / "audio"),
    )
    real_binding = resolve_tts_provider(
        TTSProviderSelectionConfig(
            mode="real",
            real=RealTTSProviderConfig(
                enabled=True,
                provider_name="openai_compatible_tts",
                model_name="tts-model",
                base_url="https://example.test/v1",
                api_key="secret",
                voice_name="alloy",
            ),
        ),
        mock_provider=mock_tts,
        output_dir=str(tmp_path / "audio"),
    )

    assert mock_binding.mode == "mock"
    assert real_binding.mode == "real"
    assert real_binding.provider_name == "openai_compatible_tts"
    assert real_binding.voice_name == "alloy"


def test_real_tts_provider_selection_reads_minimax_voice_id_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        tts_provider_module.config_loader,
        "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH",
        tmp_path / "missing_autodub.local.json",
    )
    monkeypatch.setenv("AUTODUB_TTS_MODE", "real")
    monkeypatch.setenv("AUTODUB_TTS_ENABLED", "true")
    monkeypatch.setenv("AUTODUB_TTS_PROVIDER_NAME", "minimax_tts")
    monkeypatch.setenv("AUTODUB_TTS_MODEL_NAME", "speech-02-turbo")
    monkeypatch.setenv("AUTODUB_TTS_BASE_URL", "https://api.minimaxi.com/v1")
    monkeypatch.setenv("AUTODUB_TTS_API_KEY", "secret")
    monkeypatch.setenv("AUTODUB_TTS_API_PROTOCOL", "minimax_t2a_v2")
    monkeypatch.setenv("AUTODUB_TTS_VOICE_ID", "vt_speaker_a_minimaxiauto_1773282135988")

    selection = TTSProviderSelectionConfig.from_env()
    binding = resolve_tts_provider(
        selection,
        mock_provider=MockTTSService(output_dir=str(tmp_path / "audio")),
        output_dir=str(tmp_path / "audio"),
    )

    assert selection.real.voice_id == "vt_speaker_a_minimaxiauto_1773282135988"
    assert binding.mode == "real"
    assert binding.provider_name == "minimax_tts"
    assert binding.voice_name == "vt_speaker_a_minimaxiauto_1773282135988"
    assert binding.version_context["api_protocol"] == "minimax_t2a_v2"
    assert binding.version_context["voice_id"] == "vt_speaker_a_minimaxiauto_1773282135988"
    assert binding.version_context["provider_variant"] == "minimax_tts_v1"
    assert binding.version_context["voice_resolution_strategy"] == "env_voice_id_only"


def test_real_tts_provider_config_reads_autodub_local_json(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "tts": {
                    "enabled": True,
                    "mode": "real",
                    "provider_name": "minimax_tts",
                    "model_name": "speech-2.8-turbo",
                    "base_url": "https://api.minimaxi.com",
                    "api_key": "config-secret",
                    "api_protocol": "minimax_t2a_v2",
                    "voice_registry_path": "voice_registry.json",
                },
                "voice_registry": {
                    "registry_path": "voice_registry.json",
                },
            }
        ),
        encoding="utf-8",
    )

    selection = TTSProviderSelectionConfig.from_env(config_path=config_path)

    assert selection.mode == "real"
    assert selection.real.enabled is True
    assert selection.real.provider_name == "minimax_tts"
    assert selection.real.model_name == "speech-2.8-turbo"
    assert selection.real.base_url == "https://api.minimaxi.com"
    assert selection.real.resolved_api_key() == "config-secret"
    assert selection.real.resolved_voice_registry_path() == str(tmp_path / "voice_registry.json")
    assert selection.real.build_diagnostic_summary()["config_source"] == "autodub.local.json"
    assert selection.real.build_diagnostic_summary()["api_key_source_type"] == "legacy_file"


def test_real_tts_provider_env_overrides_autodub_local_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "tts": {
                    "enabled": True,
                    "mode": "mock",
                    "provider_name": "minimax_tts",
                    "model_name": "config-model",
                    "base_url": "https://config.example/v1",
                    "api_key": "config-secret",
                    "api_protocol": "minimax_t2a_v2",
                    "voice_id": "config_voice_id",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTODUB_TTS_MODE", "real")
    monkeypatch.setenv("AUTODUB_TTS_MODEL_NAME", "env-model")
    monkeypatch.setenv("AUTODUB_TTS_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("AUTODUB_TTS_API_KEY", "env-secret")
    monkeypatch.setenv("AUTODUB_TTS_VOICE_ID", "env_voice_id")
    monkeypatch.setenv("AUTODUB_TTS_API_PROTOCOL", "minimax_t2a_v2")

    selection = TTSProviderSelectionConfig.from_env(config_path=config_path)

    assert selection.mode == "real"
    assert selection.real.model_name == "env-model"
    assert selection.real.base_url == "https://env.example/v1"
    assert selection.real.resolved_api_key() == "env-secret"
    assert selection.real.resolved_voice_id() == "env_voice_id"
    assert selection.real.build_diagnostic_summary()["config_source"] == "env"
    assert selection.real.build_diagnostic_summary()["api_key_source_type"] == "direct_env"


def test_real_tts_provider_reports_api_key_env_var_source_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "tts": {
                    "enabled": True,
                    "mode": "real",
                    "provider_name": "minimax_tts",
                    "model_name": "speech-2.8-turbo",
                    "base_url": "https://api.minimaxi.com",
                    "api_key_env_var": "AUTODUB_TTS_API_KEY_ALT",
                    "api_protocol": "minimax_t2a_v2",
                    "voice_id": "config_voice_id",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTODUB_TTS_API_KEY_ALT", "env-secret")

    selection = TTSProviderSelectionConfig.from_env(config_path=config_path)

    assert selection.real.resolved_api_key() == "env-secret"
    assert selection.real.build_diagnostic_summary()["api_key_source_type"] == "api_key_env_var"


def test_real_tts_provider_reports_persisted_env_source_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "tts": {
                    "enabled": True,
                    "mode": "real",
                    "provider_name": "minimax_tts",
                    "model_name": "speech-2.8-turbo",
                    "base_url": "https://api.minimaxi.com",
                    "api_protocol": "minimax_t2a_v2",
                    "voice_id": "config_voice_id",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tts_provider_module.config_loader,
        "iter_env_readers",
        _build_env_readers(persisted={"AUTODUB_TTS_API_KEY": "persisted-secret"}),
    )

    selection = TTSProviderSelectionConfig.from_env(config_path=config_path)

    assert selection.real.resolved_api_key() == "persisted-secret"
    assert selection.real.build_diagnostic_summary()["api_key_source_type"] == "persisted_env"


def test_real_tts_provider_reports_missing_api_key_source_type(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "tts": {
                    "enabled": True,
                    "mode": "real",
                    "provider_name": "minimax_tts",
                    "model_name": "speech-2.8-turbo",
                    "base_url": "https://api.minimaxi.com",
                    "api_protocol": "minimax_t2a_v2",
                    "voice_id": "config_voice_id",
                }
            }
        ),
        encoding="utf-8",
    )

    selection = TTSProviderSelectionConfig.from_env(config_path=config_path)

    assert selection.real.resolved_api_key() is None
    assert selection.real.build_diagnostic_summary()["api_key_source_type"] == "missing"


def test_voice_clone_config_reports_direct_env_source_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTODUB_TTS_CLONE_BASE_URL", "https://clone.example/v1")
    monkeypatch.setenv("AUTODUB_TTS_CLONE_API_KEY", "clone-secret")

    config = VoiceCloneConfig.from_env()

    assert config.resolved_api_key() == "clone-secret"
    assert config.build_diagnostic_summary()["api_key_source_type"] == "direct_env"


def test_voice_clone_config_reports_api_key_env_var_source_type(
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
                    "api_key_env_var": "AUTODUB_TTS_CLONE_API_KEY_ALT",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTODUB_TTS_CLONE_API_KEY_ALT", "clone-secret")

    config = VoiceCloneConfig.from_env(config_path=config_path)

    assert config.resolved_api_key() == "clone-secret"
    assert config.build_diagnostic_summary()["api_key_source_type"] == "api_key_env_var"


def test_voice_clone_config_reports_persisted_env_source_type(
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
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        voice_clone_module,
        "_iter_env_readers",
        _build_env_readers(persisted={"AUTODUB_TTS_API_KEY": "persisted-secret"}),
    )

    config = VoiceCloneConfig.from_env(config_path=config_path)

    assert config.resolved_api_key() == "persisted-secret"
    assert config.build_diagnostic_summary()["api_key_source_type"] == "persisted_env"


def test_voice_clone_config_reports_legacy_file_source_type(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "voice_clone": {
                    "enabled": True,
                    "base_url": "https://clone.example/v1",
                    "api_key": "clone-secret",
                }
            }
        ),
        encoding="utf-8",
    )

    config = VoiceCloneConfig.from_env(config_path=config_path)

    assert config.resolved_api_key() == "clone-secret"
    assert config.build_diagnostic_summary()["api_key_source_type"] == "legacy_file"


def test_voice_clone_config_reads_timeout_and_retry_settings_from_file(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "voice_clone": {
                    "enabled": True,
                    "base_url": "https://clone.example/v1",
                    "api_key": "clone-secret",
                    "timeout_seconds": 240.0,
                    "max_retries": 5,
                    "retry_backoff_seconds": 1.25,
                }
            }
        ),
        encoding="utf-8",
    )

    config = VoiceCloneConfig.from_env(config_path=config_path)

    assert config.timeout_seconds == 240.0
    assert config.max_retries == 5
    assert config.retry_backoff_seconds == 1.25


def test_voice_clone_config_reports_missing_api_key_source_type(tmp_path: Path) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "voice_clone": {
                    "enabled": True,
                    "base_url": "https://clone.example/v1",
                }
            }
        ),
        encoding="utf-8",
    )

    config = VoiceCloneConfig.from_env(config_path=config_path)

    assert config.resolved_api_key() is None
    assert config.build_diagnostic_summary()["api_key_source_type"] == "missing"


def test_real_tts_provider_builds_minimax_voice_id_request_payload() -> None:
    provider = OpenAICompatibleTTSProvider(
        output_dir="D:/tmp/tts",
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="minimax_tts",
            model_name="speech-02-turbo",
            base_url="https://api.minimaxi.com/v1",
            api_key="secret",
            voice_id="vt_speaker_a_minimaxiauto_1773282135988",
            api_protocol="minimax_t2a_v2",
        ),
    )
    block = SemanticBlock(
        block_id="block_minimax_voice_id",
        speaker_id="speaker_host",
        speaker_name="Host",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="测试一下",
    )

    resolved_voice = provider.resolve_block_voice(block)
    payload = provider._build_request_payload(block, resolved_voice)

    assert payload["model"] == "speech-02-turbo"
    assert payload["text"] == "测试一下"
    assert payload["stream"] is False
    assert payload["output_format"] == "hex"
    assert payload["audio_setting"] == {"format": "wav", "channel": 1}
    assert payload["voice_setting"] == {
        "voice_id": "vt_speaker_a_minimaxiauto_1773282135988"
    }
    assert "voice" not in payload


def test_real_tts_provider_builds_minimax_v1_endpoint() -> None:
    provider = OpenAICompatibleTTSProvider(
        output_dir="D:/tmp/tts",
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="minimax_tts",
            model_name="speech-02-turbo",
            base_url="https://api.minimaxi.com",
            api_key="secret",
            voice_id="vt_speaker_a_minimaxiauto_1773282135988",
            api_protocol="minimax_t2a_v2",
        ),
    )

    assert provider._build_endpoint() == "https://api.minimaxi.com/v1/t2a_v2"


def test_real_tts_provider_prefers_speaker_default_cloned_voice_from_registry(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    registry.register_voice(
        "speaker_host",
        speaker_name="Host",
        voice_id="clone_host_001",
        voice_type="cloned",
        provider="minimax_tts",
        label="Host Clone",
        created_at="2026-03-13T09:00:00+00:00",
        set_default=True,
    )
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="minimax_tts",
            model_name="speech-02-turbo",
            base_url="https://api.minimaxi.com/v1",
            api_key="secret",
            voice_registry_path=str(tmp_path / "voice_registry.json"),
            api_protocol="minimax_t2a_v2",
        ),
    )
    block = SemanticBlock(
        block_id="block_registry_cloned",
        speaker_id="speaker_host",
        speaker_name="Host",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="Registry cloned voice",
    )

    resolved_voice = provider.resolve_block_voice(block)
    runtime_context = build_tts_block_runtime_context(
        provider,
        block,
        default_voice_name="alloy",
        default_version_context=provider.get_cache_context(),
    )

    assert resolved_voice.resolved is True
    assert resolved_voice.voice_id == "clone_host_001"
    assert resolved_voice.source == "speaker_default_cloned"
    assert runtime_context["voice_name"] == "clone_host_001"
    assert runtime_context["resolved_voice_id"] == "clone_host_001"
    assert runtime_context["voice_resolution_source"] == "speaker_default_cloned"
    assert runtime_context["version_context"]["voice_id"] == "clone_host_001"
    assert runtime_context["version_context"]["voice_resolution_strategy"] == "speaker_registry_then_env_fallback"


def test_real_tts_provider_falls_back_to_speaker_default_builtin_voice_from_registry(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    registry.register_voice(
        "speaker_guest",
        speaker_name="Guest",
        voice_id="builtin_guest_001",
        voice_type="builtin",
        provider="minimax_tts",
        label="Guest Builtin",
        created_at="2026-03-13T09:05:00+00:00",
        set_default=True,
    )
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="minimax_tts",
            model_name="speech-02-turbo",
            base_url="https://api.minimaxi.com/v1",
            api_key="secret",
            voice_registry_path=str(tmp_path / "voice_registry.json"),
            api_protocol="minimax_t2a_v2",
        ),
    )
    block = SemanticBlock(
        block_id="block_registry_builtin",
        speaker_id="speaker_guest",
        speaker_name="Guest",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="Registry builtin voice",
    )

    resolved_voice = provider.resolve_block_voice(block)

    assert resolved_voice.resolved is True
    assert resolved_voice.voice_id == "builtin_guest_001"
    assert resolved_voice.source == "speaker_default_builtin"


def test_real_tts_provider_falls_back_to_project_default_builtin_voice_from_registry(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    registry.set_project_default_builtin_voice(
        voice_id="project_builtin_001",
        provider="minimax_tts",
        label="Project Builtin",
        created_at="2026-03-13T09:10:00+00:00",
    )
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="minimax_tts",
            model_name="speech-02-turbo",
            base_url="https://api.minimaxi.com/v1",
            api_key="secret",
            voice_registry_path=str(tmp_path / "voice_registry.json"),
            api_protocol="minimax_t2a_v2",
        ),
    )
    block = SemanticBlock(
        block_id="block_registry_project_default",
        speaker_id="speaker_unknown",
        speaker_name=None,
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="Project default builtin",
    )

    resolved_voice = provider.resolve_block_voice(block)

    assert resolved_voice.resolved is True
    assert resolved_voice.voice_id == "project_builtin_001"
    assert resolved_voice.source == "project_default_builtin"


def test_real_tts_provider_falls_back_to_env_voice_id_when_registry_has_no_match(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    registry.save(registry.load())
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="minimax_tts",
            model_name="speech-02-turbo",
            base_url="https://api.minimaxi.com/v1",
            api_key="secret",
            voice_id="vt_global_env_001",
            voice_registry_path=str(tmp_path / "voice_registry.json"),
            api_protocol="minimax_t2a_v2",
        ),
    )
    block = SemanticBlock(
        block_id="block_registry_env_fallback",
        speaker_id="speaker_unknown",
        speaker_name=None,
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="Env fallback voice",
    )

    resolved_voice = provider.resolve_block_voice(block)

    assert resolved_voice.resolved is True
    assert resolved_voice.voice_id == "vt_global_env_001"
    assert resolved_voice.source == "env_fallback"


def test_real_tts_provider_fails_cleanly_when_voice_resolution_is_unresolved(tmp_path: Path) -> None:
    registry = VoiceRegistry(str(tmp_path / "voice_registry.json"))
    registry.save(registry.load())
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="minimax_tts",
            model_name="speech-02-turbo",
            base_url="https://api.minimaxi.com/v1",
            api_key="secret",
            voice_registry_path=str(tmp_path / "voice_registry.json"),
            api_protocol="minimax_t2a_v2",
        ),
    )
    block = SemanticBlock(
        block_id="block_registry_unresolved",
        speaker_id="speaker_unknown",
        speaker_name=None,
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="Unresolved voice",
    )

    resolved_voice = provider.resolve_block_voice(block)

    assert resolved_voice.resolved is False
    assert resolved_voice.source == "unresolved"
    with pytest.raises(TTSConfigurationError, match="No TTS voice could be resolved"):
        provider.synthesize(block)


def test_real_tts_provider_extracts_base64_wav_payload() -> None:
    provider = OpenAICompatibleTTSProvider(
        output_dir="D:/tmp/tts",
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="openai_compatible_tts",
            model_name="tts-model",
            base_url="https://example.test/v1",
            api_key="secret",
        ),
    )
    wav_bytes = b"RIFFdemo"
    payload = json_payload = (
        '{"audio_base64":"' + base64.b64encode(wav_bytes).decode("ascii") + '"}'
    ).encode("utf-8")

    extracted = provider._extract_wav_bytes(payload, "application/json")
    classified = classify_tts_error(TTSConfigurationError("missing config"))

    assert extracted == wav_bytes
    assert classified == {"error_type": "configuration_error", "retry_candidate": False}


def test_real_tts_provider_extracts_nested_minimax_hex_audio_payload() -> None:
    provider = OpenAICompatibleTTSProvider(
        output_dir="D:/tmp/tts",
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="minimax_tts",
            model_name="speech-02-turbo",
            base_url="https://api.minimaxi.com/v1",
            api_key="secret",
            voice_id="vt_speaker_a_minimaxiauto_1773282135988",
            api_protocol="minimax_t2a_v2",
        ),
    )
    wav_bytes = b"RIFFdemo"
    payload = (
        '{"data":{"audio":"' + wav_bytes.hex() + '"}}'
    ).encode("utf-8")

    extracted = provider._extract_wav_bytes(payload, "application/json")

    assert extracted == wav_bytes


def test_real_tts_provider_classifies_runtime_errors() -> None:
    unavailable = classify_tts_error(TTSProviderUnavailableError("provider down"))
    timeout = classify_tts_error(TTSProviderTimeoutError("timed out"))
    network_error = classify_tts_error(TTSProviderNetworkError("connection reset"))
    bad_format = classify_tts_error(TTSProviderResponseFormatError("bad envelope"))
    invalid_audio = classify_tts_error(TTSInvalidAudioPayloadError("bad audio"))
    write_failure = classify_tts_error(TTSOutputFileWriteError("disk full"))

    assert unavailable == {"error_type": "provider_unavailable", "retry_candidate": False}
    assert timeout == {"error_type": "provider_timeout", "retry_candidate": True}
    assert network_error == {"error_type": "provider_network_error", "retry_candidate": True}
    assert bad_format == {"error_type": "invalid_provider_response_format", "retry_candidate": False}
    assert invalid_audio == {"error_type": "invalid_audio_payload", "retry_candidate": False}
    assert write_failure == {"error_type": "output_file_write_failure", "retry_candidate": False}


def test_real_tts_provider_rejects_invalid_audio_payload() -> None:
    provider = OpenAICompatibleTTSProvider(
        output_dir="D:/tmp/tts",
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="openai_compatible_tts",
            model_name="tts-model",
            base_url="https://example.test/v1",
            api_key="secret",
        ),
    )

    with pytest.raises(TTSInvalidAudioPayloadError, match="does not contain audio payload"):
        provider._extract_wav_bytes(b'{"unexpected":"value"}', "application/json")


def test_real_tts_provider_distinguishes_timeout_and_network_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="openai_compatible_tts",
            model_name="tts-model",
            base_url="https://example.test/v1",
            api_key="secret",
        ),
    )

    def raise_timeout(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(tts_provider_module.request, "urlopen", raise_timeout)
    with pytest.raises(TTSProviderTimeoutError, match="timeout"):
        provider._post_tts_request({"input": "hello"})

    def raise_network_error(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise error.URLError("connection refused")

    monkeypatch.setattr(tts_provider_module.request, "urlopen", raise_network_error)
    with pytest.raises(TTSProviderNetworkError, match="network-like failure"):
        provider._post_tts_request({"input": "hello"})


def test_real_tts_provider_uses_partial_body_when_response_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="minimax_tts",
            model_name="speech-02-turbo",
            base_url="https://api.minimaxi.com",
            api_key="secret",
            voice_id="vt_speaker_a_minimaxiauto_1773282135988",
            api_protocol="minimax_t2a_v2",
        ),
    )
    partial_body = b'{"data":{"audio":"52494646"}}'

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def read(self) -> bytes:
            raise IncompleteRead(partial_body, 20)

    monkeypatch.setattr(tts_provider_module.request, "urlopen", lambda *args, **kwargs: FakeResponse())

    response_body, content_type = provider._post_tts_request({"text": "hello"})

    assert response_body == partial_body
    assert content_type == "application/json"


def test_real_tts_provider_surfaces_output_file_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="openai_compatible_tts",
            model_name="tts-model",
            base_url="https://example.test/v1",
            api_key="secret",
        ),
    )
    block = SemanticBlock(
        block_id="block_tts_write_failure",
        speaker_id="speaker_host",
        speaker_name="Host",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="测试",
    )

    monkeypatch.setattr(provider, "_post_tts_request", lambda payload: (b"RIFFdemo", "audio/wav"))
    monkeypatch.setattr(Path, "write_bytes", lambda self, data: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(TTSOutputFileWriteError, match="Failed to write TTS audio output"):
        provider.synthesize(block)


def test_real_tts_provider_retries_retry_candidate_errors_before_success(
    tmp_path: Path,
) -> None:
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="openai_compatible_tts",
            model_name="tts-model",
            base_url="https://example.test/v1",
            api_key="secret",
            max_retries=2,
            retry_backoff_seconds=0.0,
        ),
    )
    block = SemanticBlock(
        block_id="block_tts_retry_success",
        speaker_id="speaker_host",
        speaker_name="Host",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="测试",
    )
    call_count = {"value": 0}

    def flaky_post(payload: dict[str, object]) -> tuple[bytes, str]:
        del payload
        call_count["value"] += 1
        if call_count["value"] < 3:
            raise TTSProviderTimeoutError("provider timed out")
        return (b"RIFFdemo", "audio/wav")

    provider.reset_retry_report()
    provider._post_tts_request = flaky_post  # type: ignore[method-assign]

    synthesized_path = provider.synthesize(block)
    retry_report = provider.get_retry_report()

    assert Path(synthesized_path).exists()
    assert call_count["value"] == 3
    assert retry_report["retry_attempted"] is True
    assert retry_report["retry_count"] == 2
    assert retry_report["retry_candidate"] is True
    assert retry_report["final_error_type"] is None
    assert retry_report["final_error_message"] is None


def test_real_tts_provider_does_not_retry_non_retry_candidate_errors(
    tmp_path: Path,
) -> None:
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="openai_compatible_tts",
            model_name="tts-model",
            base_url="https://example.test/v1",
            api_key="secret",
            max_retries=2,
            retry_backoff_seconds=0.0,
        ),
    )
    block = SemanticBlock(
        block_id="block_tts_no_retry",
        speaker_id="speaker_host",
        speaker_name="Host",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="测试",
    )
    call_count = {"value": 0}

    def invalid_audio(payload: dict[str, object]) -> tuple[bytes, str]:
        del payload
        call_count["value"] += 1
        return (b"not_wav", "audio/wav")

    provider.reset_retry_report()
    provider._post_tts_request = invalid_audio  # type: ignore[method-assign]

    with pytest.raises(TTSInvalidAudioPayloadError, match="not a wav payload"):
        provider.synthesize(block)

    retry_report = provider.get_retry_report()
    assert call_count["value"] == 1
    assert retry_report["retry_attempted"] is False
    assert retry_report["retry_count"] == 0
    assert retry_report["retry_candidate"] is False
    assert retry_report["final_error_type"] == "invalid_audio_payload"
    assert retry_report["final_error_message"] == "TTS provider output is not a wav payload."


def test_real_tts_provider_surfaces_retry_exhaustion(
    tmp_path: Path,
) -> None:
    provider = OpenAICompatibleTTSProvider(
        output_dir=str(tmp_path / "audio"),
        config=RealTTSProviderConfig(
            enabled=True,
            provider_name="openai_compatible_tts",
            model_name="tts-model",
            base_url="https://example.test/v1",
            api_key="secret",
            max_retries=2,
            retry_backoff_seconds=0.0,
        ),
    )
    block = SemanticBlock(
        block_id="block_tts_retry_exhausted",
        speaker_id="speaker_host",
        speaker_name="Host",
        original_srt_indices=[1],
        first_start_ms=0,
        last_end_ms=1_000,
        target_duration_ms=1_000,
        merged_cn_text="测试",
    )
    call_count = {"value": 0}

    def always_timeout(payload: dict[str, object]) -> tuple[bytes, str]:
        del payload
        call_count["value"] += 1
        raise TTSProviderTimeoutError("provider timed out")

    provider.reset_retry_report()
    provider._post_tts_request = always_timeout  # type: ignore[method-assign]

    with pytest.raises(TTSProviderTimeoutError, match="provider timed out"):
        provider.synthesize(block)

    retry_report = provider.get_retry_report()
    assert call_count["value"] == 3
    assert retry_report["retry_attempted"] is True
    assert retry_report["retry_count"] == 2
    assert retry_report["retry_candidate"] is True
    assert retry_report["final_error_type"] == "provider_timeout"
    assert retry_report["final_error_message"] == "provider timed out"
