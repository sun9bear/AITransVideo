from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydub import AudioSegment

from services.assemblyai.transcriber import TranscriptLine


MIN_SEGMENT_DURATION_MS = 5_000
FALLBACK_SEGMENT_DURATION_MS = 1_000
MAX_SEGMENT_JOIN_GAP_MS = 1_500
LOW_VOLUME_THRESHOLD_DBFS = -40.0
MIN_SAMPLE_DURATION_SECONDS = 10.0
SILENCE_WARNING_RATIO = 0.30
ANALYSIS_CHUNK_MS = 100


class SampleExtractionError(Exception):
    pass


@dataclass(slots=True)
class _SpeakerCandidate:
    """A concatenation of one-or-more contiguous speaker_lines selected
    for inclusion in the final sample.

    Note: intervals track **actual line audio ranges only** (not the
    gaps between joined lines). Matches the pre-rewrite pydub behavior
    where ``current_clip += clip`` concatenated per-line slices
    directly without including the silence between them."""
    intervals: list[tuple[int, int]]

    @property
    def duration_ms(self) -> int:
        return sum(max(0, end - start) for start, end in self.intervals)


class VoiceSampleExtractor:
    """Extract a compact voice sample for voice cloning by concatenating
    the loudest contiguous stretches of the speaker's lines.

    2026-04-20 OOM-safe rewrite: the old pydub path loaded the FULL
    original.wav (3h ≈ 1 GB raw PCM) and sliced + concatenated in
    memory. For long inputs (interviews, conference talks) this
    ballooned to multi-GB RSS and could OOM in the 7.6 GB container.
    The new path uses ffmpeg subprocesses:

      - per-line RMS probes stream a short `-ss -t` window (O(1) memory)
      - selected candidates are extracted with `-ss -t` as 16 kHz mono
        s16 WAVs
      - final sample is a ffmpeg-concat of those slices

    Semantics preserved: same grouping rule (join adjacent lines within
    ``MAX_SEGMENT_JOIN_GAP_MS``), same loudness gate
    (``LOW_VOLUME_THRESHOLD_DBFS``), same min/max duration bounds, same
    output format (16 kHz mono s16 WAV).
    """

    def extract_sample(
        self,
        audio_path: str,
        speaker_lines: list[TranscriptLine],
        output_path: str,
        min_duration_s: float = 10.0,
        max_duration_s: float = 300.0,
    ) -> str:
        source_path = Path(audio_path).expanduser().resolve(strict=False)
        if not source_path.exists():
            raise SampleExtractionError(f"原始音频不存在：{source_path}")

        output_file = Path(output_path).expanduser().resolve(strict=False)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        min_duration_ms = max(int(min_duration_s * 1000), 1)
        max_duration_ms = max(int(max_duration_s * 1000), min_duration_ms)

        all_candidates = _build_candidate_ranges(source_path, speaker_lines)
        candidates = [c for c in all_candidates if c.duration_ms >= MIN_SEGMENT_DURATION_MS]
        if sum(c.duration_ms for c in candidates) < min_duration_ms:
            candidates = [c for c in all_candidates if c.duration_ms >= FALLBACK_SEGMENT_DURATION_MS]

        if not candidates:
            raise SampleExtractionError("没有可用的说话人音色样本片段。")

        # Largest first — match the original "give the longest runs to
        # the cloning model" policy.
        candidates.sort(key=lambda c: c.duration_ms, reverse=True)

        # Flatten picked candidates into a per-interval extraction plan,
        # truncating the last one if total would exceed max_duration_ms.
        # Each interval produces one ffmpeg slice file, then we concat.
        extract_plan: list[tuple[int, int]] = []  # [(start_ms, duration_ms), ...]
        total_ms = 0
        for candidate in candidates:
            if total_ms >= max_duration_ms:
                break
            for start_ms, end_ms in candidate.intervals:
                interval_ms = max(0, end_ms - start_ms)
                if interval_ms <= 0:
                    continue
                remaining_ms = max_duration_ms - total_ms
                if remaining_ms <= 0:
                    break
                take_ms = min(interval_ms, remaining_ms)
                extract_plan.append((start_ms, take_ms))
                total_ms += take_ms

        if total_ms <= 0 or not extract_plan:
            raise SampleExtractionError("样本提取失败，没有拼接出可用音频。")

        if total_ms < min_duration_ms:
            print(
                f"[S2] 警告：提取的样本仅 {round(total_ms / 1000, 1)} 秒，"
                f"低于建议最小时长 {round(min_duration_ms / 1000, 1)} 秒"
            )

        # Slice → concat → write final output. Temp dir auto-cleaned.
        with tempfile.TemporaryDirectory(prefix="voice_sample_") as temp_root:
            temp_dir = Path(temp_root)
            slice_paths: list[Path] = []
            for idx, (start_ms, take_ms) in enumerate(extract_plan):
                slice_path = temp_dir / f"slice_{idx:04d}.wav"
                _ffmpeg_extract_slice(
                    source_path=source_path,
                    start_ms=start_ms,
                    duration_ms=take_ms,
                    dst_path=slice_path,
                )
                if slice_path.exists() and slice_path.stat().st_size > 0:
                    slice_paths.append(slice_path)

            if not slice_paths:
                raise SampleExtractionError("样本提取失败，没有可用切片。")

            if len(slice_paths) == 1:
                # Single slice — copy straight to destination (skip concat)
                _copy_file(slice_paths[0], output_file)
            else:
                _ffmpeg_concat_wavs(slice_paths, output_file)

        return str(output_file)

    def validate_sample(self, sample_path: str) -> dict:
        path = Path(sample_path).expanduser().resolve(strict=False)
        if not path.exists():
            raise SampleExtractionError(f"样本文件不存在：{path}")

        # validate_sample operates on the final trimmed sample (≤300s),
        # so pydub loading here costs ≤30 MB — safe to keep.
        audio = AudioSegment.from_wav(path)
        duration_s = round(len(audio) / 1000, 1)
        rms_dbfs = round(_safe_dbfs(audio), 1)
        silence_ratio = round(_calculate_silence_ratio(audio), 2)

        warnings: list[str] = []
        if duration_s < MIN_SAMPLE_DURATION_SECONDS:
            warnings.append(f"样本时长不足{int(MIN_SAMPLE_DURATION_SECONDS)}秒")
        if silence_ratio > SILENCE_WARNING_RATIO:
            warnings.append("静音占比超过30%")
        if rms_dbfs <= LOW_VOLUME_THRESHOLD_DBFS:
            warnings.append("样本整体音量过低")

        return {
            "duration_s": duration_s,
            "rms_dbfs": rms_dbfs,
            "silence_ratio": silence_ratio,
            "is_valid": len(warnings) == 0,
            "warnings": warnings,
        }


