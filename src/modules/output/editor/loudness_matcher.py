from __future__ import annotations

import array
from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import statistics
import subprocess
import sys
import wave
from collections.abc import Iterable, Mapping, Sequence

from modules.output.editor.editor_package_models import AlignedSegment

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LoudnessMatchPolicy:
    min_segment_duration_ms: int = 300
    frame_ms: int = 50
    active_floor_dbfs: float = -50.0
    active_below_peak_db: float = 35.0
    min_gain_db: float = -24.0
    max_gain_db: float = 10.0
    max_segment_residual_db: float = 3.0
    min_apply_gain_db: float = 0.25
    limiter_limit: float = 0.97


@dataclass(frozen=True, slots=True)
class LoudnessPair:
    segment_id: int
    speaker_id: str
    source_dbfs: float
    output_dbfs: float
    duration_ms: int


@dataclass(frozen=True, slots=True)
class SegmentGain:
    segment_id: int
    speaker_id: str
    source_dbfs: float | None
    output_dbfs: float | None
    speaker_gain_db: float | None
    gain_db: float
    applied: bool
    reason: str | None = None


_DEFAULT_POLICY = LoudnessMatchPolicy()


def match_segment_loudness_to_source(
    *,
    project_dir: Path,
    segments: Sequence[AlignedSegment],
    segment_paths: Mapping[int, str],
    output_root: Path,
    policy: LoudnessMatchPolicy = _DEFAULT_POLICY,
) -> Path:
    """Match generated segment loudness to the original speaker timeline.

    TTS providers and cloned voices emit very different raw gains. We correct
    that before ``amix`` by measuring each generated segment against the same
    time window in the source vocal track, then applying a speaker-level gain
    plus a small per-segment residual.
    """
    report_path = output_root / "loudness_report.json"
    output_root.mkdir(parents=True, exist_ok=True)

    reference_path = _resolve_reference_audio_path(project_dir)
    if reference_path is None:
        _write_report(
            report_path,
            reference_audio_path=None,
            speaker_gains={},
            segments=[
                SegmentGain(
                    segment_id=segment.segment_id,
                    speaker_id=segment.speaker_id,
                    source_dbfs=None,
                    output_dbfs=None,
                    speaker_gain_db=None,
                    gain_db=0.0,
                    applied=False,
                    reason="missing_reference_audio",
                )
                for segment in segments
            ],
            policy=policy,
        )
        return report_path

    pairs: list[LoudnessPair] = []
    gains: list[SegmentGain] = []
    sorted_segments = sorted(
        segments,
        key=lambda item: (item.start_ms, item.segment_id),
    )
    for segment in sorted_segments:
        segment_path_raw = segment_paths.get(segment.segment_id)
        if not segment_path_raw:
            gains.append(_skipped_segment(segment, "missing_segment_audio"))
            continue
        segment_path = Path(segment_path_raw).resolve(strict=False)
        duration_ms = max(0, int(segment.end_ms) - int(segment.start_ms))
        if duration_ms < policy.min_segment_duration_ms:
            gains.append(_skipped_segment(segment, "too_short"))
            continue
        if segment.alignment_method == "keep_original":
            gains.append(_skipped_segment(segment, "keep_original"))
            continue

        source_dbfs = measure_active_loudness_dbfs(
            reference_path,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            policy=policy,
        )
        output_dbfs = measure_active_loudness_dbfs(segment_path, policy=policy)
        if source_dbfs is None or output_dbfs is None:
            gains.append(
                SegmentGain(
                    segment_id=segment.segment_id,
                    speaker_id=segment.speaker_id,
                    source_dbfs=source_dbfs,
                    output_dbfs=output_dbfs,
                    speaker_gain_db=None,
                    gain_db=0.0,
                    applied=False,
                    reason="unmeasurable_loudness",
                )
            )
            continue
        pairs.append(
            LoudnessPair(
                segment_id=segment.segment_id,
                speaker_id=segment.speaker_id,
                source_dbfs=source_dbfs,
                output_dbfs=output_dbfs,
                duration_ms=duration_ms,
            )
        )

    speaker_gains = calculate_speaker_gains(pairs, policy=policy)
    pair_by_id = {pair.segment_id: pair for pair in pairs}
    adjusted_segments = 0
    total_abs_gain = 0.0
    for segment in sorted_segments:
        pair = pair_by_id.get(segment.segment_id)
        if pair is None:
            continue
        speaker_gain = speaker_gains.get(segment.speaker_id)
        if speaker_gain is None:
            gains.append(
                SegmentGain(
                    segment_id=segment.segment_id,
                    speaker_id=segment.speaker_id,
                    source_dbfs=pair.source_dbfs,
                    output_dbfs=pair.output_dbfs,
                    speaker_gain_db=None,
                    gain_db=0.0,
                    applied=False,
                    reason="missing_speaker_gain",
                )
            )
            continue

        gain_db = calculate_segment_gain_db(
            source_dbfs=pair.source_dbfs,
            output_dbfs=pair.output_dbfs,
            speaker_gain_db=speaker_gain,
            policy=policy,
        )
        segment_path = Path(segment_paths[segment.segment_id]).resolve(strict=False)
        applied = False
        reason: str | None = None
        if abs(gain_db) < policy.min_apply_gain_db:
            reason = "within_tolerance"
        elif _apply_gain(segment_path, gain_db, policy=policy):
            applied = True
            adjusted_segments += 1
            total_abs_gain += abs(gain_db)
        else:
            reason = "ffmpeg_gain_failed"

        gains.append(
            SegmentGain(
                segment_id=segment.segment_id,
                speaker_id=segment.speaker_id,
                source_dbfs=pair.source_dbfs,
                output_dbfs=pair.output_dbfs,
                speaker_gain_db=speaker_gain,
                gain_db=gain_db,
                applied=applied,
                reason=reason,
            )
        )

    gains.sort(key=lambda item: item.segment_id)
    _write_report(
        report_path,
        reference_audio_path=str(reference_path),
        speaker_gains=speaker_gains,
        segments=gains,
        policy=policy,
    )
    if adjusted_segments:
        logger.info(
            "matched segment loudness for %s segments, mean_abs_gain=%.2fdB",
            adjusted_segments,
            total_abs_gain / adjusted_segments,
        )
        print(f"[S6] 分段配音响度匹配完成：{adjusted_segments}段")
    return report_path


