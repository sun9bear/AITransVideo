"""Tests for gateway/voice_speed_calibrator.calibrate_voice.

The function is intentionally I/O-injectable (synth_fn + duration_fn),
so all of these tests run zero paid API calls and zero ffprobe."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# sys.path: gateway lives outside src/, mirror the existing
# voice_selection test pattern.
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)
_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# database / models import shims (same pattern as test_voice_selection_*)
_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)

from voice_speed_calibrator import (  # noqa: E402
    MAX_VALID_CPS,
    MIN_VALID_CPS,
    CalibrationResult,
    calibrate_voice,
)


# Standard texts have known hanzi counts: T1=101, T2=153, T3=204 (total 458).
# We use those numbers in the assertions below so the tests stay
# deterministic without re-implementing count_hanzi.
T1_HANZI = 101
T2_HANZI = 153
T3_HANZI = 204
TOTAL_HANZI = T1_HANZI + T2_HANZI + T3_HANZI


def _fake_synth(duration_per_call_ms: int):
    """Synth that ignores text and returns a constant token blob.
    The duration_fn below uses the blob's first byte as the ms value."""
    def synth(text, voice_id, model):
        # Encode the duration into the bytes so duration_fn can read it.
        # Use a 4-byte big-endian int so we can carry up to ~71 minutes.
        return duration_per_call_ms.to_bytes(4, "big")
    return synth


def _decode_duration(blob: bytes) -> int:
    return int.from_bytes(blob, "big")


# ----- Happy path -----

def test_returns_correct_cps_for_constant_speed(monkeypatch):
    """All 3 texts at the same TTS speed → cps = total_hanzi / total_seconds."""
    # Pretend each call took exactly the same seconds-per-hanzi so we can
    # compute cps deterministically. We send a fixed 30s for each text;
    # cps = 458 / 90s ≈ 5.089.
    result = calibrate_voice(
        provider="minimax",  # ignored when synth_fn is supplied
        model="speech-2.8-turbo",
        voice_id="vt_test_voice",
        inter_call_sleep_s=0.0,
        synth_fn=_fake_synth(30_000),
        duration_fn=_decode_duration,
    )

    assert result.ok is True
    assert result.error == ""
    assert result.total_hanzi == TOTAL_HANZI
    assert result.total_duration_ms == 90_000
    expected_cps = round(TOTAL_HANZI / 90.0, 4)
    assert result.cps == pytest.approx(expected_cps)
    assert len(result.per_text) == 3


def test_per_text_breakdown_recorded():
    """Each text's hanzi + duration_ms + cps captured in per_text."""
    # 20s per call → per_text cps = hanzi / 20
    result = calibrate_voice(
        provider="minimax",
        model="x",
        voice_id="vt_x",
        inter_call_sleep_s=0.0,
        synth_fn=_fake_synth(20_000),
        duration_fn=_decode_duration,
    )

    assert result.ok
    assert len(result.per_text) == 3
    by_name = {t.name: t for t in result.per_text}
    assert by_name["T1_tech_review"].hanzi == T1_HANZI
    assert by_name["T1_tech_review"].duration_ms == 20_000
    assert by_name["T1_tech_review"].cps == pytest.approx(T1_HANZI / 20.0, abs=0.001)
    assert by_name["T2_documentary"].hanzi == T2_HANZI
    assert by_name["T3_startup_speech"].hanzi == T3_HANZI


# ----- Sanity bounds -----

def test_rejects_cps_below_min_bound():
    """Slow voice (TTS pauses too long) yields cps < 2.0 → ok=False but
    cps + per_text still populated for debugging."""
    # Make each text very slow: 200s per call → cps = 458 / 600 ≈ 0.76
    result = calibrate_voice(
        provider="minimax",
        model="x",
        voice_id="vt_slow",
        inter_call_sleep_s=0.0,
        synth_fn=_fake_synth(200_000),
        duration_fn=_decode_duration,
    )

    assert result.ok is False
    assert "out of sanity range" in result.error
    assert result.cps < MIN_VALID_CPS
    assert len(result.per_text) == 3  # diagnostics preserved


