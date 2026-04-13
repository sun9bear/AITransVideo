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
    "gemini": {
        "api_model_id": "gemini-2.5-flash-lite",
        "provider": "gemini",
        "supports_audio": True,
        "auth": "client_factory",
        "cost_rank": 2,
        "label": "Gemini 2.5 Flash Lite（低成本）",
        "cost_hint": "¥0.27/h 音频",
    },
    # DeepSeek (text-only)
    "deepseek": {
        "api_model_id": "deepseek-chat",
        "provider": "deepseek",
        "supports_audio": False,
        "api_key_env": "DEEPSEEK_API_KEY",
        "cost_rank": 3,
        "label": "DeepSeek Chat",
        "cost_hint": "¥1/百万 token",
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
    # MiMo (text-only, free)
    "mimo_omni": {
        "api_model_id": "mimo-v2-omni",
        "provider": "mimo",
        "supports_audio": False,
        "api_key_env": "MIMO_API_KEY",
        "cost_rank": 1,
        "label": "MiMo-V2-Omni（免费）",
        "cost_hint": "免费",
    },
}

# Default model per prompt key (used when admin hasn't configured)
_DEFAULTS: dict[str, str] = {
    "pass1": "gemini_pro",
    "pass2": "gemini",
    "pass3": "gemini_pro",
    "translate": "deepseek",
    "rewrite": "deepseek",
    "probe_translate": "deepseek",
}

# ---------------------------------------------------------------------------
# Settings cache (TTL-based to avoid repeated file reads in a single pipeline)
# ---------------------------------------------------------------------------

_SETTINGS_PATH = Path("/opt/aivideotrans/config/admin_settings.json")
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

    Parameters
    ----------
    mode : "studio" | "express"
    prompt_key : "pass1" | "pass2" | "pass3" | "translate" | "rewrite"

    Returns
    -------
    Logical model name (key in MODEL_REGISTRY).
    """
    settings = _load_settings()
    models = settings.get("prompt_models", {}).get(mode, {})
    model = models.get(prompt_key, "")
    if model and model in MODEL_REGISTRY:
        return model
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


def get_available_models_for_prompt(prompt_key: str) -> list[dict]:
    """Get the list of selectable models for a prompt key (for admin UI).

    Pass 1/3 require audio → only audio-capable models.
    Pass 2/translate/rewrite → all models.
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