def calculate_speaker_gains(
    pairs: Iterable[LoudnessPair],
    *,
    policy: LoudnessMatchPolicy = _DEFAULT_POLICY,
) -> dict[str, float]:
    deltas_by_speaker: dict[str, list[float]] = {}
    for pair in pairs:
        deltas_by_speaker.setdefault(pair.speaker_id, []).append(
            pair.source_dbfs - pair.output_dbfs
        )
    return {
        speaker_id: _clamp(
            _robust_median(deltas),
            policy.min_gain_db,
            policy.max_gain_db,
        )
        for speaker_id, deltas in deltas_by_speaker.items()
        if deltas
    }


def calculate_segment_gain_db(
    *,
    source_dbfs: float,
    output_dbfs: float,
    speaker_gain_db: float,
    policy: LoudnessMatchPolicy = _DEFAULT_POLICY,
) -> float:
    raw_gain = source_dbfs - output_dbfs
    residual = _clamp(
        raw_gain - speaker_gain_db,
        -policy.max_segment_residual_db,
        policy.max_segment_residual_db,
    )
    return _clamp(
        speaker_gain_db + residual,
        policy.min_gain_db,
        policy.max_gain_db,
    )


def measure_active_loudness_dbfs(
    path: Path,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    policy: LoudnessMatchPolicy = _DEFAULT_POLICY,
) -> float | None:
    try:
        rate, samples = _read_pcm16_mono(path, start_ms=start_ms, end_ms=end_ms)
    except Exception as exc:
        logger.debug("failed to read wav loudness for %s: %s", path, exc)
        return None
    if not samples:
        return None

    frame_len = max(1, int(rate * policy.frame_ms / 1000))
    frame_powers: list[float] = []
    peak = 0
    for offset in range(0, len(samples), frame_len):
        frame = samples[offset:offset + frame_len]
        if not frame:
            continue
        peak = max(peak, max(abs(sample) for sample in frame))
        frame_powers.append(
            sum(float(sample) * float(sample) for sample in frame)
            / len(frame)
            / (32768.0 * 32768.0)
        )
    if not frame_powers:
        return None

    peak_dbfs = 20.0 * math.log10(max(1, peak) / 32768.0)
    active_threshold_dbfs = max(
        policy.active_floor_dbfs,
        peak_dbfs - policy.active_below_peak_db,
    )
    active_powers: list[float] = []
    for power in frame_powers:
        frame_dbfs = _dbfs_from_power(power)
        if frame_dbfs is not None and frame_dbfs >= active_threshold_dbfs:
            active_powers.append(power)
    if not active_powers:
        return None
    return _dbfs_from_power(sum(active_powers) / len(active_powers))


