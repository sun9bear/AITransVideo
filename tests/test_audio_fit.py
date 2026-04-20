"""Tests for ``src/utils/audio_fit.fit_audio_to_slot``.

The 2026-04-20 redesign replaces "single atempo to exact target" with
a three-stage policy:
  smart silence trim → clamped atempo stretch → pad/truncate to slot

Each stage has its own invariants; this file pins them down.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydub import AudioSegment
from pydub.generators import Sine

from utils.audio_fit import FitPolicy, fit_audio_to_slot


_HAS_FFMPEG = shutil.which("ffmpeg") is not None
pytestmark = pytest.mark.skipif(
    not _HAS_FFMPEG,
    reason="ffmpeg required in PATH for atempo stretch",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_silent_wav(path: Path, duration_ms: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    AudioSegment.silent(duration=duration_ms, frame_rate=16_000).export(
        path, format="wav",
    )
    return path


def _make_tone_wav(
    path: Path, duration_ms: int, *, freq: int = 440, gain_db: float = -10.0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    (Sine(freq).to_audio_segment(duration=duration_ms).apply_gain(gain_db)
        .export(path, format="wav"))
    return path


def _make_padded_wav(
    path: Path,
    *,
    leading_silence_ms: int,
    tone_ms: int,
    trailing_silence_ms: int,
    freq: int = 440,
    gain_db: float = -10.0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tone = Sine(freq).to_audio_segment(duration=tone_ms).apply_gain(gain_db)
    combined = (
        AudioSegment.silent(duration=leading_silence_ms)
        + tone
        + AudioSegment.silent(duration=trailing_silence_ms)
    )
    combined.export(path, format="wav")
    return path


def _duration_ms(path: Path) -> int:
    return len(AudioSegment.from_wav(path))


# ---------------------------------------------------------------------------
# Stage 1 — smart silence trim
# ---------------------------------------------------------------------------


def test_smart_trim_fires_when_helpful(tmp_path: Path) -> None:
    """Audio 1300ms (150 sil + 1000 tone + 150 sil) → slot 800ms.
    Trimmed version = 1000ms, abs(1000-800)=200 < abs(1300-800)=500.
    Trim helps → should fire. After fit, final is 800ms (clamped
    stretch 1.25x → 800ms exactly)."""
    wav = _make_padded_wav(
        tmp_path / "padded.wav",
        leading_silence_ms=150, tone_ms=1000, trailing_silence_ms=150,
    )

    result = fit_audio_to_slot(wav, slot_duration_ms=800)

    assert result is not None
    assert result.initial_duration_ms == 1300
    assert result.trimmed_duration_ms == 1000, (
        f"smart trim should reduce to ~1000ms, got {result.trimmed_duration_ms}"
    )
    # Final should land on slot (possibly ±small atempo imprecision).
    assert abs(result.final_duration_ms - 800) <= 30
    # Verify leading edge is audible (silence was trimmed).
    out = AudioSegment.from_wav(wav)
    assert out[:20].dBFS > -40, "leading silence still present — trim skipped"


def test_smart_trim_skips_when_it_would_hurt(tmp_path: Path) -> None:
    """Short TTS-like audio: 200ms tone + 50ms silence each side = 300ms.
    Slot = 1000ms. abs(300-1000)=700. Trimmed=200ms, abs(200-1000)=800.
    Trim moves FURTHER from slot → should skip.

    Without this guard, the old unconditional trim would produce 200ms,
    and atempo 0.2x (clamped to 0.8x) would yield 250ms + 750ms silence
    pad. WITH the guard, we start at 300ms, atempo to 0.8x clamp →
    375ms + 625ms silence pad. Either way final = 1000ms, but the
    content audibility in the "pre-pad" portion differs.

    The specific assertion: trimmed_duration_ms == initial (not 200),
    proving the smart-trim guard short-circuited the trim step.
    """
    wav = _make_padded_wav(
        tmp_path / "shortish.wav",
        leading_silence_ms=50, tone_ms=200, trailing_silence_ms=50,
    )

    result = fit_audio_to_slot(wav, slot_duration_ms=1000)

    assert result is not None
    assert result.trimmed_duration_ms == result.initial_duration_ms, (
        f"smart trim should SKIP when it would move away from slot; "
        f"initial={result.initial_duration_ms} "
        f"trimmed={result.trimmed_duration_ms}"
    )


def test_smart_trim_skips_for_long_segments(tmp_path: Path) -> None:
    """Gate: audio > _SILENCE_TRIM_MAX_MS (3s) doesn't trim regardless.
    Rationale: long segments may contain intentional pauses, not pure
    TTS padding. 4000ms audio → slot 1000ms."""
    wav = _make_padded_wav(
        tmp_path / "long.wav",
        leading_silence_ms=200, tone_ms=3600, trailing_silence_ms=200,
    )

    result = fit_audio_to_slot(wav, slot_duration_ms=1000)

    assert result is not None
    assert result.trimmed_duration_ms == result.initial_duration_ms


# ---------------------------------------------------------------------------
# Stage 2 — clamped atempo stretch
# ---------------------------------------------------------------------------


def test_clamped_atempo_floor_for_very_short_audio(tmp_path: Path) -> None:
    """Audio 200ms, slot 1000ms → raw ratio 0.2x. Clamped to
    atempo_min=0.8x. After stretch: 200/0.8 = 250ms. Then silence pad
    fills the rest (750ms) → total 1000ms."""
    wav = _make_tone_wav(tmp_path / "short.wav", duration_ms=200)

    result = fit_audio_to_slot(wav, slot_duration_ms=1000)

    assert result is not None
    assert result.speed_ratio_used == pytest.approx(0.8), (
        f"clamp floor not applied; ratio={result.speed_ratio_used}"
    )
    # stretched_duration should be ~250ms (200/0.8)
    assert 220 <= result.stretched_duration_ms <= 280
    assert result.silence_padded_ms > 600, (
        f"silence pad should fill gap to slot, got "
        f"{result.silence_padded_ms}ms"
    )
    assert result.final_duration_ms == 1000
    assert _duration_ms(wav) == pytest.approx(1000, abs=5)


def test_clamped_atempo_ceiling_for_very_long_audio(tmp_path: Path) -> None:
    """Audio 3000ms, slot 500ms → raw ratio 6x. Clamped to
    atempo_max=1.5x. After stretch: 3000/1.5 = 2000ms. Truncate to
    slot → 500ms. (We prefer truncation over extreme atempo to keep
    audio natural.)"""
    wav = _make_tone_wav(tmp_path / "long.wav", duration_ms=3000)

    result = fit_audio_to_slot(wav, slot_duration_ms=500)

    assert result is not None
    assert result.speed_ratio_used == pytest.approx(1.5)
    assert result.truncated_ms > 0
    assert result.final_duration_ms == 500


def test_moderate_ratio_uses_uncapped_atempo(tmp_path: Path) -> None:
    """Audio 900ms, slot 1000ms → ratio 0.9x (within [0.8, 1.5]). Plain
    atempo, no silence pad, no truncate."""
    wav = _make_tone_wav(tmp_path / "mod.wav", duration_ms=900)

    result = fit_audio_to_slot(wav, slot_duration_ms=1000)

    assert result is not None
    assert result.speed_ratio_used == pytest.approx(0.9, abs=0.01)
    # Small residual gap fills with silence (atempo imprecision)
    assert result.silence_padded_ms <= 50
    assert result.final_duration_ms == pytest.approx(1000, abs=5)


# ---------------------------------------------------------------------------
# Stage 3 — pad / truncate
# ---------------------------------------------------------------------------


def test_silence_pad_is_real_silence_not_noise(tmp_path: Path) -> None:
    """When the clamp floor leaves extra space, the pad must be true
    silence (dBFS = -inf), not e.g. repeated audio or noise."""
    wav = _make_tone_wav(tmp_path / "short.wav", duration_ms=200)

    fit_audio_to_slot(wav, slot_duration_ms=1000)

    result_audio = AudioSegment.from_wav(wav)
    # Tail 400ms should be pure silence (the pad region)
    tail_dbfs = result_audio[-400:].dBFS
    assert tail_dbfs < -60, (
        f"pad tail not silent enough: dBFS={tail_dbfs}"
    )


def test_truncate_preserves_beginning(tmp_path: Path) -> None:
    """When audio is longer than slot even after clamp, truncation
    must keep the start (user hears the beginning of speech, loses
    the tail). Alternative of dropping the start would sound worse."""
    # Tone at 440Hz will have non-silent dBFS; truncating tail keeps tone.
    wav = _make_tone_wav(tmp_path / "long.wav", duration_ms=3000)

    fit_audio_to_slot(wav, slot_duration_ms=500)

    result_audio = AudioSegment.from_wav(wav)
    # First 100ms should still be audible tone
    head_dbfs = result_audio[:100].dBFS
    assert head_dbfs > -40, (
        f"beginning of audio lost after truncation: dBFS={head_dbfs}"
    )


# ---------------------------------------------------------------------------
# Noop / edge cases
# ---------------------------------------------------------------------------


def test_noop_within_tolerance(tmp_path: Path) -> None:
    """Within ±10ms, the file is left bit-identical (no re-encode)."""
    wav = _make_silent_wav(tmp_path / "exact.wav", duration_ms=1000)
    before_bytes = wav.read_bytes()

    result = fit_audio_to_slot(wav, slot_duration_ms=1000)

    assert result is not None
    assert result.speed_ratio_used == 1.0
    assert result.silence_padded_ms == 0
    assert result.truncated_ms == 0
    assert wav.read_bytes() == before_bytes, (
        "within-tolerance fit rewrote the file unnecessarily"
    )


def test_returns_none_when_slot_zero(tmp_path: Path) -> None:
    wav = _make_silent_wav(tmp_path / "x.wav", duration_ms=500)
    assert fit_audio_to_slot(wav, slot_duration_ms=0) is None


def test_returns_none_when_wav_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.wav"
    assert fit_audio_to_slot(missing, slot_duration_ms=1000) is None


# ---------------------------------------------------------------------------
# The segment-103-style case from production (user's real 0.12x ratio)
# ---------------------------------------------------------------------------


def test_segment_103_scenario_pads_with_silence_not_slowmo(
    tmp_path: Path,
) -> None:
    """Real production case: 208ms user-regenerated TTS placed in a
    1720ms slot (ratio 0.12x). Old code atempo-stretched to chipmunk
    slow-mo. New code clamps to 0.8x → 260ms speech + 1460ms silence
    pad. Natural listening experience: short utterance + pause.
    """
    wav = _make_tone_wav(tmp_path / "s103.wav", duration_ms=208)

    result = fit_audio_to_slot(wav, slot_duration_ms=1720)

    assert result is not None
    assert result.speed_ratio_used == pytest.approx(0.8)
    # Stretched ≈ 260ms, pad ≈ 1460ms
    assert 240 <= result.stretched_duration_ms <= 280
    assert result.silence_padded_ms >= 1400
    assert result.final_duration_ms == 1720

    # Listenability: first ~250ms is audio, the rest is silence
    final = AudioSegment.from_wav(wav)
    head_dbfs = final[:200].dBFS
    tail_dbfs = final[-1000:].dBFS
    assert head_dbfs > -40, f"speech lost: head_dbfs={head_dbfs}"
    assert tail_dbfs < -60, f"tail not silent: tail_dbfs={tail_dbfs}"
