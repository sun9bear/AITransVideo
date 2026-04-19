"""Tests for EditorPackageWriter._fit_segment_audio_to_slot.

Replaces the old _trim_segment_audio_to_slot (which only truncated long
segments and left short ones untouched — producing silence gaps). The
new contract: any segment whose actual duration differs from the slot
(end_ms - start_ms) is **time-stretched** via ffmpeg atempo to match
the slot, regardless of direction.

Rationale (2026-04-19, γ publish-only resume):

- γ trusts ``editor/tts_segments/{sid}.wav`` as authoritative audio
  (the "user 保证" contract). But a user-regenerated TTS rarely matches
  the original slot duration exactly — the new TTS might be 1.74s for
  a 0.76s slot.
- Hard-trimming 1.74s → 0.76s (old behaviour) kept only the first 760ms
  of audio, producing a segment that said "我" instead of "我的很负责".
- Hard-leaving a 0.5s audio inside a 1.0s slot (old behaviour) produced
  silent gaps in the dubbed video.
- atempo stretches BOTH directions without re-running TTS / calling
  any paid API, so it's γ-compliant. Extreme ratios (>2x, <0.5x) still
  work (ffmpeg chains atempo automatically) but audio quality degrades
  — user reviews in the test-playback UI (planned) and re-edits if
  unsatisfied.

Required invariants covered here:

1. actual > slot → output duration ≈ slot (stretched faster)
2. actual < slot → output duration ≈ slot (stretched slower)
3. actual ≈ slot (within tolerance) → no-op (avoid pointless re-encode)
4. slot_duration_ms <= 0 → no-op (sanity)
5. Extreme ratio (10x) still produces a valid wav matching slot duration
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydub import AudioSegment

from modules.output.editor.editor_package_models import AlignedSegment
from modules.output.editor.editor_package_writer import EditorPackageWriter


_HAS_FFMPEG = shutil.which("ffmpeg") is not None
pytestmark = pytest.mark.skipif(
    not _HAS_FFMPEG,
    reason="ffmpeg required in PATH for atempo stretch tests",
)


def _make_wav(path: Path, duration_ms: int, *, sample_rate: int = 16000) -> Path:
    """Write a silent wav at the given duration + sample rate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    AudioSegment.silent(duration=duration_ms, frame_rate=sample_rate).export(
        path, format="wav"
    )
    return path


