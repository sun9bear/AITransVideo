"""CosyVoice endpoint configuration — runtime vs offline endpoint mode.

Runtime (express/production TTS) defaults to international endpoint.
Offline (B2 calibration/profiling) defaults to mainland endpoint.

Resolution priority (highest first):
    1. Environment variable (COSYVOICE_RUNTIME_ENDPOINT_MODE / COSYVOICE_OFFLINE_ENDPOINT_MODE)
    2. admin_settings.json (cosyvoice_runtime_endpoint_mode / cosyvoice_offline_endpoint_mode)
    3. Hardcoded default (international / mainland)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Endpoint URLs
# ---------------------------------------------------------------------------

INTL_WS_URL: Final[str] = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference"
MAINLAND_WS_URL: Final[str] = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_RUNTIME_MODE: Final[str] = "international"
DEFAULT_OFFLINE_MODE: Final[str] = "mainland"

# Valid mode values (normalized to lowercase)
_INTL_ALIASES: Final[frozenset[str]] = frozenset({"international", "intl"})
_MAINLAND_ALIASES: Final[frozenset[str]] = frozenset({"mainland", "cn", "domestic"})

# Settings file path (same as gateway admin_settings.py)
_ADMIN_SETTINGS_PATH: Final[Path] = Path(
    os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config")
) / "admin_settings.json"

# ---------------------------------------------------------------------------
# Intl endpoint voice availability (audit node 10 results, 2026-03-30)
# Voices that return OK on wss://dashscope-intl.aliyuncs.com with
# cosyvoice-v3-flash. All other matchable voices return error 418.
# ---------------------------------------------------------------------------

INTL_AVAILABLE_VOICES: Final[frozenset[str]] = frozenset({
    "longanyang",
    "longanhuan",
    "longhuhu_v3",
    "longanzhi_v3",
    "longanwen_v3",
    "longanyun_v3",
    "longanlang_v3",
    "longjiqi_v3",
    "longlaobo_v3",
    "longlaoyi_v3",
})


def _normalize_mode(raw: str) -> str:
    """Normalize endpoint mode string to 'international' or 'mainland'."""
    val = raw.strip().lower()
    if val in _INTL_ALIASES:
        return "international"
    if val in _MAINLAND_ALIASES:
        return "mainland"
    return val or "international"


def _read_admin_settings_field(key: str) -> str:
    """Read a single field from admin_settings.json. Returns '' if missing."""
    try:
        path = _ADMIN_SETTINGS_PATH
        if not path.exists():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get(key, "")).strip()
    except Exception:
        return ""


def _write_admin_settings_field(key: str, value: str) -> None:
    """Write a single field to admin_settings.json (merge, not overwrite)."""
    try:
        path = _ADMIN_SETTINGS_PATH
        data: dict = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        data[key] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_runtime_endpoint_mode() -> str:
    """Get the endpoint mode for runtime/production CosyVoice calls.

    Priority: env > admin_settings.json > default.
    """
    raw = os.environ.get("COSYVOICE_RUNTIME_ENDPOINT_MODE", "").strip()
    if raw:
        return _normalize_mode(raw)
    raw = _read_admin_settings_field("cosyvoice_runtime_endpoint_mode")
    if raw:
        return _normalize_mode(raw)
    return DEFAULT_RUNTIME_MODE


def get_offline_endpoint_mode() -> str:
    """Get the endpoint mode for offline B2 calibration/profiling.

    Priority: env > admin_settings.json > default.
    """
    raw = os.environ.get("COSYVOICE_OFFLINE_ENDPOINT_MODE", "").strip()
    if raw:
        return _normalize_mode(raw)
    raw = _read_admin_settings_field("cosyvoice_offline_endpoint_mode")
    if raw:
        return _normalize_mode(raw)
    return DEFAULT_OFFLINE_MODE


def set_runtime_endpoint_mode(mode: str) -> None:
    """Persist the runtime endpoint mode to admin_settings.json."""
    _write_admin_settings_field("cosyvoice_runtime_endpoint_mode", _normalize_mode(mode))


def set_offline_endpoint_mode(mode: str) -> None:
    """Persist the offline endpoint mode to admin_settings.json."""
    _write_admin_settings_field("cosyvoice_offline_endpoint_mode", _normalize_mode(mode))


def get_ws_url(mode: str) -> str:
    """Get the WebSocket URL for the given endpoint mode."""
    if _normalize_mode(mode) == "international":
        return INTL_WS_URL
    return MAINLAND_WS_URL


def get_api_key_for_mode(mode: str) -> str:
    """Get the DashScope API key for the given endpoint mode.

    Priority: mode-specific key > generic DASHSCOPE_API_KEY.
    Returns empty string if no key found.
    """
    normalized = _normalize_mode(mode)
    if normalized == "international":
        key = os.environ.get("DASHSCOPE_INTERNATIONAL_API_KEY", "").strip()
    else:
        key = os.environ.get("DASHSCOPE_MAINLAND_API_KEY", "").strip()
    if not key:
        key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    return key


def is_voice_available(voice_id: str, mode: str) -> bool:
    """Check if a voice is available on the given endpoint.

    Mainland supports all matchable voices.
    International only supports INTL_AVAILABLE_VOICES.
    """
    if _normalize_mode(mode) == "mainland":
        return True
    return voice_id in INTL_AVAILABLE_VOICES
