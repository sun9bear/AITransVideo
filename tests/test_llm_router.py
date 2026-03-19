from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.llm.base import LLMCallConfig, LLMProviderError
from services.llm.router import DEFAULT_LLM_FALLBACKS, LLMRouter, load_llm_fallback_config
import services.llm.router as router_module


class _FakeProvider:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict[str, object]] = []
        self.config = LLMCallConfig(
            provider="fake",
            model_name="fake-model",
            api_key="test-key",
        )

    def generate_text(
        self,
        *,
        prompt: str,
        model_name: str,
        temperature: float,
        max_output_tokens: int,
        json_mode: bool,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "model_name": model_name,
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
                "json_mode": json_mode,
            }
        )
        return self.text


def test_load_llm_fallback_config_uses_defaults_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(router_module, "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH", config_path)

    config = load_llm_fallback_config()

    assert config["llm_fallbacks"] == DEFAULT_LLM_FALLBACKS
    assert "deepseek_chat" in config["llm_models"]
    assert "gemini_3_1_flash_lite_preview" in config["llm_models"]
    assert "gpt_41" in config["llm_models"]
    assert "gpt_41_mini" in config["llm_models"]
    assert "gpt_54" in config["llm_models"]
    assert "claude_sonnet_46" in config["llm_models"]


def test_llm_router_uses_injected_provider_for_alias() -> None:
    provider = _FakeProvider("hello")
    router = LLMRouter(
        {
            "llm_fallbacks": {"s3_translate": ["gpt_41"]},
            "llm_models": {},
        },
        providers={"gpt_41": provider},
    )

    response = router.generate_via_alias("gpt_41", prompt="ping", json_mode=True)

    assert response == "hello"
    assert provider.calls[0]["prompt"] == "ping"
    assert provider.calls[0]["json_mode"] is True


def test_llm_router_syncs_s2_and_s5_routes_to_s3_route() -> None:
    router = LLMRouter(
        {
            "llm_fallbacks": {
                "s2_infer": ["default_llm", "gpt_41_mini"],
                "s2_review": ["default_llm", "gpt_41"],
                "s3_translate": [
                    "deepseek_chat",
                    "gemini_3_1_flash_lite_preview",
                    "default_llm",
                    "gpt_41",
                ],
                "s5_rewrite": [
                    "gemini_3_1_flash_lite_preview",
                    "default_llm",
                    "claude_sonnet_46",
                ]
            },
            "llm_models": {},
            "gemini": {"model_name": "gemini-2.5-pro"},
        },
        providers={},
    )

    assert router.get_route("s2_infer") == [
        "deepseek_chat",
        "gemini_3_1_flash_lite_preview",
        "default_llm",
        "gpt_41",
    ]
    assert router.get_route("s2_review") == router.get_route("s3_translate")
    assert router.get_route("s5_rewrite") == router.get_route("s3_translate")


def test_llm_router_returns_default_route_for_unknown_task() -> None:
    router = LLMRouter({"llm_fallbacks": {}, "llm_models": {}}, providers={})

    assert router.get_route("s3_translate") == DEFAULT_LLM_FALLBACKS["s3_translate"]


def test_llm_router_missing_provider_alias_raises_clear_error() -> None:
    router = LLMRouter({"llm_fallbacks": {}, "llm_models": {}}, providers={})

    with pytest.raises(LLMProviderError, match="No LLM provider is configured"):
        router.generate_via_alias("gpt_41", prompt="ping", json_mode=False)


def test_llm_router_openai_alias_without_key_raises_clear_error() -> None:
    router = LLMRouter(
        {
            "openai": {
                "api_key": None,
                "api_key_env_var": "OPENAI_API_KEY",
                "base_url": "https://api.openai.com/v1",
                "model_name": "gpt-4.1",
                "temperature": 0.3,
                "max_output_tokens": 8192,
                "timeout_seconds": 120.0,
            },
            "anthropic": {},
            "llm_models": {
                "gpt_41": {"provider": "openai", "model_name": "gpt-4.1"},
            },
            "llm_fallbacks": {"s3_translate": ["gpt_41"]},
        }
    )

    with pytest.raises(LLMProviderError, match="OpenAI API key is required"):
        router.generate_via_alias("gpt_41", prompt="ping", json_mode=False)


def test_load_llm_fallback_config_respects_user_route_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "autodub.local.json"
    config_path.write_text(
        json.dumps(
            {
                "llm_fallbacks": {
                    "s3_translate": ["gemini_3_1_flash_lite_preview", "default_llm", "gpt_41"],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(router_module, "DEFAULT_AUTODUB_LOCAL_CONFIG_PATH", config_path)

    config = load_llm_fallback_config()

    assert config["llm_fallbacks"]["s3_translate"] == [
        "gemini_3_1_flash_lite_preview",
        "default_llm",
        "gpt_41",
    ]
    assert config["llm_fallbacks"]["s2_infer"] == config["llm_fallbacks"]["s3_translate"]
    assert config["llm_fallbacks"]["s2_review"] == config["llm_fallbacks"]["s3_translate"]
    assert config["llm_fallbacks"]["s5_rewrite"] == config["llm_fallbacks"]["s3_translate"]


def test_llm_router_deepseek_alias_without_key_raises_clear_error() -> None:
    router = LLMRouter(
        {
            "deepseek": {
                "api_key": None,
                "api_key_env_var": "DEEPSEEK_API_KEY",
                "base_url": "https://api.deepseek.com/v1",
                "model_name": "deepseek-chat",
                "temperature": 0.3,
                "max_output_tokens": 8192,
                "timeout_seconds": 120.0,
            },
            "openai": {},
            "anthropic": {},
            "llm_models": {
                "deepseek_chat": {"provider": "deepseek", "model_name": "deepseek-chat"},
            },
            "llm_fallbacks": {"s3_translate": ["deepseek_chat", "gpt_41"]},
        }
    )

    with pytest.raises(LLMProviderError, match="DeepSeek API key is required"):
        router.generate_via_alias("deepseek_chat", prompt="ping", json_mode=False)


def test_llm_router_maps_legacy_gemini_current_alias_to_default_llm() -> None:
    router = LLMRouter(
        {
            "gemini": {
                "model_name": "gemini-2.5-pro",
                "temperature": 0.3,
                "max_output_tokens": 8192,
            },
            "llm_fallbacks": {
                "s3_translate": ["gemini_current", "gpt_41"],
            },
            "llm_models": {},
        },
        providers={},
    )

    assert router.get_route("s3_translate") == ["default_llm", "gpt_41"]
    assert router.get_model_config("default_llm")["model_name"] == "gemini-2.5-pro"