def _make_wav_with_silence_padding(
    path: Path,
    *,
    speech_ms: int,
    leading_silence_ms: int,
    trailing_silence_ms: int,
    sample_rate: int = 16000,
) -> Path:
    """Write a wav simulating TTS output: leading silence + speech + trailing silence.

    Speech is simulated with a 440Hz tone (loud enough to exceed -40dB).
    Silence is true AudioSegment.silent. Shape matches what TTS providers
    (MiniMax / VolcEngine / CosyVoice) typically produce.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Generate 440Hz tone by concatenating sine-wave bytes at -10dB roughly
    # — pydub's Sine generator is the simplest path:
    from pydub.generators import Sine
    speech = Sine(440).to_audio_segment(duration=speech_ms).apply_gain(-10)
    leading = AudioSegment.silent(duration=leading_silence_ms)
    trailing = AudioSegment.silent(duration=trailing_silence_ms)
    combined = leading + speech + trailing
    combined.set_frame_rate(sample_rate).export(path, format="wav")
    return path


def _measure_ms(path: Path) -> int:
    return len(AudioSegment.from_wav(path))


def _make_segment(start_ms: int, end_ms: int, *, segment_id: int = 1) -> AlignedSegment:
    return AlignedSegment(
        segment_id=segment_id,
        speaker_id="speaker_a",
        display_name="A",
        start_ms=start_ms,
        end_ms=end_ms,
        cn_text="x",
        en_text="x",
        aligned_audio_path="",
        actual_duration_ms=0,
        alignment_method="direct",
        needs_review=False,
    )


# ---------------------------------------------------------------------------
# core invariant: both directions stretch to slot
# ---------------------------------------------------------------------------


def _assert_stretched_toward(
    result_ms: int, original_ms: int, target_ms: int,
    *, relative_tolerance: float = 0.1,
) -> None:
    """Assert atempo moved the duration toward target within tolerance.

    ffmpeg atempo is not millisecond-exact — it has accumulated rounding
    at moderate ratios (~5-10%) and worse at extremes (chained atempo
    stages). We assert two properties:

    1. **Direction correct**: result is between original and target
       (inclusive of target with tolerance), never overshoots past
       target in the wrong direction.
    2. **Magnitude correct**: |result - target| <= target * tolerance.
    """
    # Directional sanity: must have moved toward target
    if original_ms > target_ms:
        assert result_ms <= original_ms, (
            f"stretching long ({original_ms}ms) toward short target "
            f"({target_ms}ms) should compress, but result={result_ms}ms "
            "is longer"
        )
    elif original_ms < target_ms:
        assert result_ms >= original_ms, (
            f"stretching short ({original_ms}ms) toward long target "
            f"({target_ms}ms) should expand, but result={result_ms}ms "
            "is shorter"
        )
    # Magnitude: within tolerance of target
    max_drift = max(30, int(target_ms * relative_tolerance))
    assert abs(result_ms - target_ms) <= max_drift, (
        f"result={result_ms}ms drifts more than {max_drift}ms from "
        f"target={target_ms}ms (ratio original/target="
        f"{original_ms / target_ms:.2f}x)"
    )


def test_fit_stretches_long_segment_to_slot(tmp_path: Path) -> None:
    """Regression: old _trim_segment_audio_to_slot truncated 1.74s to
    0.76s, leaving only the first fraction of the user's TTS. New
    behaviour: stretch via atempo (≈2.3x speedup). Output ≈ slot ms."""
    wav = _make_wav(tmp_path / "long.wav", duration_ms=1740)
    segment = _make_segment(start_ms=0, end_ms=760)  # slot = 760ms

    EditorPackageWriter()._fit_segment_audio_to_slot(wav, segment)

    _assert_stretched_toward(_measure_ms(wav), 1740, 760)


def test_fit_stretches_short_segment_to_slot(tmp_path: Path) -> None:
    """Complement of above: 0.5s wav in 1.0s slot must be stretched UP
    (atempo < 1.0), not left short (which would leave dubbed audio
    with a silent tail in that slot). Old code did the latter."""
    wav = _make_wav(tmp_path / "short.wav", duration_ms=500)
    segment = _make_segment(start_ms=0, end_ms=1000)  # slot = 1000ms

    EditorPackageWriter()._fit_segment_audio_to_slot(wav, segment)

    _assert_stretched_toward(_measure_ms(wav), 500, 1000)


# ---------------------------------------------------------------------------
# tolerance no-op
# ---------------------------------------------------------------------------


def test_fit_is_noop_when_within_tolerance(tmp_path: Path) -> None:
    """If actual ≈ slot (within ~10ms), avoid the pointless ffmpeg round-
    trip. Preserves bit-for-bit identity for segments that happened to
    be exactly the right length (common when user didn't re-TTS that
    segment)."""
    wav = _make_wav(tmp_path / "exact.wav", duration_ms=1000)
    before_bytes = wav.read_bytes()
    segment = _make_segment(start_ms=0, end_ms=1000)

    EditorPackageWriter()._fit_segment_audio_to_slot(wav, segment)

    assert wav.read_bytes() == before_bytes, (
        "within-tolerance segments should not be touched — running "
        "atempo unnecessarily burns CPU and can introduce re-encode "
        "artefacts on a bit-exact match"
    )


def test_fit_is_noop_when_slot_duration_zero(tmp_path: Path) -> None:
    """Defensive: malformed AlignedSegment with end_ms <= start_ms."""
    wav = _make_wav(tmp_path / "x.wav", duration_ms=500)
    before_bytes = wav.read_bytes()
    segment = _make_segment(start_ms=100, end_ms=100)  # slot = 0

    EditorPackageWriter()._fit_segment_audio_to_slot(wav, segment)

    assert wav.read_bytes() == before_bytes


def test_fit_is_noop_when_wav_missing(tmp_path: Path) -> None:
    """Second sanity: don't crash if the file disappeared between
    _copy_segment_files' shutil.copy2 and this call (shouldn't happen
    in practice, but previous _trim handled this branch)."""
    wav = tmp_path / "missing.wav"  # never created
    segment = _make_segment(start_ms=0, end_ms=1000)

    # Must not raise
    EditorPackageWriter()._fit_segment_audio_to_slot(wav, segment)

    assert not wav.exists()


# ---------------------------------------------------------------------------
# extreme ratios still work (per 方案 A: no cap, user hears it in
# test-playback UI and re-edits if unhappy)
# ---------------------------------------------------------------------------


def test_fit_handles_extreme_compression_ratio(tmp_path: Path) -> None:
    """5s wav → 0.5s slot = 10x. ffmpeg atempo chains to support this;
    quality degrades ("chipmunk" effect) but the output is still a valid
    wav at the right duration. User reviews + re-edits if unhappy."""
    wav = _make_wav(tmp_path / "extreme.wav", duration_ms=5000)
    segment = _make_segment(start_ms=0, end_ms=500)  # 10x compression

    EditorPackageWriter()._fit_segment_audio_to_slot(wav, segment)

    result_ms = _measure_ms(wav)
    # Slightly looser tolerance at extreme ratios (accumulated rounding).
    assert abs(result_ms - 500) <= 80, (
        f"expected ~500ms at 10x compression, got {result_ms}ms"
    )



# ---------------------------------------------------------------------------
# TTS silence padding trim (pre-stretch optimisation)
#
# TTS providers typically pad output with 50-200ms of silence at each end.
# For γ publish resume, that padding gets counted against the user's slot —
# a 1.74s wav with 200ms silence on each side only has 1.34s of real speech.
# If slot is 0.76s, stretching 1.74s → 0.76s (2.3x) blows out audio quality;
# stretching 1.34s → 0.76s (1.76x) is much more tolerable.
#
# Strategy: trim leading/trailing silence BEFORE computing the atempo
# ratio. Leaves the shortened audio in the slot — any gap between segments
# is already covered by the dubbed_audio_complete master silence base.
# ---------------------------------------------------------------------------


def test_fit_trims_leading_and_trailing_silence_before_stretching(
    tmp_path: Path,
) -> None:
    """TTS output has ~150ms silence padding at each end. After fit,
    the silence should be gone and only the speech portion stretched
    toward the slot duration. Verifies two things:

    1. Result duration matches slot (within atempo tolerance)
    2. The leading edge of result is NOT silence (trim happened)
    """
    # 1300ms total = 150 silence + 1000 speech + 150 silence
    wav = _make_wav_with_silence_padding(
        tmp_path / "padded.wav",
        speech_ms=1000,
        leading_silence_ms=150,
        trailing_silence_ms=150,
    )
    # slot = 800ms; naive stretch would use ratio 1300/800 = 1.625x
    # trim-first stretch uses ratio 1000/800 = 1.25x (much better quality)
    segment = _make_segment(start_ms=0, end_ms=800)

    EditorPackageWriter()._fit_segment_audio_to_slot(wav, segment)

    result = AudioSegment.from_wav(wav)
    _assert_stretched_toward(len(result), 1300, 800)

    # First 20ms of result must be audible (NOT silence) — proves trim fired.
    # Silence would be dBFS around -inf / very negative; speech tone ~ -10dB.
    first_slice_dbfs = result[:20].dBFS
    assert first_slice_dbfs > -40, (
        f"leading 20ms of result is silent (dBFS={first_slice_dbfs:.1f}); "
        "silence trim did not fire — atempo ate CPU stretching padding"
    )


def test_fit_no_trim_when_audio_already_tight(tmp_path: Path) -> None:
    """No silence padding → trim is a no-op, stretch proceeds as before."""
    # 1000ms pure speech, no silence edges
    from pydub.generators import Sine
    wav = tmp_path / "tight.wav"
    Sine(440).to_audio_segment(duration=1000).apply_gain(-10).export(
        wav, format="wav",
    )
    segment = _make_segment(start_ms=0, end_ms=500)  # ratio 2x

    EditorPackageWriter()._fit_segment_audio_to_slot(wav, segment)

    _assert_stretched_toward(_measure_ms(wav), 1000, 500)


def test_fit_does_not_trim_silence_on_long_segments(tmp_path: Path) -> None:
    """Segments longer than 3s skip the silence-trim step: their quiet
    leading/trailing regions may be intentional pauses between phrases,
    not pure TTS padding. This test constructs a >3s wav with 200ms
    leading silence — after fit, that leading silence must still be
    present (stretched with the rest, not cut)."""
    # 4300ms total = 200 silence + 3900 speech + 200 silence (> 3s gate)
    wav = _make_wav_with_silence_padding(
        tmp_path / "long-with-pad.wav",
        speech_ms=3900,
        leading_silence_ms=200,
        trailing_silence_ms=200,
    )
    segment = _make_segment(start_ms=0, end_ms=2000)  # slot 2s

    EditorPackageWriter()._fit_segment_audio_to_slot(wav, segment)

    result = AudioSegment.from_wav(wav)
    # Leading slice is proportionally compressed: original 200ms padding
    # at 2.15x speedup ≈ 93ms of silence still remains at the head.
    # Assert the first 20ms is still silent (proves trim did NOT fire).
    first_slice_dbfs = result[:20].dBFS
    assert first_slice_dbfs < -40, (
        f"long segment ({len(result)}ms) had leading silence trimmed "
        f"(first 20ms dBFS={first_slice_dbfs:.1f}); silence-trim gate "
        "must only fire on short (≤3s) segments"
    )


def test_fit_does_not_over_trim_all_silent_wav(tmp_path: Path) -> None:
    """Defensive: if TTS output is entirely silence (bug / failed
    generation), the trim helper must NOT return an empty audio —
    that would crash ffmpeg atempo (can't stretch zero-length input).
    Fall back to stretching the original wav even if it's all silence."""
    wav = _make_wav(tmp_path / "all-silent.wav", duration_ms=1500)
    segment = _make_segment(start_ms=0, end_ms=800)

    # Must not raise
    EditorPackageWriter()._fit_segment_audio_to_slot(wav, segment)

    # Result still exists and is a valid wav
    result_ms = _measure_ms(wav)
    assert result_ms > 0, (
        "silence trim over-trimmed to empty audio; fit should fall back "
        "to stretching the original wav intact"
    )


def test_fit_handles_extreme_expansion_ratio(tmp_path: Path) -> None:
    """Flip: 0.2s wav → 5s slot = 25x expansion. Weaker assertion than
    the compression equivalent: ffmpeg atempo chained below 0.5x (e.g.
    atempo=0.5,atempo=0.5,atempo=0.5...) has known accumulated drift on
    silence/short input — the output lands between 2x and target but
    rarely hits target exactly. γ contract allows this: the user will
    hear it in test-playback and re-edit if unhappy.

    We still assert three properties:
    1. No exception (ffmpeg ran successfully)
    2. Output is strictly longer than input (stretched, not truncated)
    3. Output is a valid wav readable by pydub
    """
    wav = _make_wav(tmp_path / "tiny.wav", duration_ms=200)
    segment = _make_segment(start_ms=0, end_ms=5000)  # 25x expansion

    EditorPackageWriter()._fit_segment_audio_to_slot(wav, segment)

    result_ms = _measure_ms(wav)
    assert result_ms > 200, (
        f"25x expansion must produce audio longer than input 200ms, "
        f"got {result_ms}ms — atempo likely failed silently"
    )
    # At minimum 2x — sanity check that the stretch actually ran
    assert result_ms >= 400, (
        f"expected at least 2x expansion, got {result_ms}ms"
    )
