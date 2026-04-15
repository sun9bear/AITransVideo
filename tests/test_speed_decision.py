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
    _SPEED_MAX_AGGRESSIVE,
    _SPEED_MAX_DEFAULT,
    _SPEED_MIN_AGGRESSIVE,
    _SPEED_MIN_DEFAULT,
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
    """estimated > target by >8% → out of default clamp → speed=1.0."""
    out = decide_tts_speed(
        cn_text=_TEXT_100_HANZI,         # estimated 25 000 ms
        target_duration_ms=20_000,        # ratio = 1.25 → desired 1.25 > 1.08
        chars_per_second=_CPS,
        enabled=True,
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
