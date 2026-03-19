from __future__ import annotations

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


class VoiceSampleExtractor:
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

        source_audio = AudioSegment.from_wav(source_path)
        min_duration_ms = max(int(min_duration_s * 1000), 1)
        max_duration_ms = max(int(max_duration_s * 1000), min_duration_ms)

        all_candidates = _build_candidate_clips(source_audio, speaker_lines)
        candidates = [
            (duration_ms, clip)
            for duration_ms, clip in all_candidates
            if duration_ms >= MIN_SEGMENT_DURATION_MS
        ]
        if sum(duration_ms for duration_ms, _ in candidates) < min_duration_ms:
            candidates = [
                (duration_ms, clip)
                for duration_ms, clip in all_candidates
                if duration_ms >= FALLBACK_SEGMENT_DURATION_MS
            ]

        if not candidates:
            raise SampleExtractionError("没有可用的说话人音色样本片段。")

        candidates.sort(key=lambda item: item[0], reverse=True)

        assembled = AudioSegment.empty()
        for _, clip in candidates:
            if len(assembled) >= max_duration_ms:
                break

            remaining_ms = max_duration_ms - len(assembled)
            if remaining_ms <= 0:
                break

            clip_to_add = clip[:remaining_ms]
            if len(clip_to_add) <= 0:
                continue
            assembled += clip_to_add

        if len(assembled) <= 0:
            raise SampleExtractionError("样本提取失败，没有拼接出可用音频。")

        if len(assembled) < min_duration_ms:
            print(
                f"[S2] 警告：提取的样本仅 {round(len(assembled) / 1000, 1)} 秒，"
                f"低于建议最小时长 {round(min_duration_ms / 1000, 1)} 秒"
            )

        normalized_audio = (
            assembled.set_frame_rate(16_000)
            .set_channels(1)
            .set_sample_width(2)
        )
        normalized_audio.export(output_file, format="wav")
        return str(output_file)

    def validate_sample(self, sample_path: str) -> dict:
        path = Path(sample_path).expanduser().resolve(strict=False)
        if not path.exists():
            raise SampleExtractionError(f"样本文件不存在：{path}")

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


def _build_candidate_clips(
    source_audio: AudioSegment,
    speaker_lines: list[TranscriptLine],
) -> list[tuple[int, AudioSegment]]:
    candidates: list[tuple[int, AudioSegment]] = []
    current_duration_ms = 0
    current_clip = AudioSegment.empty()
    previous_end_ms: int | None = None

    def flush_current_clip() -> None:
        nonlocal current_duration_ms, current_clip
        if current_duration_ms > 0 and len(current_clip) > 0:
            candidates.append((current_duration_ms, current_clip))
        current_duration_ms = 0
        current_clip = AudioSegment.empty()

    for line in sorted(speaker_lines, key=lambda item: (item.start_ms, item.index)):
        duration_ms = max(0, line.end_ms - line.start_ms)
        if duration_ms <= 0:
            continue

        clip = source_audio[line.start_ms:line.end_ms]
        clip_dbfs = _safe_dbfs(clip)
        if clip_dbfs <= LOW_VOLUME_THRESHOLD_DBFS:
            continue

        should_join_current = (
            previous_end_ms is not None
            and line.start_ms >= previous_end_ms
            and (line.start_ms - previous_end_ms) <= MAX_SEGMENT_JOIN_GAP_MS
        )
        if not should_join_current:
            flush_current_clip()

        current_clip += clip
        current_duration_ms += duration_ms
        previous_end_ms = line.end_ms

    flush_current_clip()
    return candidates
