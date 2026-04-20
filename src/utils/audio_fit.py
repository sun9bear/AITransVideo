"""Time-align a TTS wav file to a target slot duration.

Used by γ publish (``editor_package_writer._fit_segment_audio_to_slot``)
and available for future adoption by the main pipeline's alignment
fallback (``services.alignment.aligner._dsp_stretch``).

Policy (2026-04-20, superseding the naive "single atempo to exact
target" approach):

1. **Smart silence trim** — only trim leading/trailing silence if
   trimming brings the duration *closer* to the slot. For a TTS that's
   already much shorter than its slot (e.g. short translation "嗯"
   placed in a 2-second slot), trimming would make the atempo ratio
   even more extreme; skip it.

2. **Clamped atempo stretch** — the ratio is capped to a natural-
   sounding window ``[atempo_min, atempo_max]`` (defaults 0.8x–1.5x).
   Beyond that range, audio quality degrades into chipmunk / slow-mo
   territory even though atempo technically "works".

3. **Silence pad / truncate to exact slot** — after clamped stretch:
   - shorter than slot → pad the tail with silence (natural pause,
     better than slow-mo rubber voice)
   - longer than slot → truncate at slot (segments are composed into a
     fixed timeline in publish; overrun would overlap the next segment)

The decision matrix visualised:

    actual/slot ratio     policy
    =================     ===================================
    1.0  ± tolerance      noop (bit-identical preserve)
    [atempo_min, atempo_max]   plain atempo
    < atempo_min          atempo to atempo_min, then pad silence
    > atempo_max          atempo to atempo_max, then truncate

Design note: smart trim runs BEFORE clamped stretch so it can reduce
the effective ratio into the natural-sounding window when the "too
much silence padding" is the actual problem (common on TTS output).
It never hurts: the "closer to slot" guard makes it a no-op for
already-too-short audio.

"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydub import AudioSegment
from pydub.silence import detect_leading_silence

logger = logging.getLogger(__name__)

__all__ = [
    "FitPolicy",
    "FitResult",
    "fit_audio_to_slot",
]


# Thresholds kept module-level (vs method arguments) so downstream tests
# can import + assert against the same constants the runtime uses.
_DEFAULT_FIT_TOLERANCE_MS = 10
_DEFAULT_ATEMPO_MIN = 0.8
_DEFAULT_ATEMPO_MAX = 1.5
_DEFAULT_SILENCE_TRIM_MAX_MS = 3_000
_DEFAULT_SILENCE_THRESHOLD_DBFS = -40.0
_DEFAULT_SILENCE_CHUNK_MS = 10
_MIN_KEEP_RATIO_AFTER_TRIM = 0.05


@dataclass(frozen=True, slots=True)
class FitPolicy:
    """Tuneable parameters for ``fit_audio_to_slot``.

    Kept as an immutable dataclass so callers can share one instance
    across many segments without worrying about accidental mutation.
    Defaults are calibrated for Studio γ publish (user-edited TTS
    placed into fixed slots); the main-pipeline alignment fallback
    may want wider bounds and can pass its own.
    """
    tolerance_ms: int = _DEFAULT_FIT_TOLERANCE_MS
    atempo_min: float = _DEFAULT_ATEMPO_MIN
    atempo_max: float = _DEFAULT_ATEMPO_MAX
    silence_trim_enabled: bool = True
    silence_trim_max_ms: int = _DEFAULT_SILENCE_TRIM_MAX_MS
    silence_threshold_dbfs: float = _DEFAULT_SILENCE_THRESHOLD_DBFS
    silence_chunk_ms: int = _DEFAULT_SILENCE_CHUNK_MS
    pad_short_with_silence: bool = True


@dataclass(frozen=True, slots=True)
class FitResult:
    """Return value of ``fit_audio_to_slot`` describing what the helper
    actually did. Useful for tests + logging; callers that only care
    about the final duration can use ``result.final_duration_ms``."""
    initial_duration_ms: int
    trimmed_duration_ms: int    # == initial if trim was skipped
    stretched_duration_ms: int  # == trimmed if within tolerance / clamped
    final_duration_ms: int
    speed_ratio_used: float     # what atempo actually applied
    silence_padded_ms: int      # how much trailing silence was added
    truncated_ms: int           # how much tail was cut


_DEFAULT_POLICY = FitPolicy()


def fit_audio_to_slot(
    wav_path: Path,
    slot_duration_ms: int,
    *,
    output_path: Path | None = None,
    policy: FitPolicy = _DEFAULT_POLICY,
) -> FitResult | None:
    """Time-align ``wav_path`` to exactly ``slot_duration_ms``.

    ``output_path`` defaults to ``wav_path`` (in-place). Writes are
    atomic (tmp file + ``Path.replace``) so a hardlink-backed ``wav_path``
    is unlinked first, never mutated in place — callers in copy_as_new
    targets can rely on source wav's inode staying untouched.

    Returns ``None`` when the helper short-circuits (slot ≤ 0, wav
    missing, or unreadable); otherwise a ``FitResult`` describing each
    stage's effect on duration.

    Safe to call with actual duration already matching slot — within
    ``policy.tolerance_ms`` the file is left bit-identical (no
    re-encode) and a FitResult is still returned.
    """
    target_path = output_path or wav_path
    if slot_duration_ms <= 0 or not wav_path.exists():
        return None

    try:
        audio = AudioSegment.from_wav(wav_path)
    except Exception:
        return None

    initial_ms = len(audio)
    if initial_ms <= 0:
        return None

    # Step 1 — smart silence trim.
    trimmed_ms = initial_ms
    current_audio = audio
    if (
        policy.silence_trim_enabled
        and initial_ms <= policy.silence_trim_max_ms
    ):
        candidate = _trim_silence_edges(audio, policy)
        candidate_len = len(candidate)
        if (
            candidate_len > 0
            and candidate_len < initial_ms
            and abs(candidate_len - slot_duration_ms)
            < abs(initial_ms - slot_duration_ms)
        ):
            current_audio = candidate
            trimmed_ms = candidate_len

    # Already close enough? Write the (possibly trimmed) audio back and
    # return. Skip atempo and silence padding — pointless re-encode.
    if abs(trimmed_ms - slot_duration_ms) <= policy.tolerance_ms:
        if trimmed_ms != initial_ms:  # trim actually fired
            _atomic_write_wav(current_audio, wav_path, target_path)
        return FitResult(
            initial_duration_ms=initial_ms,
            trimmed_duration_ms=trimmed_ms,
            stretched_duration_ms=trimmed_ms,
            final_duration_ms=trimmed_ms,
            speed_ratio_used=1.0,
            silence_padded_ms=0,
            truncated_ms=0,
        )

    # Step 2 — clamped atempo stretch.
    raw_ratio = trimmed_ms / slot_duration_ms
    clamped_ratio = max(
        policy.atempo_min, min(policy.atempo_max, raw_ratio)
    )
    if abs(clamped_ratio - 1.0) < 1e-6:
        stretched_ms = trimmed_ms
        stretched_audio = current_audio
    else:
        stretched_audio = _apply_atempo_via_ffmpeg(
            current_audio, wav_path, target_path, clamped_ratio,
        )
        if stretched_audio is None:
            return None  # ffmpeg failed, caller sees no state change
        stretched_ms = len(stretched_audio)

    # Step 3 — pad silence or truncate to exactly slot_duration_ms.
    silence_padded_ms = 0
    truncated_ms = 0
    if stretched_ms < slot_duration_ms and policy.pad_short_with_silence:
        gap = slot_duration_ms - stretched_ms
        stretched_audio = stretched_audio + AudioSegment.silent(
            duration=gap,
            frame_rate=stretched_audio.frame_rate,
        ).set_channels(stretched_audio.channels).set_sample_width(
            stretched_audio.sample_width
        )
        silence_padded_ms = gap
    elif stretched_ms > slot_duration_ms:
        truncated_ms = stretched_ms - slot_duration_ms
        stretched_audio = stretched_audio[:slot_duration_ms]

    _atomic_write_wav(stretched_audio, wav_path, target_path)
    final_ms = len(stretched_audio)

    return FitResult(
        initial_duration_ms=initial_ms,
        trimmed_duration_ms=trimmed_ms,
        stretched_duration_ms=stretched_ms,
        final_duration_ms=final_ms,
        speed_ratio_used=clamped_ratio,
        silence_padded_ms=silence_padded_ms,
        truncated_ms=truncated_ms,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trim_silence_edges(
    audio: AudioSegment, policy: FitPolicy,
) -> AudioSegment:
    """Return audio with leading + trailing silence removed. Returns
    the original if the trim would leave <5% of the duration (defensive
    for all-silence TTS bug outputs)."""
    if len(audio) == 0:
        return audio
    leading = detect_leading_silence(
        audio,
        silence_threshold=policy.silence_threshold_dbfs,
        chunk_size=policy.silence_chunk_ms,
    )
    trailing = detect_leading_silence(
        audio.reverse(),
        silence_threshold=policy.silence_threshold_dbfs,
        chunk_size=policy.silence_chunk_ms,
    )
    keep_len = len(audio) - leading - trailing
    if keep_len <= 0 or keep_len < len(audio) * _MIN_KEEP_RATIO_AFTER_TRIM:
        return audio
    return audio[leading:len(audio) - trailing]


def _apply_atempo_via_ffmpeg(
    audio_in_memory: AudioSegment,
    source_path: Path,
    target_path: Path,
    speed_ratio: float,
) -> AudioSegment | None:
    """Run ffmpeg atempo on ``source_path`` (or a fresh export of
    ``audio_in_memory`` if it's been modified) and return the resulting
    AudioSegment. Returns None on ffmpeg failure (caller keeps original
    state)."""
    # If the in-memory audio differs from what's on disk (trim ran),
    # export to a temp .wav first so ffmpeg operates on the trimmed
    # content, not the untouched source.
    temp_input_path: Path | None = None
    ffmpeg_input: Path = source_path
    try:
        on_disk_audio = AudioSegment.from_wav(source_path)
        if len(on_disk_audio) != len(audio_in_memory):
            temp_input_path = source_path.with_name(
                f".{source_path.stem}.trimmed.wav"
            )
            audio_in_memory.export(temp_input_path, format="wav")
            ffmpeg_input = temp_input_path
    except Exception:
        # If we can't even re-read the source, fall back to export path.
        temp_input_path = source_path.with_name(
            f".{source_path.stem}.trimmed.wav"
        )
        audio_in_memory.export(temp_input_path, format="wav")
        ffmpeg_input = temp_input_path

    stretched_path = source_path.with_name(
        f".{source_path.stem}.stretched.wav"
    )
    filter_value = _build_atempo_filter(speed_ratio)
    command = [
        "ffmpeg",
        "-i", str(ffmpeg_input),
        "-filter:a", filter_value,
        "-f", "wav",
        "-acodec", "pcm_s16le",
        "-ar", "44100",
        "-ac", "2",
        "-y", str(stretched_path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        logger.warning("ffmpeg not in PATH; atempo stretch skipped")
        _cleanup(stretched_path, temp_input_path)
        return None

    if completed.returncode != 0 or not stretched_path.exists():
        logger.warning(
            "atempo stretch failed (rc=%s stderr=%r)",
            completed.returncode, (completed.stderr or "")[:200],
        )
        _cleanup(stretched_path, temp_input_path)
        return None

    try:
        stretched_audio = AudioSegment.from_wav(stretched_path)
    except Exception:
        _cleanup(stretched_path, temp_input_path)
        return None

    # Clean the ffmpeg tmp file; caller will atomic-write the final
    # result. Temp trimmed-input path is no longer needed either.
    _cleanup(stretched_path, temp_input_path)
    return stretched_audio


def _cleanup(*paths: Path | None) -> None:
    for p in paths:
        if p is None:
            continue
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_write_wav(
    audio: AudioSegment, source_path: Path, target_path: Path,
) -> None:
    """Export audio to ``target_path`` atomically. If target_path is
    hardlinked to another location (copy_as_new siblings), the old
    link is broken via Path.replace, leaving the other side's inode
    untouched."""
    tmp = target_path.with_name(f".{target_path.stem}.write.tmp.wav")
    audio.export(tmp, format="wav")
    tmp.replace(target_path)


def _build_atempo_filter(speed_ratio: float) -> str:
    """Build a multi-stage ``atempo`` filter string for arbitrary ratios.

    ffmpeg's ``atempo`` natively supports [0.5, 2.0]; outside that range
    we chain stages until the remaining factor fits. ``fit_audio_to_slot``
    clamps ratio to a narrower natural-sounding window, so the chain
    usually collapses to a single stage; keeping the generic form lets
    the same helper serve wider main-pipeline policies in future.
    """
    if speed_ratio <= 0:
        raise ValueError("speed_ratio must be positive")
    remaining = float(speed_ratio)
    factors: list[float] = []
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(remaining)
    return ",".join(
        f"atempo={_format_atempo_factor(factor)}" for factor in factors
    )


def _format_atempo_factor(value: float) -> str:
    formatted = f"{value:.6f}".rstrip("0").rstrip(".")
    return formatted or "1"