# ---------------------------------------------------------------------------
# pydub-based helpers for validate_sample (≤300s sample, safe to load)
# ---------------------------------------------------------------------------


def _safe_dbfs(audio: AudioSegment) -> float:
    if audio.rms <= 0:
        return -100.0
    return float(audio.dBFS)


def _calculate_silence_ratio(audio: AudioSegment) -> float:
    if len(audio) <= 0:
        return 1.0

    silent_chunks = 0
    total_chunks = 0
    for start_ms in range(0, len(audio), ANALYSIS_CHUNK_MS):
        chunk = audio[start_ms:start_ms + ANALYSIS_CHUNK_MS]
        total_chunks += 1
        if _safe_dbfs(chunk) <= LOW_VOLUME_THRESHOLD_DBFS:
            silent_chunks += 1

    if total_chunks == 0:
        return 1.0
    return silent_chunks / total_chunks


# ---------------------------------------------------------------------------
# ffmpeg streaming primitives — O(1) python memory regardless of source length
# ---------------------------------------------------------------------------


_RMS_LEVEL_RE = re.compile(r"RMS\s*level\s*dB:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def _ffmpeg_probe_rms_db(
    source_path: Path, start_ms: int, end_ms: int,
) -> float | None:
    """RMS dBFS of the `[start_ms, end_ms]` window via streaming astats.

    Returns None on probe failure or on a silent/inf window (caller
    should treat as "skip this line"). Each call is a small ffmpeg
    subprocess (<200 ms typical for short windows)."""
    duration_ms = end_ms - start_ms
    if duration_ms <= 0:
        return None
    start_s = max(0, start_ms) / 1000.0
    duration_s = duration_ms / 1000.0
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostats", "-hide_banner",
                "-ss", f"{start_s:.3f}",
                "-t", f"{duration_s:.3f}",
                "-i", str(source_path),
                "-af", "astats=metadata=0:measure_overall=RMS_level",
                "-vn",
                "-f", "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    stderr = result.stderr or ""
    match = _RMS_LEVEL_RE.search(stderr)
    if not match:
        return None
    try:
        rms = float(match.group(1))
    except (TypeError, ValueError):
        return None
    if rms == float("-inf") or rms <= -120.0:
        return None
    return rms


