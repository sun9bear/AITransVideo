"""Unit tests for ``services.tts.speed_decision``.

Covers:
- feature flag off → speed=1.0 / reason=disabled
- missing/invalid inputs → speed=1.0 / reason=missing_inputs
- neutral band (±5%) → speed=1.0 / reason=neutral
- in-range adjustment → clamped speed / reason=in_range
- outside-range fallback → speed=1.0 / reason=outside_range
- aggressive clamp wider than default
- spoken-char counting (P2 fix): punctuation does not skew the estimate
"""

from __future__ import annotations

import pytest

from services.tts.speed_decision import (
    SpeedDecision,
    decide_tts_speed,
    speed_to_volcengine_speech_rate,
    _SPEED_MAX_AGGRESSIVE,
    _SPEED_MAX_DEFAULT,
    _SPEED_MIN_AGGRESSIVE,
    _SPEED_MIN_DEFAULT,
    _VOLCENGINE_SPEECH_RATE_MAX,
    _VOLCENGINE_SPEECH_RATE_MIN,
)


# Helpful constants for the tests below.  When chars_per_second=4.0,
# 100 hanzi take exactly 25 000 ms.
_CPS = 4.0
_TEXT_100_HANZI = "".join(["中"] * 100)


def test_disabled_returns_neutral_speed():
    """Explicit `enabled=False` short-circuits with speed=1.0 + reason=disabled."""
    out = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,
        target_duration_ms=20_000,    # would normally need speed > 1
        chars_per_second=_CPS,
        enabled=False,
    )
    assert out.speed == 1.0
    assert out.reason == "disabled"
    assert out.estimated_ms == 0  # short-circuited before estimation


def test_missing_text_returns_missing_inputs():
    out = decide_tts_speed(
        cn_text="",
        target_duration_ms=10_000,
        chars_per_second=_CPS,
        enabled=True,
    )
    assert out.speed == 1.0
    assert out.reason == "missing_inputs"


def test_zero_target_duration_returns_missing_inputs():
    out = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,
        target_duration_ms=0,
        chars_per_second=_CPS,
        enabled=True,
    )
    assert out.reason == "missing_inputs"


def test_zero_chars_per_second_returns_missing_inputs():
    out = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,
        target_duration_ms=20_000,
        chars_per_second=0.0,
        enabled=True,
    )
    assert out.reason == "missing_inputs"


def test_perfect_match_is_neutral():
    """estimated_ms == target_ms → ratio=1.0, neutral band."""
    out = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,         # 100 hanzi at 4.0 cps → 25 000 ms
        target_duration_ms=25_000,
        chars_per_second=_CPS,
        enabled=True,
    )
    assert out.reason == "neutral"
    assert out.speed == 1.0
    assert out.estimated_ms == 25_000
    assert abs(out.ratio - 1.0) < 1e-9


def test_within_neutral_band_no_speed_change():
    """ratio between 0.95 and 1.05 → still neutral."""
    out = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,         # estimated 25 000 ms
        target_duration_ms=24_000,        # ratio ≈ 1.04
        chars_per_second=_CPS,
        enabled=True,
    )
    assert out.reason == "neutral"
    assert out.speed == 1.0


def test_in_range_speed_up_for_overshoot():
    """estimated > target by ~7% → desired speed ~1.07, in default clamp."""
    out = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,         # estimated 25 000 ms
        target_duration_ms=23_400,        # ratio ≈ 1.068
        chars_per_second=_CPS,
        enabled=True,
    )
    assert out.reason == "in_range"
    # speed should equal estimated/target (rounded), within clamp.
    assert _SPEED_MIN_DEFAULT <= out.speed <= _SPEED_MAX_DEFAULT
    assert out.speed == round(25_000 / 23_400, 4)


def test_in_range_slow_down_for_undershoot():
    """estimated < target by ~6% → desired speed ~0.94, in default clamp."""
    out = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,         # estimated 25 000 ms
        target_duration_ms=26_600,        # ratio ≈ 0.939
        chars_per_second=_CPS,
        enabled=True,
    )
    assert out.reason == "in_range"
    assert _SPEED_MIN_DEFAULT <= out.speed <= _SPEED_MAX_DEFAULT
    assert out.speed == round(25_000 / 26_600, 4)


def test_outside_range_overshoot_falls_back():
    """estimated > target by >8% → out of default clamp → speed=1.0.

    Explicit speed_clamp isolates this test from the live admin_settings.json
    (which may be set to aggressive/extreme/unlimited on real hosts and would
    otherwise make ratio=1.25 fall inside the clamp).
    """
    out = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,         # estimated 25 000 ms
        target_duration_ms=20_000,        # ratio = 1.25 → desired 1.25 > 1.08
        chars_per_second=_CPS,
        enabled=True,
        speed_clamp=(_SPEED_MIN_DEFAULT, _SPEED_MAX_DEFAULT),
    )
    assert out.reason == "outside_range"
    assert out.speed == 1.0
    assert out.ratio == 1.25


def test_outside_range_undershoot_falls_back():
    """estimated << target → out of clamp on the slow side → speed=1.0."""
    out = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,         # estimated 25 000 ms
        target_duration_ms=35_000,        # ratio ≈ 0.71 → desired 0.71 < 0.92
        chars_per_second=_CPS,
        enabled=True,
        speed_clamp=(_SPEED_MIN_DEFAULT, _SPEED_MAX_DEFAULT),
    )
    assert out.reason == "outside_range"
    assert out.speed == 1.0


