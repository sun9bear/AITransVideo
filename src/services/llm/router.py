"""LLM Router — DEPRECATED (2026-04-17).

本模块的**路由决策层**（`LLMRouter.get_route` / `generate_via_alias` /
`DEFAULT_LLM_MODELS` / `DEFAULT_SHARED_TEXT_ROUTE`）已被
`src/services/llm_registry.py` 取代。见
`docs/plans/2026-04-09-prompt-model-management-plan.md` §5.4。

**观察期**：2026-04-17 ~ 2026-05-01。生产日志 grep 关键字 `[LLM-ROUTER-LEGACY]`：

- 若观察期内**零命中**：本模块将在 2026-05-01 后整体归档/删除。
- 若有命中：记录 task 名和上下文，补 `prompt_key_map`（translator.py:998），
  修完后重启观察期。

**观察期计划文档**：`docs/plans/2026-04-17-llmrouter-deprecation.md`

**模块剩余真实用户**（观察期内需保留）：
1. `translator.py` 的 `_call_task_with_fallback` legacy path（触发 `[LLM-ROUTER-LEGACY]`）
2. `process.py:578 / 807 / 2158-2160`（构造 + 注入 segmenter + 读 `model_configs`）
3. `web_ui/config_helpers.py`（只引用 `DEFAULT_AUTODUB_LOCAL_CONFIG_PATH`，
   归档本模块前需把 web_ui 的 import 源改为直接从 `services.config_loader` 引）
4. `tests/test_llm_router.py`（12+ 个专项测试，模块下线时整组删除）
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from services.config_loader import DEFAULT_AUTODUB_LOCAL_CONFIG_PATH
from services.llm.base import LLMCallConfig, LLMProvider, LLMProviderError
from services.llm.providers.anthropic_provider import AnthropicProvider
from services.llm.providers.deepseek_provider import DeepSeekProvider
from services.llm.providers.openai_provider import OpenAIProvider


DEFAULT_OPENAI_MODEL_NAME = "gpt-4.1"
DEFAULT_ANTHROPIC_MODEL_NAME = "claude-sonnet-4-6"
DEFAULT_DEEPSEEK_MODEL_NAME = "deepseek-chat"
DEFAULT_GPT_41_MINI_MODEL_NAME = "gpt-4.1-mini"
DEFAULT_GPT_54_MODEL_NAME = "gpt-5.4"
DEFAULT_GEMINI_25_FLASH_MODEL_NAME = "gemini-2.5-flash"
DEFAULT_GEMINI_3_1_FLASH_LITE_PREVIEW_MODEL_NAME = "gemini-3.1-flash-lite-preview"
DEFAULT_DEFAULT_LLM_ALIAS = "default_llm"
LEGACY_GEMINI_CURRENT_ALIAS = "gemini_current"
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_SHARED_TEXT_ROUTE = [
    "deepseek_chat",
    "gemini_3_1_flash_lite_preview",
    DEFAULT_DEFAULT_LLM_ALIAS,
    "gpt_41",
]
S3_SYNCED_TASKS = ("s2_infer", "s2_review", "s5_rewrite")
DEFAULT_LLM_MODELS: dict[str, dict[str, object]] = {
    "deepseek_chat": {
        "provider": "deepseek",
        "model_name": DEFAULT_DEEPSEEK_MODEL_NAME,
        "temperature": DEFAULT_TEMPERATURE,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    },
    "gemini_3_1_flash_lite_preview": {
        "provider": "gemini",
        "model_name": DEFAULT_GEMINI_3_1_FLASH_LITE_PREVIEW_MODEL_NAME,
        "temperature": DEFAULT_TEMPERATURE,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    },
    "gemini_25_flash": {
        "provider": "gemini",
        "model_name": DEFAULT_GEMINI_25_FLASH_MODEL_NAME,
        "temperature": DEFAULT_TEMPERATURE,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    },
    "gpt_41_mini": {
        "provider": "openai",
        "model_name": DEFAULT_GPT_41_MINI_MODEL_NAME,
        "temperature": DEFAULT_TEMPERATURE,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    },
    "gpt_41": {
        "provider": "openai",
        "model_name": DEFAULT_OPENAI_MODEL_NAME,
        "temperature": DEFAULT_TEMPERATURE,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    },
    "gpt_54": {
        "provider": "openai",
        "model_name": DEFAULT_GPT_54_MODEL_NAME,
        "temperature": DEFAULT_TEMPERATURE,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    },
    "claude_sonnet_46": {
        "provider": "anthropic",
        "model_name": DEFAULT_ANTHROPIC_MODEL_NAME,
        "temperature": DEFAULT_TEMPERATURE,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
    },
}
DEFAULT_LLM_FALLBACKS: dict[str, list[str]] = {
    "s2_infer": list(DEFAULT_SHARED_TEXT_ROUTE),
    "s2_review": list(DEFAULT_SHARED_TEXT_ROUTE),
    "s3_translate": list(DEFAULT_SHARED_TEXT_ROUTE),
    "s5_rewrite": list(DEFAULT_SHARED_TEXT_ROUTE),
}


def load_llm_fallback_config() -> dict[str, object]:
    config_path = DEFAULT_AUTODUB_LOCAL_CONFIG_PATH.resolve(strict=False)
    payload: dict[str, object] = {}
    if config_path.exists():
        try:
            loaded_payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMProviderError(f"Failed to load LLM fallback config from {config_path}") from exc
        if not isinstance(loaded_payload, dict):
            raise LLMProviderError("LLM fallback config file must contain a top-level JSON object.")
        payload = loaded_payload

    openai_section = _ensure_dict(payload.get("openai"))
    anthropic_section = _ensure_dict(payload.get("anthropic"))
    deepseek_section = _ensure_dict(payload.get("deepseek"))
    gemini_section = _ensure_dict(payload.get("gemini"))
    llm_models_section = _ensure_dict(payload.get("llm_models"))
    fallbacks_section = _ensure_dict(payload.get("llm_fallbacks"))

    openai_env_var = _normalize_optional_text(openai_section.get("api_key_env_var")) or "OPENAI_API_KEY"
    anthropic_env_var = _normalize_optional_text(anthropic_section.get("api_key_env_var")) or "ANTHROPIC_API_KEY"
    deepseek_env_var = _normalize_optional_text(deepseek_section.get("api_key_env_var")) or "DEEPSEEK_API_KEY"

    routes: dict[str, list[str]] = {}
    for task, default_route in DEFAULT_LLM_FALLBACKS.items():
        configured_route = fallbacks_section.get(task)
        normalized_route = _normalize_route(configured_route)
        routes[task] = normalized_route or list(default_route)
    s3_route = list(routes.get("s3_translate", DEFAULT_LLM_FALLBACKS["s3_translate"]))
    for task in S3_SYNCED_TASKS:
        routes[task] = list(s3_route)

    return {
        "openai": {
            "api_key": _normalize_optional_text(openai_section.get("api_key"))
            or _normalize_optional_text(os.getenv(openai_env_var)),
            "api_key_env_var": openai_env_var,
            "base_url": _normalize_optional_text(openai_section.get("base_url")) or "https://api.openai.com/v1",
            "model_name": _normalize_optional_text(openai_section.get("model_name")) or DEFAULT_OPENAI_MODEL_NAME,
            "temperature": _coerce_float(openai_section.get("temperature"), default=DEFAULT_TEMPERATURE),
            "max_output_tokens": _coerce_int(
                openai_section.get("max_output_tokens"),
                default=DEFAULT_MAX_OUTPUT_TOKENS,
            ),
            "timeout_seconds": _coerce_float(
                openai_section.get("timeout_seconds"),
                default=DEFAULT_TIMEOUT_SECONDS,
            ),
        },
        "anthropic": {
            "api_key": _normalize_optional_text(anthropic_section.get("api_key"))
            or _normalize_optional_text(os.getenv(anthropic_env_var)),
            "api_key_env_var": anthropic_env_var,
            "base_url": _normalize_optional_text(anthropic_section.get("base_url")) or "https://api.anthropic.com",
            "model_name": _normalize_optional_text(anthropic_section.get("model_name"))
            or DEFAULT_ANTHROPIC_MODEL_NAME,
            "temperature": _coerce_float(anthropic_section.get("temperature"), default=DEFAULT_TEMPERATURE),
            "max_output_tokens": _coerce_int(
                anthropic_section.get("max_output_tokens"),
                default=DEFAULT_MAX_OUTPUT_TOKENS,
            ),
            "timeout_seconds": _coerce_float(
                anthropic_section.get("timeout_seconds"),
                default=DEFAULT_TIMEOUT_SECONDS,
            ),
        },
        "deepseek": {
            "api_key": _normalize_optional_text(deepseek_section.get("api_key"))
            or _normalize_optional_text(os.getenv(deepseek_env_var)),
            "api_key_env_var": deepseek_env_var,
            "base_url": _normalize_optional_text(deepseek_section.get("base_url")) or "https://api.deepseek.com/v1",
            "model_name": _normalize_optional_text(deepseek_section.get("model_name"))
            or DEFAULT_DEEPSEEK_MODEL_NAME,
            "temperature": _coerce_float(deepseek_section.get("temperature"), default=DEFAULT_TEMPERATURE),
            "max_output_tokens": _coerce_int(
                deepseek_section.get("max_output_tokens"),
                default=DEFAULT_MAX_OUTPUT_TOKENS,
            ),
            "timeout_seconds": _coerce_float(
                deepseek_section.get("timeout_seconds"),
                default=DEFAULT_TIMEOUT_SECONDS,
            ),
        },
        "gemini": {
            "model_name": _normalize_optional_text(gemini_section.get("model_name")),
            "temperature": _coerce_float(gemini_section.get("temperature"), default=DEFAULT_TEMPERATURE),
            "max_output_tokens": _coerce_int(
                gemini_section.get("max_output_tokens"),
                default=DEFAULT_MAX_OUTPUT_TOKENS,
            ),
        },
        "llm_models": _merge_llm_models(llm_models_section),
        "llm_fallbacks": routes,
    }


class LLMRouter:
    def __init__(
        self,
        config: dict[str, object],
        *,
        providers: dict[str, LLMProvider] | None = None,
    ) -> None:
        self.routes = {
            task: _normalize_route(route)
            for task, route in _ensure_dict(config.get("llm_fallbacks")).items()
            if isinstance(route, list)
        }
        self.gemini_config = _ensure_dict(config.get("gemini"))
        self.model_configs = {
            alias: dict(model_payload)
            for alias, model_payload in _ensure_dict(config.get("llm_models")).items()
            if isinstance(alias, str) and isinstance(model_payload, dict)
        }
        self.providers = providers if providers is not None else self._build_default_providers(config)

    def get_route(self, task: str) -> list[str]:
        normalized_task = "s3_translate" if task in S3_SYNCED_TASKS else task
        route = self.routes.get(normalized_task)
        if route:
            return list(route)
        return list(DEFAULT_LLM_FALLBACKS.get(normalized_task, [DEFAULT_DEFAULT_LLM_ALIAS]))

    def get_model_config(self, alias: str) -> dict[str, object]:
        normalized_alias = _normalize_alias(alias)
        if normalized_alias == DEFAULT_DEFAULT_LLM_ALIAS:
            model_name = _normalize_optional_text(self.gemini_config.get("model_name"))
            return {
                "provider": "gemini",
                "model_name": model_name,
                "temperature": _coerce_float(self.gemini_config.get("temperature"), default=DEFAULT_TEMPERATURE),
                "max_output_tokens": _coerce_int(
                    self.gemini_config.get("max_output_tokens"),
                    default=DEFAULT_MAX_OUTPUT_TOKENS,
                ),
            }
        model_config = self.model_configs.get(alias)
        if isinstance(model_config, dict):
            return dict(model_config)
        model_config = self.model_configs.get(normalized_alias)
        if isinstance(model_config, dict):
            return dict(model_config)
        return {}

    def generate_via_alias(
        self,
        alias: str,
        *,
        prompt: str,
        json_mode: bool = False,
    ) -> str:
        provider = self.providers.get(alias)
        if provider is None:
            raise LLMProviderError(f"No LLM provider is configured for alias '{alias}'.")

        config = getattr(provider, "config", None)
        if not isinstance(config, LLMCallConfig):
            raise LLMProviderError(f"LLM provider '{alias}' is missing call config.")

        return provider.generate_text(
            prompt=prompt,
            model_name=config.model_name,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
            json_mode=json_mode,
        )

    def _build_default_providers(self, config: dict[str, object]) -> dict[str, LLMProvider]:
        providers: dict[str, LLMProvider] = {}
        openai_section = _ensure_dict(config.get("openai"))
        anthropic_section = _ensure_dict(config.get("anthropic"))
        deepseek_section = _ensure_dict(config.get("deepseek"))
        llm_models = self.model_configs

        for alias, model_payload in llm_models.items():
            if not isinstance(alias, str):
                continue
            model_section = _ensure_dict(model_payload)
            provider_name = _normalize_optional_text(model_section.get("provider"))
            if provider_name == "openai":
                providers[alias] = OpenAIProvider(
                    _build_call_config(
                        openai_section,
                        provider="openai",
                        model_name=_normalize_optional_text(model_section.get("model_name")),
                        temperature=model_section.get("temperature"),
                        max_output_tokens=model_section.get("max_output_tokens"),
                    )
                )
            elif provider_name == "anthropic":
                providers[alias] = AnthropicProvider(
                    _build_call_config(
                        anthropic_section,
                        provider="anthropic",
                        model_name=_normalize_optional_text(model_section.get("model_name")),
                        temperature=model_section.get("temperature"),
                        max_output_tokens=model_section.get("max_output_tokens"),
                    )
                )
            elif provider_name == "deepseek":
                providers[alias] = DeepSeekProvider(
                    _build_call_config(
                        deepseek_section,
                        provider="deepseek",
                        model_name=_normalize_optional_text(model_section.get("model_name")),
                        temperature=model_section.get("temperature"),
                        max_output_tokens=model_section.get("max_output_tokens"),
                    )
                )
        return providers


def _build_call_config(
    section: dict[str, object],
    *,
    provider: str,
    model_name: str | None = None,
    temperature: object | None = None,
    max_output_tokens: object | None = None,
) -> LLMCallConfig:
    return LLMCallConfig(
        provider=provider,
        model_name=model_name
        or _normalize_optional_text(section.get("model_name"))
        or (
            DEFAULT_OPENAI_MODEL_NAME
            if provider == "openai"
            else DEFAULT_ANTHROPIC_MODEL_NAME
            if provider == "anthropic"
            else DEFAULT_DEEPSEEK_MODEL_NAME
        ),
        api_key=_normalize_optional_text(section.get("api_key")),
        api_key_env_var=_normalize_optional_text(section.get("api_key_env_var")),
        base_url=_normalize_optional_text(section.get("base_url")),
        temperature=_coerce_float(
            temperature if temperature is not None else section.get("temperature"),
            default=DEFAULT_TEMPERATURE,
        ),
        max_output_tokens=_coerce_int(
            max_output_tokens if max_output_tokens is not None else section.get("max_output_tokens"),
            default=DEFAULT_MAX_OUTPUT_TOKENS,
        ),
        timeout_seconds=_coerce_float(
            section.get("timeout_seconds"),
            default=DEFAULT_TIMEOUT_SECONDS,
        ),
    )


def _normalize_route(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized_items: list[str] = []
    for item in value:
        normalized_item = _normalize_optional_text(item)
        if normalized_item is not None:
            normalized_items.append(_normalize_alias(normalized_item))
    return normalized_items


def _normalize_alias(alias: str) -> str:
    normalized_alias = _normalize_optional_text(alias)
    if normalized_alias is None:
        return DEFAULT_DEFAULT_LLM_ALIAS
    if normalized_alias == LEGACY_GEMINI_CURRENT_ALIAS:
        return DEFAULT_DEFAULT_LLM_ALIAS
    return normalized_alias


def _merge_llm_models(configured_models: dict[str, object]) -> dict[str, dict[str, object]]:
    merged_models = {
        alias: dict(model_payload)
        for alias, model_payload in DEFAULT_LLM_MODELS.items()
    }
    for alias, model_payload in configured_models.items():
        if not isinstance(alias, str):
            continue
        if not isinstance(model_payload, dict):
            continue
        current_payload = merged_models.get(alias, {})
        merged_models[alias] = {**current_payload, **model_payload}
    return merged_models


def _ensure_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _coerce_float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _coerce_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)
