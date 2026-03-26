"""TTS provider selection strategy.

Free users: always "mimo" (auto-generated, no cloning needed).
Paid users: use admin_settings.json tts_provider (default "minimax", supports cloning).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "autodub.local.json"
ADMIN_SETTINGS_PATH = Path("/opt/aivideotrans/config/admin_settings.json")

VALID_PROVIDERS = {"minimax", "mimo"}
DEFAULT_PROVIDER = "minimax"  # Default for paid users
FREE_USER_PROVIDER = "mimo"   # Always for free users


def _is_free_user() -> bool:
    """Check if current pipeline run is for a free user (skip_all_reviews mode).

    Note: Pipeline doesn't know the current user's identity. This is a global
    toggle. When per-user tiers are implemented, the user tier should be passed
    as a job parameter from Gateway.
    """
    try:
        if ADMIN_SETTINGS_PATH.exists():
            with open(ADMIN_SETTINGS_PATH) as f:
                settings = json.load(f)
            return bool(settings.get("skip_all_reviews_for_free_users", True))
    except Exception:
        pass
    return True  # Default: treat as free user


def get_tts_provider(config_path: Path | None = None) -> str:
    """Returns TTS provider name.

    Free users: always "mimo" (auto voice_description, no cloning).
    Paid users: admin_settings.json -> tts_provider (default "minimax").

    Override: TTS_PROVIDER env var forces a specific provider for all users.
    """
    # 0. Environment variable override (highest priority, ignores user tier)
    env_val = os.environ.get("TTS_PROVIDER", "").strip().lower()
    if env_val in VALID_PROVIDERS:
        return env_val

    # 1. Free users always use MiMo
    if _is_free_user():
        return FREE_USER_PROVIDER

    # 2. Paid users: read from admin_settings.json
    try:
        if ADMIN_SETTINGS_PATH.exists():
            with open(ADMIN_SETTINGS_PATH) as f:
                settings = json.load(f)
            provider = str(settings.get("tts_provider", "")).strip().lower()
            if provider in VALID_PROVIDERS:
                return provider
    except Exception:
        pass

    # 3. Fallback: autodub.local.json
    path = (config_path or DEFAULT_CONFIG_PATH).resolve()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                tts_section = data.get("tts", {})
                if isinstance(tts_section, dict):
                    provider = str(tts_section.get("provider", "")).strip().lower()
                    if provider in VALID_PROVIDERS:
                        return provider
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass

    return DEFAULT_PROVIDER


def get_tts_rpm(provider: str) -> int:
    """Return the RPM limit for a given provider."""
    if provider == "mimo":
        return 100
    # minimax default
    return 20
