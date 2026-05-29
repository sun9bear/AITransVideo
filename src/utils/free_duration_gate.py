"""Phase 2a Task 7 (gate #7) — free-tier duration cap decision (FAIL-CLOSED).

Pure decision split out of ``process._check_duration_limit`` so it is
unit-testable without importing the (heavy) pipeline module. The free service
tier's 10-minute cap must hold even when the ffprobe-derived duration cannot be
trusted: an unknown / zero / non-numeric duration (probe failure, corrupt
upload) REJECTS *before* the expensive ASR / LLM / TTS stages — it never
proceeds (the cost gate must close, plan §4.1). Paid modes keep the legacy
lenient plan-based check in the caller (a 0 there means "unknown" and is
allowed).
"""
from __future__ import annotations

import math

# Free service-mode duration cap (minutes). Mirrors
# process._PLAN_MAX_DURATION_MINUTES["free"] and plan_catalog free.max_duration_minutes.
FREE_DURATION_CAP_MINUTES = 10

REJECT_UNTRUSTED = "duration_untrusted"
REJECT_OVER_CAP = "duration_over_cap"


def evaluate_free_duration_cap(
    duration_ms: float | int | None,
    *,
    max_minutes: float = FREE_DURATION_CAP_MINUTES,
) -> str | None:
    """Decide whether a free-tier job must be rejected on duration grounds.

    Returns a rejection reason — ``REJECT_UNTRUSTED`` or ``REJECT_OVER_CAP`` — or
    ``None`` to proceed.

    FAIL-CLOSED: a missing / non-positive / non-finite (NaN / inf) / non-numeric
    ``duration_ms`` returns ``REJECT_UNTRUSTED`` (the cost gate cannot be
    confirmed open, so the job must not enter the paid stages). A ``duration_ms``
    exactly at the cap is allowed (strict ``>`` comparison).
    """
    try:
        ms = float(duration_ms)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return REJECT_UNTRUSTED
    # NaN/inf slip through float() but are not a trusted duration: NaN fails every
    # comparison (would otherwise return None = allow), so reject all non-finite.
    if not math.isfinite(ms) or ms <= 0:
        return REJECT_UNTRUSTED
    if (ms / 60_000.0) > float(max_minutes):
        return REJECT_OVER_CAP
    return None


__all__ = [
    "FREE_DURATION_CAP_MINUTES",
    "REJECT_UNTRUSTED",
    "REJECT_OVER_CAP",
    "evaluate_free_duration_cap",
]