def test_rejects_cps_above_max_bound():
    """Garbage-fast TTS (broken provider) yields cps > 8.0 → reject."""
    # 5s per call → cps = 458 / 15 ≈ 30.5
    result = calibrate_voice(
        provider="minimax",
        model="x",
        voice_id="vt_fast",
        inter_call_sleep_s=0.0,
        synth_fn=_fake_synth(5_000),
        duration_fn=_decode_duration,
    )

    assert result.ok is False
    assert "out of sanity range" in result.error
    assert result.cps > MAX_VALID_CPS


# ----- Failure paths -----

def test_synth_failure_returns_named_error():
    """When synth raises, error message names the failing text so the UI
    can show something actionable like 'failed on T2_documentary'."""
    call_count = {"n": 0}

    def flaky_synth(text, voice_id, model):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("provider 503")
        return (10_000).to_bytes(4, "big")

    result = calibrate_voice(
        provider="minimax",
        model="x",
        voice_id="vt_x",
        inter_call_sleep_s=0.0,
        synth_fn=flaky_synth,
        duration_fn=_decode_duration,
    )

    assert result.ok is False
    assert "synth failed on T2_documentary" in result.error
    assert "provider 503" in result.error
    # First text already succeeded — preserved for diagnostics.
    assert len(result.per_text) == 1
    assert result.per_text[0].name == "T1_tech_review"


def test_duration_zero_returns_error():
    """Provider returned audio but ffprobe couldn't measure it (0 ms) —
    should be treated as a failure, not silently divide by zero."""
    result = calibrate_voice(
        provider="minimax",
        model="x",
        voice_id="vt_x",
        inter_call_sleep_s=0.0,
        synth_fn=_fake_synth(0),
        duration_fn=_decode_duration,
    )

    assert result.ok is False
    assert "non-positive duration" in result.error


def test_duration_fn_failure_returns_error():
    """ffprobe raised — name the failing text."""
    def explode(_blob):
        raise RuntimeError("ffprobe missing")

    result = calibrate_voice(
        provider="minimax",
        model="x",
        voice_id="vt_x",
        inter_call_sleep_s=0.0,
        synth_fn=_fake_synth(20_000),
        duration_fn=explode,
    )

    assert result.ok is False
    assert "duration measurement failed on T1_tech_review" in result.error


def test_unknown_provider_with_no_synth_fn_returns_error():
    """No synth_fn override + unknown provider → fail clean, not crash."""
    result = calibrate_voice(
        provider="azure_speech_or_whatever",
        model="x",
        voice_id="vt_x",
    )

    assert result.ok is False
    assert "unknown provider" in result.error


# ----- API contract -----

def test_result_dataclass_fields_are_serialisable():
    """All fields of CalibrationResult / TextResult need to round-trip
    through the JSON-style dict the API uses to respond."""
    result = calibrate_voice(
        provider="minimax",
        model="speech-2.8-turbo",
        voice_id="vt_x",
        inter_call_sleep_s=0.0,
        synth_fn=_fake_synth(25_000),
        duration_fn=_decode_duration,
    )

    # Mimic the dict the API builds.
    payload = {
        "ok": result.ok,
        "cps": result.cps,
        "total_hanzi": result.total_hanzi,
        "total_duration_ms": result.total_duration_ms,
        "per_text": [
            {"name": t.name, "hanzi": t.hanzi, "duration_ms": t.duration_ms, "cps": t.cps}
            for t in result.per_text
        ],
        "error": result.error,
    }
    assert isinstance(payload["cps"], float)
    assert isinstance(payload["per_text"], list)
    assert all(isinstance(t["cps"], float) for t in payload["per_text"])


def test_inter_call_sleep_is_optional():
    """Setting sleep=0 should not block tests — we don't actually sleep
    between the 3 calls when caller asks for it."""
    import time
    t0 = time.time()
    calibrate_voice(
        provider="minimax",
        model="x",
        voice_id="vt_x",
        inter_call_sleep_s=0.0,
        synth_fn=_fake_synth(10_000),
        duration_fn=_decode_duration,
    )
    elapsed = time.time() - t0
    # Real impl sleeps 2*2s between texts when the default is used.
    # With 0 we should be way under 0.5s.
    assert elapsed < 0.5
