from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from threading import Lock

from pricing_schema import PricingPayload, build_default_pricing_payload

logger = logging.getLogger(__name__)

# Default path inside the Docker container config bind-mount
PRICING_RUNTIME_FILE: Path = Path(
    os.environ.get("AIVIDEOTRANS_CONFIG_DIR", "/opt/aivideotrans/config")
) / "pricing_runtime.json"

_cache: PricingPayload | None = None
_cache_mtime_ns: int | None = None
_cache_lock = Lock()


def _file_mtime_ns_or_none() -> int | None:
    """Return file mtime in nanoseconds, or None if file is missing.

    P1-11d (audit 2026-05-07, D-HIGH-6): used as the cross-process
    cache key — when admin process A writes a new snapshot, processes
    B/C will see a different mtime on their next get_runtime_pricing()
    call and re-read instead of serving stale cached data.
    """
    try:
        return os.stat(PRICING_RUNTIME_FILE).st_mtime_ns
    except (FileNotFoundError, OSError):
        return None


def get_runtime_pricing(force_reload: bool = False) -> PricingPayload:
    """Return the current active pricing payload (sync, no DB).

    Reads from PRICING_RUNTIME_FILE if it exists, otherwise falls back
    to build_default_pricing_payload(). Result is cached in-process.

    P1-11d (audit 2026-05-07, D-HIGH-6): the previous in-process cache
    was opaque to other workers — admin publishing a new price in
    process A left B/C serving stale prices until their process
    restarted. We now stat the file on every call and re-read on
    mtime change. Stat is a cheap syscall (~1us local fs) so we
    avoid the rare-but-real stale-price bug at minimal cost.
    """
    global _cache, _cache_mtime_ns
    current_mtime = _file_mtime_ns_or_none()
    if (
        _cache is not None
        and not force_reload
        and current_mtime == _cache_mtime_ns
    ):
        return _cache
    with _cache_lock:
        # Re-check inside lock to avoid duplicate reads under contention.
        current_mtime = _file_mtime_ns_or_none()
        if (
            _cache is not None
            and not force_reload
            and current_mtime == _cache_mtime_ns
        ):
            return _cache
        _cache = _load_from_file()
        _cache_mtime_ns = current_mtime
        return _cache


def write_runtime_snapshot(payload: PricingPayload) -> None:
    """Write the active pricing payload to the runtime file and invalidate cache."""
    global _cache, _cache_mtime_ns
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
            # Refresh cached mtime to match the file we just wrote, so
            # the in-process cache stays consistent with cross-process
            # mtime invalidation in get_runtime_pricing().
            _cache_mtime_ns = _file_mtime_ns_or_none()
        logger.info("[pricing] Runtime snapshot written: %s", PRICING_RUNTIME_FILE)
    except Exception:
        logger.exception("[pricing] Failed to write runtime snapshot")
        raise


def invalidate_runtime_pricing_cache() -> None:
    """Clear in-process cache so next get_runtime_pricing() re-reads from file.

    Cross-process invalidation works automatically via mtime in
    get_runtime_pricing(); this is mainly used by tests and explicit
    admin-side cache flushes.
    """
    global _cache, _cache_mtime_ns
    with _cache_lock:
        _cache = None
        _cache_mtime_ns = None


def _load_from_file() -> PricingPayload:
    """Load from file, fallback to defaults on any error."""
    if PRICING_RUNTIME_FILE.exists():
        try:
            raw = json.loads(PRICING_RUNTIME_FILE.read_text(encoding="utf-8"))
            return PricingPayload.model_validate(raw)
        except Exception:
            logger.exception("[pricing] Failed to parse runtime file, using defaults")
    return build_default_pricing_payload()
