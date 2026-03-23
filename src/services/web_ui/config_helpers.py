from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from services import config_loader
from services.gemini.translator import (
    TranslationError,
    get_effective_rewrite_prompt_template,
    get_effective_speaker_infer_prompt_template,
    get_effective_translation_prompt_template,
    validate_rewrite_prompt_template,
    validate_speaker_infer_prompt_template,
    validate_translation_prompt_template,
)
import services.llm.router as llm_router_module
from services.llm.router import (
    DEFAULT_DEFAULT_LLM_ALIAS,
    DEFAULT_LLM_FALLBACKS,
    S3_SYNCED_TASKS,
    load_llm_fallback_config,
)

PROVIDER_DISPLAY_NAMES = {
    "gemini": "Gemini",
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
}

PROMPT_TEMPLATE_LOADERS: dict[str, tuple[Callable[[object | None], str], Callable[[str], str]]] = {
    "s2_infer": (
        get_effective_speaker_infer_prompt_template,
        validate_speaker_infer_prompt_template,
    ),
    "s3_translate": (
        get_effective_translation_prompt_template,
        validate_translation_prompt_template,
    ),
    "s5_rewrite": (
        get_effective_rewrite_prompt_template,
        validate_rewrite_prompt_template,
    ),
}


def _normalize_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _ensure_dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _resolve_provider_key_source(
    section: dict[str, object],
    *,
    api_key_env_var: str,
) -> str | None:
    configured_key = _normalize_optional_text(section.get("api_key"))
    if configured_key is not None:
        return "config"
    if api_key_env_var and _normalize_optional_text(os.environ.get(api_key_env_var)) is not None:
        return "env"
    return None


def build_translation_model_options(*, config_path: Path | None = None) -> list[dict[str, str]]:
    config = load_llm_fallback_config_for_path(config_path)
    current_route = list(
        config["llm_fallbacks"].get("s3_translate", DEFAULT_LLM_FALLBACKS["s3_translate"])
    )
    ordered_aliases: list[str] = []
    for alias in [DEFAULT_DEFAULT_LLM_ALIAS, *current_route, *config["llm_models"].keys()]:
        if alias not in ordered_aliases:
            ordered_aliases.append(alias)

    options: list[dict[str, str]] = []
    gemini_model_name = str(config["gemini"].get("model_name") or "").strip()
    for alias in ordered_aliases:
        if alias == DEFAULT_DEFAULT_LLM_ALIAS:
            label = f"default_llm (当前默认: {gemini_model_name or '未设置'})"
            options.append(
                {
                    "alias": alias,
                    "label": label,
                    "provider": "gemini",
                    "model_name": gemini_model_name or "",
                }
            )
            continue
        model_payload = config["llm_models"].get(alias)
        if not isinstance(model_payload, dict):
            continue
        provider = str(model_payload.get("provider") or "").strip()
        model_name = str(model_payload.get("model_name") or "").strip()
        label = f"{alias} ({provider}: {model_name})" if provider and model_name else alias
        options.append(
            {
                "alias": alias,
                "label": label,
                "provider": provider,
                "model_name": model_name,
            }
        )
    return options


def build_provider_key_options(*, config_path: Path | None = None) -> list[dict[str, object]]:
    loaded_config = config_loader.load_project_local_config(config_path)
    editable_payload = config_loader.build_editable_project_local_config_payload(loaded_config)
    llm_config = load_llm_fallback_config_for_path(config_path)
    model_options = build_translation_model_options(config_path=config_path)

    provider_to_aliases: dict[str, list[str]] = {}
    provider_to_models: dict[str, list[str]] = {}
    for option in model_options:
        provider = str(option.get("provider") or "").strip()
        alias = str(option.get("alias") or "").strip()
        model_name = str(option.get("model_name") or "").strip()
        if not provider or not alias:
            continue
        provider_to_aliases.setdefault(provider, []).append(alias)
        if model_name:
            provider_to_models.setdefault(provider, []).append(model_name)

    rows: list[dict[str, object]] = []
    for provider in ("gemini", "deepseek", "openai", "anthropic"):
        section = editable_payload.get(provider)
        if not isinstance(section, dict):
            section = {}
        api_key_env_var = str(section.get("api_key_env_var") or "")
        configured_source = _resolve_provider_key_source(section, api_key_env_var=api_key_env_var)
        rows.append(
            {
                "provider": provider,
                "label": PROVIDER_DISPLAY_NAMES.get(provider, provider),
                "api_key_env_var": api_key_env_var,
                "is_configured": configured_source is not None,
                "configured_source": configured_source or "",
                "model_aliases": provider_to_aliases.get(provider, []),
                "model_names": provider_to_models.get(provider, []),
                "default_model_name": str(_ensure_dict(llm_config.get(provider)).get("model_name") or ""),
            }
        )
    return rows


def build_route_visualization(task: str, *, config_path: Path | None = None) -> list[dict[str, str]]:
    config = load_llm_fallback_config_for_path(config_path)
    route = list(config["llm_fallbacks"].get(task, DEFAULT_LLM_FALLBACKS.get(task, [])))
    option_map = {
        option["alias"]: option["label"]
        for option in build_translation_model_options(config_path=config_path)
    }
    return [{"alias": alias, "label": option_map.get(alias, alias)} for alias in route]


