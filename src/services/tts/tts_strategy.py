"""TTS provider selection strategy — snapshot-based.

Each job carries its own `tts_provider` in the snapshot fields written at
creation time.  The functions here read that snapshot and derive RPM limits
and fallback chains without any global "free user" toggle.

Backward compatibility: when a job record has no snapshot field the code
falls back to reading admin_settings.json / autodub.local.json, matching
the old behaviour.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "autodub.local.json"
ADMIN_SETTINGS_PATH = Path("/opt/aivideotrans/config/admin_settings.json")

VALID_PROVIDERS = {"minimax", "mimo", "cosyvoice", "volcengine"}
DEFAULT_PROVIDER = "minimax"

# ---------------------------------------------------------------------------
# Per-provider RPM limits
# ---------------------------------------------------------------------------
_PROVIDER_RPM: dict[str, int] = {
    "cosyvoice": 180,   # ~3 RPS
    "minimax": 20,
    "mimo": 100,
    "volcengine": 60,   # 初始保守值，官方默认 10 并发，待压测修正
}


def get_tts_provider_for_job(job_record: Any) -> str:
    """Return the TTS provider string stored in a job's snapshot fields.

    *job_record* can be any object / dict that exposes ``tts_provider``
    (attribute or key).  If the value is missing or invalid the function
    falls back to the legacy resolution order (env → admin_settings →
    autodub.local.json).
    """
    provider = _read_field(job_record, "tts_provider")
    if provider and provider in VALID_PROVIDERS:
        return provider

    # Backward compatibility — legacy resolution
    return _legacy_resolve_provider()


def get_tts_rpm(provider: str) -> int:
    """Return the RPM (requests-per-minute) limit for *provider*."""
    return _PROVIDER_RPM.get(provider, 20)


# Alias for clarity
get_provider_rpm = get_tts_rpm


def get_fallback_provider(provider: str, voice_clone_enabled: bool = False) -> str | None:
    """Return the fallback provider to try when *provider* fails.

    Returns ``None`` when no fallback is available.
    """
    if provider == "cosyvoice":
        return None
    if provider == "minimax":
        if voice_clone_enabled:
            return None          # cloning is minimax-only, no fallback
        return "cosyvoice"
    if provider == "volcengine":
        return "cosyvoice"
    # mimo → no fallback
    return None


# ---------------------------------------------------------------------------
# Legacy provider resolution (kept for jobs without snapshot fields)
# ---------------------------------------------------------------------------

def _legacy_resolve_provider(config_path: Path | None = None) -> str:
    """Resolve provider the old way: env → admin_settings → config file."""
    # 0. Env override
    env_val = os.environ.get("TTS_PROVIDER", "").strip().lower()
    if env_val in VALID_PROVIDERS:
        return env_val

    # 1. admin_settings.json
    try:
        if ADMIN_SETTINGS_PATH.exists():
            with open(ADMIN_SETTINGS_PATH) as f:
                settings = json.load(f)
            provider = str(settings.get("tts_provider", "")).strip().lower()
            if provider in VALID_PROVIDERS:
                return provider
    except Exception:
        pass

    # 2. autodub.local.json
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


# Keep the old name as an alias so existing callers keep working.
def get_tts_provider(config_path: Path | None = None) -> str:  # noqa: D401
    """Legacy shim — resolves provider without a job record."""
    return _legacy_resolve_provider(config_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_field(obj: Any, key: str) -> str | None:
    """Read a string field from *obj* (dict-like or attribute access)."""
    raw: Any = None
    if isinstance(obj, dict):
        raw = obj.get(key)
    else:
        raw = getattr(obj, key, None)
    if raw is None:
        return None
    val = str(raw).strip().lower()
    return val or None