def _ffmpeg_extract_slice(
    *,
    source_path: Path,
    start_ms: int,
    duration_ms: int,
    dst_path: Path,
) -> None:
    """Stream a slice of the source to ``dst_path`` as 16 kHz mono s16le
    WAV — matches the final sample's normalized format so the concat
    demuxer can stitch slices without re-encoding."""
    start_s = max(0, start_ms) / 1000.0
    duration_s = max(0, duration_ms) / 1000.0
    if duration_s <= 0:
        return
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{start_s:.3f}",
                "-t", f"{duration_s:.3f}",
                "-i", str(source_path),
                "-ar", "16000",
                "-ac", "1",
                "-sample_fmt", "s16",
                "-f", "wav",
                str(dst_path),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        # Skip this slice — caller filters out missing dst paths. Don't
        # raise: a single bad line shouldn't abort the whole sample.
        return


def _ffmpeg_concat_wavs(slice_paths: list[Path], dst_path: Path) -> None:
    """Concatenate matched-format WAV slices into ``dst_path`` via the
    concat demuxer (copy codec, no re-encode). Falls back to concat
    filter re-encoding if copy fails (format drift edge cases).

    All ``slice_paths`` must be 16 kHz mono s16le — our extractor
    guarantees that upstream."""
    manifest_path = dst_path.with_suffix(".concat.txt")
    # Quote the path for the concat demuxer syntax.
    manifest_path.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in slice_paths),
        encoding="utf-8",
    )
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(manifest_path),
                "-c", "copy",
                str(dst_path),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
    except subprocess.CalledProcessError:
        # Codec copy failed — re-encode via concat filter (still streaming)
        inputs: list[str] = []
        for p in slice_paths:
            inputs.extend(["-i", str(p)])
        filter_parts = "".join(f"[{i}:0]" for i in range(len(slice_paths)))
        filter_graph = f"{filter_parts}concat=n={len(slice_paths)}:v=0:a=1[out]"
        subprocess.run(
            [
                "ffmpeg", "-y",
                *inputs,
                "-filter_complex", filter_graph,
                "-map", "[out]",
                "-ar", "16000",
                "-ac", "1",
                "-sample_fmt", "s16",
                "-f", "wav",
                str(dst_path),
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
    finally:
        try:
            manifest_path.unlink()
        except OSError:
            pass


def _copy_file(src: Path, dst: Path) -> None:
    """Atomic-ish copy: write to tmp then rename so partial writes don't
    create a half-valid destination on crash."""
    import shutil
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copyfile(src, tmp)
    tmp.replace(dst)


# ---------------------------------------------------------------------------
# Candidate-range grouping — same semantics as the original pydub version,
# just walks timestamps directly instead of loading audio.
# ---------------------------------------------------------------------------


def _build_candidate_ranges(
    source_path: Path,
    speaker_lines: list[TranscriptLine],
) -> list[_SpeakerCandidate]:
    """Group contiguous loud-enough speaker_lines into candidate ranges.

    Matches the pre-rewrite logic:
      - Drop lines whose RMS is at or below LOW_VOLUME_THRESHOLD_DBFS
      - Join lines whose gap is ≤ MAX_SEGMENT_JOIN_GAP_MS into a single range
      - Zero/negative-duration lines skipped silently

    RMS is probed via streaming ffmpeg astats per-line — O(1) memory."""
    candidates: list[_SpeakerCandidate] = []
    current_intervals: list[tuple[int, int]] = []
    previous_end_ms: int | None = None

    def flush() -> None:
        nonlocal current_intervals
        if current_intervals:
            total = sum(max(0, e - s) for s, e in current_intervals)
            if total > 0:
                candidates.append(_SpeakerCandidate(intervals=list(current_intervals)))
        current_intervals = []

    for line in sorted(speaker_lines, key=lambda item: (item.start_ms, item.index)):
        duration_ms = max(0, line.end_ms - line.start_ms)
        if duration_ms <= 0:
            continue

        rms_db = _ffmpeg_probe_rms_db(source_path, line.start_ms, line.end_ms)
        if rms_db is None or rms_db <= LOW_VOLUME_THRESHOLD_DBFS:
            # Silent / too quiet / probe failed — skip, same as the old
            # `_safe_dbfs(clip) <= LOW_VOLUME_THRESHOLD_DBFS` skip path.
            continue

        should_join_current = (
            previous_end_ms is not None
            and line.start_ms >= previous_end_ms
            and (line.start_ms - previous_end_ms) <= MAX_SEGMENT_JOIN_GAP_MS
            and bool(current_intervals)
        )
        if not should_join_current:
            flush()
        current_intervals.append((line.start_ms, line.end_ms))
        previous_end_ms = line.end_ms

    flush()
    return candidates