def test_aggressive_mode_widens_range():
    """A 12% overshoot is outside default clamp but inside aggressive."""
    out_default = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,
        target_duration_ms=22_300,        # ratio ≈ 1.121 → desired 1.121
        chars_per_second=_CPS,
        enabled=True,
        speed_clamp=(_SPEED_MIN_DEFAULT, _SPEED_MAX_DEFAULT),
    )
    assert out_default.reason == "outside_range"  # 1.121 > 1.08

    out_aggressive = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,
        target_duration_ms=22_300,
        chars_per_second=_CPS,
        enabled=True,
        speed_clamp=(_SPEED_MIN_AGGRESSIVE, _SPEED_MAX_AGGRESSIVE),
    )
    assert out_aggressive.reason == "in_range"     # 1.121 ≤ 1.15
    assert _SPEED_MIN_AGGRESSIVE <= out_aggressive.speed <= _SPEED_MAX_AGGRESSIVE


def test_punctuation_excluded_from_estimate():
    """Spoken-char count must ignore punctuation (CodeX P2 fix).

    A 100-hanzi text with 50 commas should estimate the same duration as
    the bare 100-hanzi text — both have the same spoken character count.
    """
    bare = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,
        target_duration_ms=24_000,
        chars_per_second=_CPS,
        enabled=True,
    )
    with_punct = decide_tts_speed(
        cn_text=_TEXT_100_HANZI + "，" * 50,
        target_duration_ms=24_000,
        chars_per_second=_CPS,
        enabled=True,
    )
    assert bare.estimated_ms == with_punct.estimated_ms
    # NB: they may differ in branch chosen iff the test is borderline,
    # but at this duration both should land identically.
    assert bare.reason == with_punct.reason
    assert bare.speed == with_punct.speed


def test_speed_decision_dataclass_is_immutable():
    """SpeedDecision is frozen — caller can't accidentally mutate it."""
    out = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,
        target_duration_ms=25_000,
        chars_per_second=_CPS,
        enabled=True,
    )
    with pytest.raises((AttributeError, TypeError)):
        out.speed = 2.0  # type: ignore[misc]


# --- VolcEngine speech_rate mapping -----------------------------------------

def test_speed_to_volcengine_rate_identity_at_one():
    """speed=1.0 is the baseline; maps to 0."""
    assert speed_to_volcengine_speech_rate(1.0) == 0


def test_speed_to_volcengine_rate_positive_values():
    """speed>1 (faster) -> positive speech_rate."""
    assert speed_to_volcengine_speech_rate(1.15) == 15
    assert speed_to_volcengine_speech_rate(1.30) == 30
    assert speed_to_volcengine_speech_rate(1.08) == 8


def test_speed_to_volcengine_rate_negative_values():
    """speed<1 (slower) -> negative speech_rate."""
    assert speed_to_volcengine_speech_rate(0.85) == -15
    assert speed_to_volcengine_speech_rate(0.70) == -30
    assert speed_to_volcengine_speech_rate(0.92) == -8


def test_speed_to_volcengine_rate_rounding():
    """Rounds to the nearest integer speech_rate."""
    # (1.085 - 1) * 100 = 8.5 → banker's rounding picks 8
    # (1.086 - 1) * 100 = 8.6 → rounds to 9
    assert speed_to_volcengine_speech_rate(1.086) == 9
    # (0.925 - 1) * 100 = -7.5 → banker's rounding picks -8
    # (0.924 - 1) * 100 = -7.6 → rounds to -8
    assert speed_to_volcengine_speech_rate(0.924) == -8


def test_speed_to_volcengine_rate_clamps_unlimited_mode():
    """Unlimited mode (0.5..2.0) saturates at VolcEngine's envelope [-50, 100]."""
    # Unlimited mode upper: speed=2.0 → (2.0-1)*100 = 100, fits exactly.
    assert speed_to_volcengine_speech_rate(2.0) == _VOLCENGINE_SPEECH_RATE_MAX
    # Unlimited mode lower: speed=0.5 → (0.5-1)*100 = -50, fits exactly.
    assert speed_to_volcengine_speech_rate(0.5) == _VOLCENGINE_SPEECH_RATE_MIN
    # Further out still clamps.
    assert speed_to_volcengine_speech_rate(3.0) == _VOLCENGINE_SPEECH_RATE_MAX
    # speed=0 maps to raw -100, clamps to -50.
    assert speed_to_volcengine_speech_rate(0.0) == _VOLCENGINE_SPEECH_RATE_MIN


def test_speed_to_volcengine_rate_invalid_input_is_safe():
    """Invalid / non-numeric input must return 0 (safe no-op), not raise."""
    assert speed_to_volcengine_speech_rate(None) == 0  # type: ignore[arg-type]
    assert speed_to_volcengine_speech_rate("fast") == 0  # type: ignore[arg-type]
    assert speed_to_volcengine_speech_rate(float("nan")) in (0, _VOLCENGINE_SPEECH_RATE_MIN, _VOLCENGINE_SPEECH_RATE_MAX)


def test_speed_to_volcengine_rate_matches_empirical_grid():
    """Matches the actual grid tested on us production via
    scripts/test_volcengine_speech_rate.py (2026-04-15). Each MiniMax
    speed multiplier maps to the speech_rate that produced the expected
    duration ratio in real VolcEngine calls.
    """
    # Real grid we validated: speech_rate in [-30, -15, 0, +15, +30]
    grid = [
        (0.70, -30),  # -> duration ~1.41x (seed-2.0) / 1.44x (seed-1.0)
        (0.85, -15),  # -> duration ~1.14x / 1.16x
        (1.00, 0),    # -> baseline
        (1.15, 15),   # -> duration ~0.86x / 0.86x
        (1.30, 30),   # -> duration ~0.73x / 0.76x
    ]
    for speed, expected_rate in grid:
        assert speed_to_volcengine_speech_rate(speed) == expected_rate, (
            f"speed={speed} should map to speech_rate={expected_rate}"
        )
