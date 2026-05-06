from __future__ import annotations

import array
import math
from pathlib import Path
import wave

import pytest

from modules.output.editor.loudness_matcher import (
    LoudnessMatchPolicy,
    LoudnessPair,
    calculate_segment_gain_db,
    calculate_speaker_gains,
    measure_active_loudness_dbfs,
)


def test_speaker_gains_follow_original_speaker_loudness() -> None:
    pairs = [
        LoudnessPair(1, "speaker_a", -36.0, -15.0, 1_000),
        LoudnessPair(2, "speaker_a", -35.0, -14.0, 1_000),
        LoudnessPair(3, "speaker_a", -37.0, -16.0, 1_000),
        LoudnessPair(4, "speaker_b", -32.0, -34.0, 1_000),
        LoudnessPair(5, "speaker_b", -31.0, -33.0, 1_000),
        LoudnessPair(6, "speaker_b", -33.0, -35.0, 1_000),
    ]

    gains = calculate_speaker_gains(pairs)

    assert gains["speaker_a"] == pytest.approx(0.0)
    assert gains["speaker_b"] == pytest.approx(14.0)


def test_speaker_gain_clamps_large_boosts_and_cuts() -> None:
    policy = LoudnessMatchPolicy(min_gain_db=-24.0, max_gain_db=10.0)
    pairs = [
        LoudnessPair(1, "too_loud", -34.0, 5.0, 1_000),
        LoudnessPair(2, "too_quiet", -34.0, -60.0, 1_000),
    ]

    gains = calculate_speaker_gains(pairs, policy=policy)

    assert gains["too_loud"] == pytest.approx(-24.0)
    assert gains["too_quiet"] == pytest.approx(10.0)


def test_segment_residual_is_limited_around_speaker_gain() -> None:
    policy = LoudnessMatchPolicy(max_segment_residual_db=3.0)

    gain = calculate_segment_gain_db(
        source_dbfs=-33.0,
        output_dbfs=-15.0,
        speaker_gain_db=-8.0,
        speaker_source_dbfs=-36.0,
        speaker_target_dbfs=-23.0,
        policy=policy,
    )

    assert gain == pytest.approx(-5.0)


def test_quiet_source_is_lifted_to_comfort_floor() -> None:
    pairs = [
        LoudnessPair(1, "speaker_a", -44.0, -30.0, 1_000),
        LoudnessPair(2, "speaker_a", -45.0, -31.0, 1_000),
        LoudnessPair(3, "speaker_b", -35.0, -20.0, 1_000),
        LoudnessPair(4, "speaker_b", -34.0, -19.0, 1_000),
    ]

    gains = calculate_speaker_gains(pairs)

    assert gains["speaker_a"] == pytest.approx(6.5)
    assert gains["speaker_b"] == pytest.approx(0.0)


def test_comfortable_segment_is_not_adjusted_inside_deadband() -> None:
    gain = calculate_segment_gain_db(
        source_dbfs=-30.0,
        output_dbfs=-18.0,
        speaker_gain_db=0.0,
        speaker_source_dbfs=-32.0,
        speaker_target_dbfs=-18.0,
    )

    assert gain == pytest.approx(0.0)


def test_measure_active_loudness_ignores_silence(tmp_path: Path) -> None:
    wav_path = tmp_path / "tone.wav"
    _write_silence_then_sine(wav_path, silence_ms=500, tone_ms=500, amplitude=0.25)

    dbfs = measure_active_loudness_dbfs(wav_path)

    assert dbfs == pytest.approx(-15.1, abs=1.0)


def _write_silence_then_sine(
    path: Path,
    *,
    silence_ms: int,
    tone_ms: int,
    amplitude: float,
    sample_rate: int = 16_000,
) -> None:
    samples = array.array("h")
    samples.extend([0] * int(sample_rate * silence_ms / 1000))
    tone_frames = int(sample_rate * tone_ms / 1000)
    for index in range(tone_frames):
        phase = 2.0 * math.pi * 440.0 * index / sample_rate
        samples.append(int(math.sin(phase) * amplitude * 32767))

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())