def set_translation_primary_model(
    alias: str,
    *,
    config_path: Path | None = None,
) -> list[str]:
    config = load_llm_fallback_config_for_path(config_path)
    available_aliases = {
        option["alias"] for option in build_translation_model_options(config_path=config_path)
    }
    if alias not in available_aliases:
        raise ValueError(f"未知翻译模型别名：{alias}")

    current_route = list(
        config["llm_fallbacks"].get("s3_translate", DEFAULT_LLM_FALLBACKS["s3_translate"])
    )
    updated_route = [alias, *[item for item in current_route if item != alias]]
    loaded_config = config_loader.load_project_local_config(config_path)
    editable_payload = config_loader.build_editable_project_local_config_payload(loaded_config)
    existing_fallbacks = editable_payload.get("llm_fallbacks")
    merged_fallbacks = dict(existing_fallbacks) if isinstance(existing_fallbacks, dict) else {}
    merged_fallbacks["s3_translate"] = updated_route
    for task in S3_SYNCED_TASKS:
        merged_fallbacks[task] = list(updated_route)
    config_loader.save_project_local_config_sections(
        {"llm_fallbacks": merged_fallbacks},
        config_path=config_path,
    )
    return updated_route


def save_web_ui_settings(
    *,
    translation_model_alias: str,
    speaker_infer_prompt_template: str | None = None,
    translation_prompt_template: str | None = None,
    rewrite_prompt_template: str | None = None,
    provider_api_keys: dict[str, str | None],
    config_path: Path | None = None,
) -> list[str]:
    updated_route = set_translation_primary_model(translation_model_alias, config_path=config_path)
    loaded_config = config_loader.load_project_local_config(config_path)
    editable_payload = config_loader.build_editable_project_local_config_payload(loaded_config)
    section_overrides: dict[str, object] = {}
    for provider in ("gemini", "deepseek", "openai", "anthropic"):
        existing_section = editable_payload.get(provider)
        merged_section = dict(existing_section) if isinstance(existing_section, dict) else {}
        if provider in provider_api_keys:
            normalized_value = (provider_api_keys[provider] or "").strip()
            merged_section["api_key"] = normalized_value or None
        section_overrides[provider] = merged_section
    prompts_section = editable_payload.get("prompts")
    merged_prompts = dict(prompts_section) if isinstance(prompts_section, dict) else {}
    prompt_updates = {
        "s2_infer": speaker_infer_prompt_template,
        "s3_translate": translation_prompt_template,
        "s5_rewrite": rewrite_prompt_template,
    }
    for prompt_key, raw_template in prompt_updates.items():
        _default_loader, validator = PROMPT_TEMPLATE_LOADERS[prompt_key]
        normalized_prompt_template = _normalize_optional_text(raw_template)
        if normalized_prompt_template is None:
            merged_prompts[prompt_key] = None
            continue
        try:
            merged_prompts[prompt_key] = validator(normalized_prompt_template)
        except TranslationError as exc:
            raise ValueError(str(exc)) from exc
    section_overrides["prompts"] = merged_prompts
    config_loader.save_project_local_config_sections(section_overrides, config_path=config_path)
    return updated_route


def load_llm_fallback_config_for_path(config_path: Path | None) -> dict[str, object]:
    if config_path is None:
        return load_llm_fallback_config()

    resolved_path = config_path.resolve(strict=False)
    original_path = llm_router_module.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH
    try:
        llm_router_module.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH = resolved_path
        return load_llm_fallback_config()
    finally:
        llm_router_module.DEFAULT_AUTODUB_LOCAL_CONFIG_PATH = original_path


def _load_selected_translation_model_alias(config_path: Path) -> str:
    config = load_llm_fallback_config_for_path(config_path)
    route = list(config["llm_fallbacks"].get("s3_translate", DEFAULT_LLM_FALLBACKS["s3_translate"]))
    if route:
        return str(route[0])
    return "gemini_3_1_flash_lite_preview"


def _find_translation_model_label(alias: str, *, config_path: Path) -> str:
    for option in build_translation_model_options(config_path=config_path):
        if option["alias"] == alias:
            return option["label"]
    return alias


def _load_prompt_templates(config_path: Path) -> dict[str, dict[str, str]]:
    loaded_config = config_loader.load_project_local_config(config_path)
    editable_payload = config_loader.build_editable_project_local_config_payload(loaded_config)
    prompts_section = editable_payload.get("prompts")
    result: dict[str, dict[str, str]] = {}
    for prompt_key, (default_loader, validator) in PROMPT_TEMPLATE_LOADERS.items():
        raw_template = None
        if isinstance(prompts_section, dict):
            raw_template = _normalize_optional_text(prompts_section.get(prompt_key))
        if raw_template is not None:
            try:
                result[prompt_key] = {
                    "template": validator(raw_template),
                    "source": "custom",
                }
                continue
            except TranslationError:
                pass
        result[prompt_key] = {
            "template": default_loader(None),
            "source": "default",
        }
    return result
