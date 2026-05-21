"""LLM model registry + per-prompt model/key resolution.

Single source of truth for model metadata, per-prompt model selection,
API key resolution, and cost-based auto-fallback.

Replaces the old scattered model maps:
- transcript_reviewer._MODEL_MAP
- llm/router.DEFAULT_LLM_MODELS
- admin_settings.review_model / translation_model
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model Registry — single source of truth
# ---------------------------------------------------------------------------

MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    # Gemini series (supports audio)
    # Auth: client_factory.create_gemini_client() handles credentials
    # Priority: GOOGLE_APPLICATION_CREDENTIALS → VERTEX_AI_EXPRESS_KEY → GEMINI_API_KEY
    # Not managed via provider_api_keys
    "gemini_pro": {
        "api_model_id": "gemini-3.1-pro-preview",
        "provider": "gemini",
        "supports_audio": True,
        "auth": "client_factory",
        "cost_rank": 5,
        "label": "Gemini 3.1 Pro（高质量）",
        "cost_hint": "¥2.4/h 音频",
    },
    "gemini_35_flash": {
        "api_model_id": "gemini-3.5-flash",
        "provider": "gemini",
        "supports_audio": True,
        "auth": "client_factory",
        # Same broad tier as gemini_pro so this new selectable model does not
        # silently change Pro fallback behavior.
        "cost_rank": 5,
        "label": "Gemini 3.5 Flash（稳定）",
        "cost_hint": "¥10.8/¥64.8 每百万 token",
    },
    "gemini": {
        "api_model_id": "gemini-2.5-flash-lite",
        "provider": "gemini",
        "supports_audio": True,
        "auth": "client_factory",
        "cost_rank": 2,
        "label": "Gemini 2.5 Flash Lite（低成本）",
        "cost_hint": "¥0.27/h 音频",
    },
    "gemini_31_flash_lite": {
        "api_model_id": "gemini-3.1-flash-lite",
        "provider": "gemini",
        "supports_audio": True,
        "auth": "client_factory",
        "cost_rank": 2,
        "label": "Gemini 3.1 Flash Lite（快速稳定）",
        "cost_hint": "¥0.25/百万 token",
    },
    # DeepSeek (text-only)
    # DeepSeek V4 defaults thinking mode to enabled. Translation/rewrite are
    # structured-output stages, so the low-cost default keeps non-thinking
    # behavior explicit instead of relying on the retiring deepseek-chat alias.
    "deepseek": {
        "api_model_id": "deepseek-v4-flash",
        "provider": "deepseek",
        "supports_audio": False,
        "api_key_env": "DEEPSEEK_API_KEY",
        "cost_rank": 3,
        "label": "DeepSeek V4 Flash（快速）",
        "cost_hint": "$0.14/$0.28 每百万 token",
        "request_overrides": {"thinking": {"type": "disabled"}},
    },
    "deepseek_v4_pro": {
        "api_model_id": "deepseek-v4-pro",
        "provider": "deepseek",
        "supports_audio": False,
        "api_key_env": "DEEPSEEK_API_KEY",
        "cost_rank": 4,
        "label": "DeepSeek V4 Pro（高质量）",
        "cost_hint": "$0.435/$0.87 每百万 token（限时）",
        "request_overrides": {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        },
    },
    # OpenAI text-only series
    "openai": {
        "api_model_id": "gpt-4.1",
        "provider": "openai",
        "supports_audio": False,
        "api_key_env": "OPENAI_API_KEY",
        "cost_rank": 4,
        "label": "GPT-4.1",
        "cost_hint": "¥0.15/千 token",
    },
    "gpt54": {
        "api_model_id": "gpt-5.4",
        "provider": "openai",
        "supports_audio": False,
        "api_key_env": "OPENAI_API_KEY",
        "cost_rank": 6,
        "label": "GPT-5.4（高质量）",
        "cost_hint": "约 ¥0.5/千 token",
    },
    # MiMo (Xiaomi)
    "mimo_v25": {
        "api_model_id": "mimo-v2.5",
        "provider": "mimo",
        "supports_audio": True,
        "api_key_env": "MIMO_API_KEY",
        "cost_rank": 2,
        "label": "MiMo-V2.5（全模态）",
        "cost_hint": "Token Plan 1x（音频已验证）",
    },
    "mimo_v25_pro": {
        "api_model_id": "mimo-v2.5-pro",
        "provider": "mimo",
        "supports_audio": False,
        "api_key_env": "MIMO_API_KEY",
        "cost_rank": 4,
        "label": "MiMo-V2.5-Pro（Agent 文本）",
        "cost_hint": "Token Plan 2x（音频 payload 未开放）",
    },
    # Legacy MiMo Omni — keep for existing admin settings and audio fallback paths.
    "mimo_omni": {
        "api_model_id": "mimo-v2-omni",
        "provider": "mimo",
        "supports_audio": True,
        "api_key_env": "MIMO_API_KEY",
        "cost_rank": 1,
        "label": "MiMo-V2-Omni（旧版）",
        "cost_hint": "旧版",
    },
}

# Default model per prompt key (used when admin hasn't configured AND mode
# has no entry in ``_MODE_DEFAULTS``). Studio/express historically used this
# flat fallback; smart-mode now goes through ``_MODE_DEFAULTS["smart"]``
# first (Codex 第四十一轮, 2026-05-16 user request).
_DEFAULTS: dict[str, str] = {
    "pass1": "gemini_pro",
    "pass2": "gemini",
    "pass3": "gemini_pro",
    "translate": "deepseek",
    "rewrite": "deepseek",
    "rewrite_strict": "gemini_pro",
    "probe_translate": "deepseek",
    "content_compliance": "gemini_31_flash_lite",
    # Customer support AI (plan 2026-05-08 §7.2). Default DeepSeek V4 Flash:
    # short text Q&A, FAQ rewrite, classification — does not need a high
    # reasoning model. The default value is used by admin_support_api
    # when no admin override is set; it does NOT auto-activate the real
    # provider (that requires AVT_SUPPORT_AI_PROVIDER + DEEPSEEK_API_KEY).
    "support_chat": "deepseek",
}

# Per-mode defaults — overrides flat ``_DEFAULTS`` for the given mode when
# the admin hasn't saved a per-stage override.
#
# Smart mode default = all Gemini 3.1 Pro per user request 2026-05-16
# ("方案里要用最好的多模态大模型... 默认都用 Gemini 3.1 Pro"). Admin can
# still override any single stage via the 智能版 tab in the model
# management UI — the override lands in
# ``admin_settings.json::prompt_models["smart"]`` and takes precedence
# over this constant.
#
# Studio/express are deliberately NOT added here — adding them would
# change behavior for the unconfigured-admin case (today they go through
# the flat ``_DEFAULTS`` fallback, which is what existing deployments
# expect). If we ever want to consolidate per-mode defaults for studio
# /express too, that's a separate change that needs explicit testing
# of cost impact.
_MODE_DEFAULTS: dict[str, dict[str, str]] = {
    "smart": {
        "pass1": "gemini_pro",
        "pass2": "gemini_pro",
        "pass3": "gemini_pro",
        "translate": "gemini_pro",
        "rewrite": "gemini_pro",
        "probe_translate": "gemini_pro",
        "content_compliance": "gemini_pro",
    },
}

# ---------------------------------------------------------------------------
# Settings cache (TTL-based to avoid repeated file reads in a single pipeline)
# ---------------------------------------------------------------------------

_SETTINGS_PATH = Path(
    os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config")
) / "admin_settings.json"
_cache: dict | None = None
_cache_ts: float = 0
_CACHE_TTL = 5.0  # seconds


def _load_settings() -> dict:
    """Load admin settings with a 5-second TTL cache."""
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
        return _cache
    try:
        if _SETTINGS_PATH.exists():
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            _cache = data
            _cache_ts = now
            return data
    except Exception:
        logger.debug("Failed to read admin settings, using defaults")
    return {}


def invalidate_cache() -> None:
    """Force next call to re-read from disk. Useful after settings update."""
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _get_disabled_models() -> set[str]:
    """Get the set of models disabled by admin."""
    settings = _load_settings()
    disabled = settings.get("disabled_models", [])
    return set(disabled) if isinstance(disabled, list) else set()


def get_prompt_model(mode: str, prompt_key: str) -> str:
    """Get the model for a given mode + prompt key.

    Resolution order:
      1. Admin override in ``admin_settings.json::prompt_models[mode][prompt_key]``
      2. Per-mode default in ``_MODE_DEFAULTS[mode][prompt_key]`` (smart only today)
      3. Flat default in ``_DEFAULTS[prompt_key]`` (studio/express historical fallback)
      4. Hard-coded final fallback ``"gemini"``

    Parameters
    ----------
    mode : "studio" | "express" | "smart"
    prompt_key : "pass1" | "pass2" | "pass3" | "translate" | "rewrite"
                | "probe_translate" | "content_compliance"

    Returns
    -------
    Logical model name (key in MODEL_REGISTRY).
    """
    settings = _load_settings()
    models = settings.get("prompt_models", {}).get(mode, {})
    model = models.get(prompt_key, "")
    if model and model in MODEL_REGISTRY:
        return model
    mode_default = _MODE_DEFAULTS.get(mode, {}).get(prompt_key, "")
    if mode_default and mode_default in MODEL_REGISTRY:
        return mode_default
    return _DEFAULTS.get(prompt_key, "gemini")


def get_api_key(model_name: str) -> str | None:
    """Get the API key for a model.

    - Gemini: returns None (credentials handled by client_factory)
    - Others: provider_api_keys override > env var fallback
    """
    model_info = MODEL_REGISTRY.get(model_name, {})
    if model_info.get("auth") == "client_factory":
        return None
    provider = model_info.get("provider", "")
    # Check per-provider override from admin settings
    settings = _load_settings()
    provider_keys = settings.get("provider_api_keys", {})
    override = provider_keys.get(provider, "")
    if override:
        return override
    # Fall back to environment variable
    env_var = model_info.get("api_key_env", "")
    return os.environ.get(env_var, "").strip() if env_var else ""


def resolve_model_id(logical_name: str) -> str:
    """Convert logical name to API model ID."""
    return MODEL_REGISTRY.get(logical_name, {}).get("api_model_id", logical_name)


def get_fallback_candidates(
    model_name: str,
    requires_audio: bool,
    *,
    allowed_models: set[str] | None = None,
) -> list[str]:
    """Get fallback candidates cheaper than the current model.

    Returns models with cost_rank < current, matching capability,
    sorted by cost_rank descending (try best quality first).

    Parameters
    ----------
    model_name : current model logical name
    requires_audio : if True, only include models with supports_audio=True
    allowed_models : optional whitelist to constrain candidates per-prompt
    """
    current = MODEL_REGISTRY.get(model_name, {})
    current_rank = current.get("cost_rank", 99)
    disabled = _get_disabled_models()
    candidates = []
    for name, info in MODEL_REGISTRY.items():
        if name == model_name:
            continue
        if name in disabled:
            continue
        if info.get("cost_rank", 99) >= current_rank:
            continue
        if requires_audio and not info.get("supports_audio"):
            continue
        if allowed_models is not None and name not in allowed_models:
            continue
        candidates.append(name)
    candidates.sort(key=lambda n: MODEL_REGISTRY[n]["cost_rank"], reverse=True)
    return candidates


def get_peer_model_candidates(
    model_name: str,
    prompt_key: str,
    *,
    cost_rank_delta: int = 1,
) -> list[str]:
    """Get enabled alternate models with a similar cost rank for one prompt.

    Used by content-compliance review after the selected model has failed its
    immediate retry.  This avoids jumping from a low-cost policy check straight
    to a much more expensive model while still giving the stage a second route.
    """
    current = MODEL_REGISTRY.get(model_name, {})
    if not current:
        return []
    current_rank = int(current.get("cost_rank", 99))
    allowed = {
        str(item.get("value"))
        for item in get_available_models_for_prompt(prompt_key)
        if item.get("value")
    }
    candidates: list[str] = []
    for name, info in MODEL_REGISTRY.items():
        if name == model_name or name not in allowed:
            continue
        rank = int(info.get("cost_rank", 99))
        if abs(rank - current_rank) <= max(0, int(cost_rank_delta)):
            candidates.append(name)
    candidates.sort(
        key=lambda n: (
            abs(int(MODEL_REGISTRY[n].get("cost_rank", 99)) - current_rank),
            int(MODEL_REGISTRY[n].get("cost_rank", 99)),
            n,
        )
    )
    return candidates


def get_available_models_for_prompt(prompt_key: str) -> list[dict]:
    """Get the list of selectable models for a prompt key (for admin UI).

    Pass 1/3 require audio → only audio-capable models.
    Pass 2/translate/rewrite/content_compliance → all models.
    Disabled models are excluded.
    """
    requires_audio = prompt_key in ("pass1", "pass3")
    disabled = _get_disabled_models()
    result = []
    for name, info in MODEL_REGISTRY.items():
        if name in disabled:
            continue
        if requires_audio and not info.get("supports_audio"):
            continue
        result.append({
            "value": name,
            "label": info["label"],
            "cost_hint": info.get("cost_hint", ""),
            "cost_rank": info.get("cost_rank", 99),
        })
    result.sort(key=lambda m: m["cost_rank"])
    return result


def get_all_models_with_status() -> list[dict]:
    """Get all models with enabled/disabled status (for admin model management UI)."""
    disabled = _get_disabled_models()
    result = []
    for name, info in MODEL_REGISTRY.items():
        result.append({
            "value": name,
            "label": info["label"],
            "cost_hint": info.get("cost_hint", ""),
            "cost_rank": info.get("cost_rank", 99),
            "supports_audio": info.get("supports_audio", False),
            "provider": info.get("provider", ""),
            "enabled": name not in disabled,
        })
    result.sort(key=lambda m: m["cost_rank"])
    return result
