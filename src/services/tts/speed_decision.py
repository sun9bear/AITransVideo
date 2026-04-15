"""TTS speed parameter decision logic — Phase 2 Task 1.

Decides the per-segment ``speed`` parameter to send to the TTS provider
(currently MiniMax only; CosyVoice/VolcEngine plumbing is a separate item).

Core idea: we already estimate how long the TTS output will be from the
text length divided by the calibrated chars-per-second of the chosen
voice. When the estimate diverges from the segment's target duration,
we can ask the provider to speak faster or slower instead of triggering
an expensive S5 rewrite — but only within a hard clamp (±8% default,
±15% aggressive) where listening quality stays acceptable.

Design constraints (CodeX two-round review):
- Spoken-char counting MUST go through ``TTSDurationEstimator`` so the
  estimate uses the same ``_NON_SPOKEN_CHAR_PATTERN`` as the rewriter
  and post-TTS calibration.  Mixing raw ``len(text)`` with that estimator
  was the P2 fix from the second review.
- Hard clamp, never extrapolate beyond [SPEED_MIN, SPEED_MAX].  Outside
  that band, return ``speed=1.0`` and let the caller fall back to
  rewrite/DSP — speed 0.85 / 1.15 is the absolute auditory edge.
- Decision is pure / deterministic: same inputs → same speed.  This
  module owns *no* I/O and is trivially unit-testable.

Default mode (admin_settings.tts_speed_mode == "default") clamps to
[0.92, 1.08] (±8%).  Aggressive mode clamps to [0.85, 1.15] (±15%).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.tts.duration_estimator import TTSDurationEstimator


# --- Constants ---------------------------------------------------------------

# A small zero-band around 1.0 where DSP can soak the residual error and
# we don't bother sending speed != 1.0.  Keeps the audio path stable for
# the majority of segments where the estimate was on target.
_NEUTRAL_BAND: Final[float] = 0.05  # ±5%

# Default mode clamp: empirically the safe edge where Mandarin TTS still
# sounds natural.  Aggressive / extreme / unlimited modes trade quality
# for alignment power; admins can opt in for special projects.
_SPEED_MIN_DEFAULT: Final[float] = 0.92
_SPEED_MAX_DEFAULT: Final[float] = 1.08
_SPEED_MIN_AGGRESSIVE: Final[float] = 0.85
_SPEED_MAX_AGGRESSIVE: Final[float] = 1.15
# Extreme: ±30%. Audibly fast/slow but still recognisable. Use for
# experimental data on tough video types where ±15% leaves too many
# segments unhandled (typical LLM-translation overshoot scenarios).
_SPEED_MIN_EXTREME: Final[float] = 0.70
_SPEED_MAX_EXTREME: Final[float] = 1.30
# Unlimited: clamps only at the MiniMax provider's API hard limits
# (0.5x–2.0x). Definite quality degradation at the edges; only useful
# for telemetry — to see how many segments would land if speed had no
# upper bound. Not recommended for production output.
_SPEED_MIN_UNLIMITED: Final[float] = 0.50
_SPEED_MAX_UNLIMITED: Final[float] = 2.00

# Where the gateway writes its admin_settings.json on the production host.
# Process.py uses the same path; we intentionally read directly rather than
# round-trip to the gateway HTTP API to keep the TTS hot path latency-free.
_ADMIN_SETTINGS_PATH = Path("/opt/aivideotrans/config/admin_settings.json")


# --- Data classes ------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class SpeedDecision:
    """Result of a speed decision for one segment.

    Attributes
    ----------
    speed:
        The value to send to the TTS provider (MiniMax voice_setting.speed).
        Always within [SPEED_MIN, SPEED_MAX] of the active mode, OR 1.0
        if the segment falls into the neutral band or out-of-range fallback.
    reason:
        Short human-readable tag describing which branch was taken; used
        for logging and the admin metric ``speed_param_distribution``.
        One of: "disabled", "neutral", "in_range", "outside_range",
        "missing_inputs".
    estimated_ms:
        The model's predicted TTS duration before any speed adjustment.
        0 when inputs were missing.
    ratio:
        estimated_ms / target_ms (0.0 when inputs were missing).
    """
    speed: float
    reason: str
    estimated_ms: int
    ratio: float


# --- Public API --------------------------------------------------------------

def is_speed_adjustment_enabled() -> bool:
    """Read the admin feature flag.  Defaults to False if the file is
    missing / unreadable / lacks the key — i.e. the safe (off) default
    matches the AdminSettings pydantic default.
    """
    try:
        if _ADMIN_SETTINGS_PATH.exists():
            import json
            with _ADMIN_SETTINGS_PATH.open(encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                return bool(cfg.get("tts_speed_adjustment_enabled", False))
    except Exception:
        pass
    return False


def _get_speed_clamp() -> tuple[float, float]:
    """Return (min, max) speed clamp based on admin tts_speed_mode."""
    mode = "default"
    try:
        if _ADMIN_SETTINGS_PATH.exists():
            import json
            with _ADMIN_SETTINGS_PATH.open(encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                mode = str(cfg.get("tts_speed_mode", "default")).strip().lower()
    except Exception:
        pass

    if mode == "unlimited":
        return _SPEED_MIN_UNLIMITED, _SPEED_MAX_UNLIMITED
    if mode == "extreme":
        return _SPEED_MIN_EXTREME, _SPEED_MAX_EXTREME
    if mode == "aggressive":
        return _SPEED_MIN_AGGRESSIVE, _SPEED_MAX_AGGRESSIVE
    return _SPEED_MIN_DEFAULT, _SPEED_MAX_DEFAULT


def decide_tts_speed(
    *,
    cn_text: str,
    target_duration_ms: int,
    chars_per_second: float | None,
    enabled: bool | None = None,
    speed_clamp: tuple[float, float] | None = None,
) -> SpeedDecision:
    """Compute the per-segment TTS speed.

    All keyword-only so the caller can't accidentally swap arguments.
    ``enabled`` and ``speed_clamp`` are exposed for unit tests; in
    production the helpers above pull them from admin settings.

    Decision tree:
      - feature flag off → speed=1.0, reason="disabled"
      - missing/invalid inputs → speed=1.0, reason="missing_inputs"
      - estimated_ms within ±5% of target → speed=1.0, reason="neutral"
      - estimated_ms recoverable by clamped speed → speed=1/ratio (clamped),
        reason="in_range"
      - estimated_ms beyond what clamp can fix → speed=1.0, reason="outside_range"
        (the caller's existing rewrite/DSP path takes over)

    Returns a ``SpeedDecision`` even in the disabled / fallback paths, so
    the caller can record the reason in ``segment.dsp_speed_param`` and
    aggregate distributions in metering.
    """
    use_enabled = is_speed_adjustment_enabled() if enabled is None else bool(enabled)
    smin, smax = _get_speed_clamp() if speed_clamp is None else speed_clamp

    if not use_enabled:
        return SpeedDecision(speed=1.0, reason="disabled", estimated_ms=0, ratio=0.0)

    if not cn_text or target_duration_ms <= 0 or not chars_per_second or chars_per_second <= 0:
        return SpeedDecision(speed=1.0, reason="missing_inputs", estimated_ms=0, ratio=0.0)

    # Use the same spoken-char counting as rewriter/post-TTS calibration.
    # See Phase 1 v2.1 → CodeX P2 fix: do NOT use raw len(cn_text).
    estimator = TTSDurationEstimator(chars_per_second=float(chars_per_second))
    estimated_ms = estimator.estimate_duration_ms(cn_text)
    if estimated_ms <= 0:
        return SpeedDecision(speed=1.0, reason="missing_inputs", estimated_ms=0, ratio=0.0)

    ratio = estimated_ms / target_duration_ms

    # Neutral band: tiny errors, let DSP / direct copy handle it.
    if abs(ratio - 1.0) <= _NEUTRAL_BAND:
        return SpeedDecision(speed=1.0, reason="neutral",
                             estimated_ms=estimated_ms, ratio=ratio)

    # Speed needed to make ``estimated_ms / speed`` = ``target_ms``.
    # If estimated > target, ratio > 1, we want speed > 1 (read faster).
    desired_speed = ratio  # since: estimated/desired = target → desired = estimated/target

    # Check whether the desired speed is reachable inside the clamp.
    # If the desired speed sits OUTSIDE [smin, smax], a clamped speed
    # would still leave the segment over/under target → not worth doing.
    if desired_speed < smin or desired_speed > smax:
        return SpeedDecision(speed=1.0, reason="outside_range",
                             estimated_ms=estimated_ms, ratio=ratio)

    # Clamp (defensive — already verified above, but explicit for clarity).
    speed = max(smin, min(smax, desired_speed))
    # Round to 4 decimals so the wire payload + log is readable.
    speed = round(speed, 4)
    return SpeedDecision(speed=speed, reason="in_range",
                         estimated_ms=estimated_ms, ratio=ratio)


# --- VolcEngine speech_rate mapping -----------------------------------------
#
# VolcEngine V3 ``audio_params.speech_rate`` is an integer in [-50, 100]:
# positive values speed up (shorter audio), negative slow down. Empirical
# validation on seed-tts-1.0 / seed-tts-2.0 (2026-04-15, see
# ``scripts/test_volcengine_speech_rate.py`` + the Phase 2 handoff doc)
# showed audio duration tracks ``1 / (1 + speech_rate/100)`` within
# |err| < 5% — so a MiniMax-style speed multiplier maps cleanly to:
#
#     speech_rate = int(round((speed - 1) * 100))
#
# Examples: speed=1.15 -> +15, speed=0.85 -> -15, speed=1.30 -> +30.
# Unlimited-mode speeds (0.5..2.0) saturate at VolcEngine's API envelope.
_VOLCENGINE_SPEECH_RATE_MIN: Final[int] = -50
_VOLCENGINE_SPEECH_RATE_MAX: Final[int] = 100


def speed_to_volcengine_speech_rate(speed: float) -> int:
    """Map a MiniMax-style speed multiplier to VolcEngine's speech_rate.

    Returns 0 for speed=1.0 (baseline) and clamps to VolcEngine's
    advertised envelope [-50, 100] so the provider never rejects
    out-of-range values even under ``tts_speed_mode=unlimited``.

    Invalid / non-numeric input yields 0 (safe no-op).
    """
    try:
        raw = int(round((float(speed) - 1.0) * 100))
    except (TypeError, ValueError):
        return 0
    return max(_VOLCENGINE_SPEECH_RATE_MIN, min(_VOLCENGINE_SPEECH_RATE_MAX, raw))