def _resolve_reference_audio_path(project_dir: Path) -> Path | None:
    audio_root = project_dir.resolve(strict=False) / "audio"
    for filename in ("speech_for_asr.wav", "original.wav"):
        candidate = audio_root / filename
        if candidate.exists():
            return candidate
    return None


def _read_pcm16_mono(
    path: Path,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> tuple[int, list[int]]:
    with wave.open(str(path), "rb") as wav:
        sample_rate = wav.getframerate()
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        total_frames = wav.getnframes()
        if sample_width != 2:
            raise ValueError(f"unsupported WAV sample width: {sample_width}")

        start_frame = (
            0
            if start_ms is None
            else max(0, int(sample_rate * start_ms / 1000))
        )
        end_frame = total_frames if end_ms is None else min(
            total_frames,
            int(sample_rate * end_ms / 1000),
        )
        if end_frame <= start_frame:
            return sample_rate, []
        wav.setpos(start_frame)
        raw = wav.readframes(end_frame - start_frame)

    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if channels <= 1:
        return sample_rate, list(samples)

    mono_samples: list[int] = []
    for index in range(0, len(samples), channels):
        mono_samples.append(int(sum(samples[index:index + channels]) / channels))
    return sample_rate, mono_samples


def _apply_gain(
    path: Path,
    gain_db: float,
    *,
    policy: LoudnessMatchPolicy,
) -> bool:
    if not path.exists():
        return False
    tmp_path = path.with_name(f"{path.stem}.__gain_tmp__{path.suffix}")
    if tmp_path.exists():
        tmp_path.unlink()
    audio_filter = (
        f"volume={gain_db:.2f}dB,"
        f"alimiter=limit={policy.limiter_limit:.3f}"
    )
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-af",
                audio_filter,
                "-acodec",
                "pcm_s16le",
                "-ar",
                "44100",
                "-ac",
                "2",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        tmp_path.unlink(missing_ok=True)
        return False
    if result.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        logger.warning(
            "ffmpeg segment gain failed for %s: %s",
            path,
            (result.stderr or "").strip(),
        )
        return False
    tmp_path.replace(path)
    return True


def _write_report(
    report_path: Path,
    *,
    reference_audio_path: str | None,
    speaker_gains: Mapping[str, float],
    segments: Sequence[SegmentGain],
    policy: LoudnessMatchPolicy,
) -> None:
    payload = {
        "reference_audio_path": reference_audio_path,
        "speaker_gains_db": {
            speaker_id: round(gain_db, 3)
            for speaker_id, gain_db in sorted(speaker_gains.items())
        },
        "policy": {
            "min_segment_duration_ms": policy.min_segment_duration_ms,
            "min_gain_db": policy.min_gain_db,
            "max_gain_db": policy.max_gain_db,
            "max_segment_residual_db": policy.max_segment_residual_db,
            "limiter_limit": policy.limiter_limit,
        },
        "segments": [
            {
                "segment_id": item.segment_id,
                "speaker_id": item.speaker_id,
                "source_active_dbfs": _round_or_none(item.source_dbfs),
                "output_active_dbfs": _round_or_none(item.output_dbfs),
                "speaker_gain_db": _round_or_none(item.speaker_gain_db),
                "gain_db": round(item.gain_db, 3),
                "applied": item.applied,
                "reason": item.reason,
            }
            for item in segments
        ],
    }
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _skipped_segment(segment: AlignedSegment, reason: str) -> SegmentGain:
    return SegmentGain(
        segment_id=segment.segment_id,
        speaker_id=segment.speaker_id,
        source_dbfs=None,
        output_dbfs=None,
        speaker_gain_db=None,
        gain_db=0.0,
        applied=False,
        reason=reason,
    )


def _dbfs_from_power(power: float) -> float | None:
    if power <= 0:
        return None
    return 10.0 * math.log10(power)


def _robust_median(values: Sequence[float]) -> float:
    if len(values) >= 7:
        ordered = sorted(values)
        trim_count = max(1, int(len(ordered) * 0.1))
        ordered = ordered[trim_count:-trim_count] or ordered
        return statistics.median(ordered)
    return statistics.median(values)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _round_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 3)
