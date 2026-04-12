from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock

from pricing_schema import PricingPayload, build_default_pricing_payload

logger = logging.getLogger(__name__)

# Default path inside the Docker container config bind-mount
PRICING_RUNTIME_FILE: Path = Path("/opt/aivideotrans/config/pricing_runtime.json")

_cache: PricingPayload | None = None
_cache_lock = Lock()


def get_runtime_pricing(force_reload: bool = False) -> PricingPayload:
    """Return the current active pricing payload (sync, no DB).

    Reads from PRICING_RUNTIME_FILE if it exists, otherwise falls back
    to build_default_pricing_payload(). Result is cached in-process.
    """
    global _cache
    if _cache is not None and not force_reload:
        return _cache
    with _cache_lock:
        if _cache is not None and not force_reload:
            return _cache
        _cache = _load_from_file()
        return _cache


def write_runtime_snapshot(payload: PricingPayload) -> None:
    """Write the active pricing payload to the runtime file and invalidate cache."""
    global _cache
    try:
        PRICING_RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PRICING_RUNTIME_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(PRICING_RUNTIME_FILE)
        with _cache_lock:
            _cache = payload
        logger.info("[pricing] Runtime snapshot written: %s", PRICING_RUNTIME_FILE)
    except Exception:
        logger.exception("[pricing] Failed to write runtime snapshot")
        raise


def invalidate_runtime_pricing_cache() -> None:
    """Clear in-process cache so next get_runtime_pricing() re-reads from file."""
    global _cache
    with _cache_lock:
        _cache = None


def _load_from_file() -> PricingPayload:
    """Load from file, fallback to defaults on any error."""
    if PRICING_RUNTIME_FILE.exists():
        try:
            raw = json.loads(PRICING_RUNTIME_FILE.read_text(encoding="utf-8"))
            return PricingPayload.model_validate(raw)
        except Exception:
            logger.exception("[pricing] Failed to parse runtime file, using defaults")
    return build_default_pricing_payload()
