"""Admin-editable support settings persistence.

The full admin_settings.json is owned by ``gateway/admin_settings.py``;
this module reads/writes a dedicated ``support`` sub-key inside the same
JSON file so all admin config stays in one place. Plan §7.2 — admin UI
limited to ~10 fields, so the JSON shape mirrors
``support_models.SupportAdminSettings``.

Read path is hot — every support message resolves it. We use the same
TTL cache pattern ``llm_registry`` uses (5 s) so admin saves propagate
quickly while we keep cold paths cheap.

Writes go through ``services._file_lock.file_lock`` and
``utils.atomic_io.atomic_write_json`` to match the rest of the admin
config story (P0-5 audit fix).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from support_models import SupportAdminSettings

logger = logging.getLogger(__name__)

# Make src/ importable so we can reuse file_lock + atomic_write_json.
for _candidate in [
    Path(__file__).resolve().parent.parent / "src",
    Path("/opt/aivideotrans/app/src"),
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

# Imports after sys.path manipulation; mypy/ruff would complain otherwise.
from services._file_lock import file_lock  # noqa: E402
from utils.atomic_io import atomic_write_json  # noqa: E402

SETTINGS_FILE = Path(
    os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config")
) / "admin_settings.json"

_SUPPORT_KEY = "support"
_CACHE_TTL = 5.0  # seconds

_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0


def _read_admin_json() -> dict[str, Any]:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to parse %s for support settings", SETTINGS_FILE)
    return {}


def load_support_settings(force_reload: bool = False) -> dict[str, Any]:
    """Return the merged support config (admin overrides + env defaults).

    Cached for 5 s so the support flow is not re-reading the file on every
    incoming message.
    """
    global _cache, _cache_ts
    now = time.monotonic()
    if not force_reload and _cache is not None and (now - _cache_ts) < _CACHE_TTL:
        return dict(_cache)

    raw = _read_admin_json()
    overrides = raw.get(_SUPPORT_KEY, {}) if isinstance(raw, dict) else {}
    if not isinstance(overrides, dict):
        overrides = {}

    # Defaults derived from env. These match the values in `.env.example` /
    # `docker-compose.yml`. Admin overrides win over env; missing values
    # fall back to env.
    base = {
        # Codex P2-1 (2026-05-08): default both flags off so a fresh
        # production deploy does not surface the support widget until an
        # operator explicitly opts in via env or the admin page.
        "support_enabled": _truthy(os.environ.get("AVT_SUPPORT_ENABLED"), default=False),
        "support_anonymous_enabled": _truthy(
            os.environ.get("AVT_SUPPORT_ANONYMOUS_ENABLED"), default=False
        ),
        "support_ai_enabled": _truthy(os.environ.get("AVT_SUPPORT_AI_ENABLED"), default=False),
        "support_ai_provider": (
            os.environ.get("AVT_SUPPORT_AI_PROVIDER", "fake") or "fake"
        ).strip().lower(),
        "support_ai_model": (
            os.environ.get("AVT_SUPPORT_AI_MODEL", "deepseek") or "deepseek"
        ).strip(),
        "support_ai_max_output_tokens": int(
            os.environ.get("AVT_SUPPORT_AI_MAX_OUTPUT_TOKENS", "400") or 400
        ),
        "support_ai_max_input_chars": int(
            os.environ.get("AVT_SUPPORT_AI_MAX_INPUT_CHARS", "2000") or 2000
        ),
        "support_ai_timeout_seconds": float(
            os.environ.get("AVT_SUPPORT_AI_TIMEOUT_SECONDS", "15") or 15
        ),
        "support_ai_monthly_budget_usd": float(
            os.environ.get("AVT_SUPPORT_AI_MONTHLY_BUDGET_USD", "50") or 50
        ),
        "support_ai_input_usd_per_1m_tokens": float(
            os.environ.get("AVT_SUPPORT_AI_INPUT_USD_PER_1M_TOKENS", "0.14") or 0.14
        ),
        "support_ai_output_usd_per_1m_tokens": float(
            os.environ.get("AVT_SUPPORT_AI_OUTPUT_USD_PER_1M_TOKENS", "0.28") or 0.28
        ),
        "support_handoff_provider": (
            os.environ.get("AVT_SUPPORT_HANDOFF_PROVIDER", "email") or "email"
        ).strip().lower(),
        "support_ops_email": (
            os.environ.get("AVT_SUPPORT_OPS_EMAIL", "sxz999@proton.me")
            or "sxz999@proton.me"
        ).strip(),
        "support_budget_exhausted_message": (
            "AI 客服当前繁忙，你可以先查看常见问题，或转人工客服处理。"
        ),
        "support_sensitive_keywords": [
            "人工",
            "真人",
            "转客服",
            "找人",
            "退款",
            "投诉",
            "差评",
            "工信部",
            "315",
            "赔偿",
            "举报",
            "律师",
            "消协",
        ],
        # Human handoff routing (L1, plan 2026-05-08 follow-up)
        "support_admin_heartbeat_interval_seconds": int(
            os.environ.get("AVT_SUPPORT_ADMIN_HEARTBEAT_INTERVAL_SECONDS", "30") or 30
        ),
        "support_admin_online_threshold_seconds": int(
            os.environ.get("AVT_SUPPORT_ADMIN_ONLINE_THRESHOLD_SECONDS", "60") or 60
        ),
        "support_handoff_offline_fallback_minutes": int(
            os.environ.get("AVT_SUPPORT_HANDOFF_OFFLINE_FALLBACK_MINUTES", "5") or 5
        ),
        "support_offline_message": (
            "运营暂未在线，可扫码添加客服微信，我们尽快回复。"
        ),
    }
    merged = {**base, **overrides}
    _cache = merged
    _cache_ts = now
    return dict(merged)


def save_support_settings(model: SupportAdminSettings) -> None:
    """Persist the admin-editable subset to admin_settings.json.

    ``file_lock`` expects a ``Path`` and appends its own ``.lock`` suffix
    via ``path.with_suffix``; ``atomic_write_json`` expects a string
    target path. Honor both signatures verbatim — passing a str into
    file_lock or appending ``.lock`` ourselves was the 2026-05-08 prod
    500 (caught the moment an admin clicked Save on /admin/support).
    """
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(SETTINGS_FILE):
        existing = _read_admin_json() if SETTINGS_FILE.exists() else {}
        if not isinstance(existing, dict):
            existing = {}
        merged_support = existing.get(_SUPPORT_KEY, {}) or {}
        if not isinstance(merged_support, dict):
            merged_support = {}
        merged_support.update(model.model_dump())
        existing[_SUPPORT_KEY] = merged_support
        atomic_write_json(str(SETTINGS_FILE), existing)
    invalidate_cache()


def invalidate_cache() -> None:
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0.0


def _truthy(raw: str | None, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
