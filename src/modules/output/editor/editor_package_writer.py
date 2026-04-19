from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from pydub import AudioSegment
from pydub.effects import normalize
from pydub.silence import detect_leading_silence

from modules.output.editor.editor_package_models import (
    ALIGNMENT_METHOD_LABELS,
    AlignedSegment,
    ProjectOutput,
    ProjectOutputResult,
)

logger = logging.getLogger(__name__)

# Ignore sub-10ms drift between draft wav and slot — a bit-identical
# no-op avoids the pointless ffmpeg round-trip (and its re-encode).
_FIT_TOLERANCE_MS = 10

# TTS providers (MiniMax / VolcEngine / CosyVoice) pad output with 50-200ms
# of silence at each end. Trim that before computing the atempo ratio so
# the stretch acts on actual speech, not padding — both saves CPU and
# drops the effective ratio closer to 1.0x (better audio quality).
#
# Only applied to short segments: long segments (> _SILENCE_TRIM_MAX_MS)
# are typically multi-phrase utterances where the leading/trailing "silence"
# may include intentional pauses at the boundary; padding impact is also
# proportionally smaller. Threshold is a judgement call — 3s balances
# "safely single-phrase" against "still benefits from trim".
_SILENCE_THRESHOLD_DBFS = -40.0       # below this is considered silence
_SILENCE_CHUNK_MS = 10                # granularity of the scan
_SILENCE_TRIM_MAX_MS = 3_000          # audio longer than this skips silence trim
# Defensive cap: if trimming would leave <5% of original audio, something
# is wrong with the input (all-silence TTS / bug) — keep original to let
# the stretch fail loudly instead of producing empty output.
_MIN_KEEP_RATIO_AFTER_TRIM = 0.05


def _trim_silence_edges(audio: AudioSegment) -> AudioSegment:
    """Strip leading + trailing silence from an AudioSegment.

    Uses ``pydub.silence.detect_leading_silence`` applied twice (forward
    for leading, reversed for trailing) with a conservative -40dB
    threshold at 10ms granularity. Returns the original audio unchanged
    if trimming would cut more than 95% (defensive: all-silence input
    → let downstream stretch decide / fail loudly rather than write an
    empty wav that ffmpeg can't operate on).
    """
    if len(audio) == 0:
        return audio
    leading_ms = detect_leading_silence(
        audio,
        silence_threshold=_SILENCE_THRESHOLD_DBFS,
        chunk_size=_SILENCE_CHUNK_MS,
    )
    trailing_ms = detect_leading_silence(
        audio.reverse(),
        silence_threshold=_SILENCE_THRESHOLD_DBFS,
        chunk_size=_SILENCE_CHUNK_MS,
    )
    keep_len = len(audio) - leading_ms - trailing_ms
    if keep_len <= 0 or keep_len < len(audio) * _MIN_KEEP_RATIO_AFTER_TRIM:
        return audio
    return audio[leading_ms:len(audio) - trailing_ms]


def _build_atempo_filter(speed_ratio: float) -> str:
    """Build a multi-stage atempo filter string for arbitrary ratios.

    ffmpeg's ``atempo`` natively supports [0.5, 2.0]; outside that range
    we chain stages (each 0.5 or 2.0) until the remaining factor is
    in-range. Mirrors ``services.alignment.aligner._build_atempo_filter``
    — duplicated (not imported) to avoid a modules/output → services/
    circular import.

    Examples: 0.25x → "atempo=0.5,atempo=0.5"; 4x → "atempo=2,atempo=2".
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


@dataclass(slots=True)
class _SubtitleSlice:
    """A single subtitle cue with both zh and en text sharing the same time range."""
    start_ms: int
    end_ms: int
    zh_text: str
    en_text: str


# Punctuation to strip from display text
_ZH_PUNCT = re.compile(r"[，。！？；：、…\.\!\?\,\;\:\"\"\'\'\(\)\[\]【】《》]")
_EN_PUNCT = re.compile(r"[,\.\!\?\;\:\"\'\(\)\[\]]")
# Split boundaries for Chinese
_ZH_SPLIT_PATTERN = re.compile(r"(?<=[，。！？；：、…])")


class EditorPackageWriter:
    """Write the editor-facing project deliverables for a dubbing run."""

    _MAX_ZH_CHARS = 18
    _MAX_EN_CHARS = 60
    _MIN_SUBTITLE_DURATION_MS = 600
    # Legacy constants kept for backward compat with _split_subtitle_text
    _MAX_SUBTITLE_CHARS = 40

    def write(self, output: ProjectOutput) -> ProjectOutputResult:
        output_root = self._resolve_output_root(output)
        output_root.mkdir(parents=True, exist_ok=True)

        segment_paths = self._copy_segment_files(output)
        dubbed_audio_path = self._compose_full_audio(output, segment_paths)
        ambient_audio_path = self._export_ambient_audio(output)
        subtitles_zh_path, subtitles_en_path, subtitles_bilingual_path = self._write_srt(output)
        background_sounds_path = self._detect_background_sounds(output)
        alignment_report_path = self._write_alignment_report(output)

        return ProjectOutputResult(
            dubbed_audio_path=dubbed_audio_path,
            ambient_audio_path=ambient_audio_path,
            segments_dir=str((output_root / "segments").resolve(strict=False)),
            segment_count=len(output.segments),
            subtitles_path=subtitles_zh_path,
            subtitles_en_path=subtitles_en_path,
            subtitles_bilingual_path=subtitles_bilingual_path,
            background_sounds_path=background_sounds_path,
            alignment_report_path=alignment_report_path,
            needs_review_count=sum(1 for segment in output.segments if segment.needs_review),
        )

    def _copy_segment_files(self, output: ProjectOutput) -> dict[int, str]:
        segments_root = self._resolve_output_root(output) / "segments"
        segments_root.mkdir(parents=True, exist_ok=True)

        copied_paths: dict[int, str] = {}
        for segment in self._sorted_segments(output):
            source_path = Path(segment.aligned_audio_path).resolve(strict=False)
            if not source_path.exists():
                raise FileNotFoundError(f"缺少对齐后音频文件：{source_path}")
            speaker_dir = segments_root / segment.speaker_id
            speaker_dir.mkdir(parents=True, exist_ok=True)
            destination_path = speaker_dir / (
                f"segment_{segment.segment_id:03d}_"
                f"{self._format_filename_timestamp(segment.start_ms)}_"
                f"{self._format_filename_timestamp(segment.end_ms)}.wav"
            )
            shutil.copy2(source_path, destination_path)
            self._fit_segment_audio_to_slot(destination_path, segment)
            copied_path = Path(self._ensure_jianying_compatible(str(destination_path))).resolve(strict=False)
            if not copied_path.exists():
                raise FileNotFoundError(f"导出分段音频失败：{copied_path}")
            copied_paths[segment.segment_id] = str(copied_path)
        return copied_paths

    def _compose_full_audio(self, output: ProjectOutput, segment_paths: dict[int, str]) -> str:
        output_root = self._resolve_output_root(output)
        output_root.mkdir(parents=True, exist_ok=True)
        output_path = output_root / "dubbed_audio_complete.wav"
        try:
            self._compose_full_audio_with_ffmpeg(output, segment_paths, output_path)
        except FileNotFoundError:
            full_audio = (
                AudioSegment.silent(
                    duration=output.total_duration_ms,
                    frame_rate=44_100,
                )
                .set_channels(2)
                .set_sample_width(2)
            )
            for segment in self._sorted_segments(output):
                dubbed = AudioSegment.from_wav(segment_paths[segment.segment_id])
                full_audio = full_audio.overlay(dubbed, position=segment.start_ms)
            full_audio.export(output_path, format="wav")

        self._normalize_full_output_audio(output_path)
        return self._ensure_jianying_compatible(str(output_path))

    def _export_ambient_audio(self, output: ProjectOutput) -> str:
        output_root = self._resolve_output_root(output)
        output_root.mkdir(parents=True, exist_ok=True)
        ambient_source_path = Path(output.output_dir).resolve(strict=False) / "audio" / "ambient.wav"
        output_path = output_root / "ambient_audio.wav"

        if ambient_source_path.exists():
            shutil.copy2(ambient_source_path, output_path)
            return self._ensure_jianying_compatible(str(output_path))

        duration_seconds = max(output.total_duration_ms, 0) / 1000
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-f", "lavfi",
                    "-t", f"{duration_seconds:.3f}",
                    "-i", "anullsrc=r=44100:cl=stereo",
                    "-acodec", "pcm_s16le",
                    "-ar", "44100", "-ac", "2",
                    "-y", str(output_path),
                ],
                capture_output=True, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            # Fallback to pydub if ffmpeg unavailable
            ambient_audio = (
                AudioSegment.silent(duration=output.total_duration_ms, frame_rate=44_100)
                .set_channels(2)
                .set_sample_width(2)
            )
            ambient_audio.export(output_path, format="wav")
        return str(output_path.resolve(strict=False))

    def _compose_full_audio_with_ffmpeg(
        self,
        output: ProjectOutput,
        segment_paths: dict[int, str],
        output_path: Path,
    ) -> None:
        duration_seconds = max(output.total_duration_ms, 0) / 1000
        command = [
            "ffmpeg",
            "-f",
            "lavfi",
            "-t",
            f"{duration_seconds:.3f}",
            "-i",
            "anullsrc=r=44100:cl=stereo",
        ]

        segments = self._sorted_segments(output)
        for segment in segments:
            command.extend(["-i", segment_paths[segment.segment_id]])

        filter_lines = ["[0:a]aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=stereo[base]"]
        mix_inputs = ["[base]"]
        for input_index, segment in enumerate(segments, start=1):
            delay_ms = max(segment.start_ms, 0)
            filter_lines.append(
                f"[{input_index}:a]"
                "aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=stereo,"
                f"adelay={delay_ms}|{delay_ms}[seg{input_index}]"
            )
            mix_inputs.append(f"[seg{input_index}]")

        if mix_inputs == ["[base]"]:
            filter_lines.append("[base]anull[mix]")
        else:
            filter_lines.append(
                "".join(mix_inputs)
                + (
                    f"amix=inputs={len(mix_inputs)}:duration=first:"
                    "dropout_transition=0:normalize=0[mix]"
                )
            )

        filter_script_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".ffmpeg-filter",
                prefix="compose_",
                dir=output_path.parent,
                delete=False,
            ) as handle:
                filter_script_path = Path(handle.name).resolve(strict=False)
                handle.write(";\n".join(filter_lines))

            command.extend(
                [
                    "-filter_complex_script",
                    str(filter_script_path),
                    "-map",
                    "[mix]",
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    "-y",
                    str(output_path),
                ]
            )
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            if filter_script_path is not None:
                filter_script_path.unlink(missing_ok=True)

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(f"ffmpeg 合成完整配音失败：{stderr}")

    def _normalize_full_output_audio(self, output_path: Path) -> None:
        if not output_path.exists():
            return

        audio = AudioSegment.from_wav(output_path)
        if audio.rms == 0 or audio.max_dBFS == float("-inf") or audio.max_dBFS >= -3.0:
            return

        normalized_audio = normalize(audio, headroom=1.0)
        normalized_audio.export(output_path, format="wav")
        print("[S6] 完整配音响度校正完成")

    def _write_srt(self, output: ProjectOutput) -> tuple[str, str, str]:
        """Write 3 SRT files (zh, en, bilingual) and return their paths.

        Also writes subtitles.srt as a copy of zh for backward compatibility.
        Returns (zh_path, en_path, bilingual_path).
        """
        output_root = self._resolve_output_root(output)
        output_root.mkdir(parents=True, exist_ok=True)

        # Build all subtitle slices from segments
        all_slices: list[_SubtitleSlice] = []
        for segment in self._sorted_segments(output):
            all_slices.extend(self._build_subtitle_slices(segment))

        # Write 3 SRT variants
        zh_path = self._write_srt_file(
            all_slices, lang="zh", output_path=output_root / "subtitles_zh.srt"
        )
        en_path = self._write_srt_file(
            all_slices, lang="en", output_path=output_root / "subtitles_en.srt"
        )
        bilingual_path = self._write_srt_file(
            all_slices, lang="bilingual", output_path=output_root / "subtitles_bilingual.srt"
        )

        # Backward compat: subtitles.srt = zh copy
        compat_path = output_root / "subtitles.srt"
        shutil.copy2(zh_path, compat_path)

        return (zh_path, en_path, bilingual_path)

    def _write_srt_file(
        self, slices: list[_SubtitleSlice], *, lang: str, output_path: Path
    ) -> str:
        blocks: list[str] = []
        for idx, s in enumerate(slices, start=1):
            if lang == "zh":
                text = s.zh_text
            elif lang == "en":
                text = s.en_text
            else:  # bilingual
                text = f"{s.en_text}\n{s.zh_text}"
            if not text.strip():
                continue
            blocks.append(
                f"{idx}\n"
                f"{self._format_srt_timestamp(s.start_ms)} --> "
                f"{self._format_srt_timestamp(s.end_ms)}\n"
                f"{text}"
            )
        serialized = "\n\n".join(blocks)
        if serialized:
            serialized += "\n"
        output_path.write_text(serialized, encoding="utf-8")
        return str(output_path.resolve(strict=False))

    def _detect_background_sounds(self, output: ProjectOutput) -> str:
        output_root = self._resolve_output_root(output)
        output_root.mkdir(parents=True, exist_ok=True)

        gaps = self._collect_gaps(output)
        reference_audio_path = self._resolve_background_reference_audio_path(output)
        original_audio = AudioSegment.from_wav(reference_audio_path) if reference_audio_path is not None else None

        findings: list[str] = []
        if original_audio is None:
            for gap_start_ms, gap_end_ms in gaps:
                findings.append(
                    f"{self._format_gap_timestamp(gap_start_ms)} → {self._format_gap_timestamp(gap_end_ms)}  "
                    "[未检测：缺少 original.wav]"
                )
        else:
            for gap_start_ms, gap_end_ms in gaps:
                gap_audio = original_audio[gap_start_ms:gap_end_ms]
                if gap_audio.rms == 0:
                    continue
                gap_dbfs = gap_audio.dBFS
                if gap_dbfs > -40:
                    findings.append(
                        f"{self._format_gap_timestamp(gap_start_ms)} → {self._format_gap_timestamp(gap_end_ms)}  "
                        f"[RMS: {round(gap_dbfs)}dBFS，可能是掌声/笑声/音效]"
                    )

        lines = [
            "背景声检测报告",
            "==============",
            "建议：对原视频音轨整体静音后，单独恢复以下片段的音量。",
            "",
            "检测到背景声的片段：",
        ]
        if findings:
            lines.extend(findings)
        else:
            lines.append("（如未检测到背景声，此处为空）")
        lines.extend(
            [
                "",
                "操作方法：",
                '在剪映中选中原视频音轨 → 点击"分割" → 只对上述时间段恢复音量 → 其余部分保持静音。',
            ]
        )

        output_path = output_root / "background_sounds.txt"
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(output_path.resolve(strict=False))

    def _resolve_background_reference_audio_path(self, output: ProjectOutput) -> Path | None:
        audio_root = Path(output.output_dir).resolve(strict=False) / "audio"
        ambient_audio_path = audio_root / "ambient.wav"
        if ambient_audio_path.exists():
            return ambient_audio_path

        original_audio_path = audio_root / "original.wav"
        if original_audio_path.exists():
            return original_audio_path
        return None

    def _write_alignment_report(self, output: ProjectOutput) -> str:
        output_root = self._resolve_output_root(output)
        output_root.mkdir(parents=True, exist_ok=True)

        segments = self._sorted_segments(output)
        speaker_counts: dict[str, int] = {}
        speaker_names: dict[str, str] = {}
        for segment in segments:
            speaker_counts[segment.speaker_id] = speaker_counts.get(segment.speaker_id, 0) + 1
            speaker_names.setdefault(segment.speaker_id, segment.display_name)

        method_counts = Counter(segment.alignment_method for segment in segments)
        review_segments = [segment for segment in segments if segment.needs_review]
        total_segments = len(segments)

        lines = [
            "对齐质量报告",
            "============",
            f"视频：{output.video_title}",
            f"总段数：{total_segments}段",
        ]
        for speaker_id in sorted(speaker_counts):
            speaker_label = self._format_speaker_label(speaker_id)
            display_name = speaker_names[speaker_id]
            lines.append(f"{speaker_label}（{display_name}）：{speaker_counts[speaker_id]}段")

        lines.extend(["", "对齐方式统计："])
        for method in ("direct", "dsp", "rewrite_direct", "rewrite_dsp", "force_dsp"):
            count = method_counts.get(method, 0)
            percentage = round((count / total_segments) * 100) if total_segments else 0
            lines.append(f"  {ALIGNMENT_METHOD_LABELS[method]}：{count}段（{percentage}%）")

        lines.append("")
        if review_segments:
            lines.append(f"⚠️ 需要手工检查的段落（共{len(review_segments)}段）：")
            for segment in review_segments:
                lines.append(
                    "  "
                    f"segment_{segment.segment_id:03d}  "
                    f"{self._format_speaker_label(segment.speaker_id)}  "
                    f"{self._format_clock_timestamp(segment.start_ms)} → {self._format_clock_timestamp(segment.end_ms)}  "
                    f"[{self._build_review_reason(segment)}]"
                )
        else:
            lines.append("全部自动对齐完成")

        output_path = output_root / "alignment_report.txt"
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(output_path.resolve(strict=False))

    def _resolve_output_root(self, output: ProjectOutput) -> Path:
        return Path(output.output_dir).resolve(strict=False) / "output"

    def _ensure_jianying_compatible(self, wav_path: str) -> str:
        source_path = Path(wav_path).resolve(strict=False)
        if not source_path.exists():
            return str(source_path)

        try:
            probe_result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "a:0",
                    "-show_entries", "stream=sample_rate,channels,bits_per_sample",
                    "-of", "json",
                    str(source_path),
                ],
                capture_output=True, text=True, check=True,
            )
            stream = json.loads(probe_result.stdout).get("streams", [{}])[0]
            if (
                str(stream.get("sample_rate")) == "44100"
                and int(stream.get("channels", 0)) == 2
                and int(stream.get("bits_per_sample", 0)) == 16
            ):
                return str(source_path)
        except Exception:
            pass

        temp_source_path = source_path.with_name(f"{source_path.stem}.__jianying_source__{source_path.suffix}")
        if temp_source_path.exists():
            temp_source_path.unlink()

        shutil.move(str(source_path), str(temp_source_path))
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    str(temp_source_path),
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    "-y",
                    str(source_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            shutil.move(str(temp_source_path), str(source_path))
            print(f"[警告] 未找到 ffmpeg，保留原始 WAV：{source_path}")
            return str(source_path)

        if result.returncode != 0:
            if source_path.exists():
                source_path.unlink()
            shutil.move(str(temp_source_path), str(source_path))
            stderr = (result.stderr or "").strip()
            print(f"[警告] ffmpeg 转码失败，保留原始 WAV：{source_path} {stderr}")
            return str(source_path)

        temp_source_path.unlink(missing_ok=True)
        return str(source_path)

    def _fit_segment_audio_to_slot(
        self, wav_path: Path, segment: AlignedSegment,
    ) -> None:
        """Time-stretch ``wav_path`` to exactly the segment's slot duration
        via ffmpeg atempo — bidirectional, no trim, no silent padding.

        Replaces the old ``_trim_segment_audio_to_slot`` (2026-04-19 γ
        incident): the trim variant only handled "audio too long" by
        truncation, leaving short audio with silent gaps in the dubbed
        output. For γ publish-only resume — where the user-regenerated
        TTS rarely matches slot duration — both directions need DSP.

        ffmpeg atempo chains to support extreme ratios (>2x, <0.5x). At
        those extremes audio quality degrades ("chipmunk" / "slow-mo"),
        but the output is a valid wav at the target duration. Users will
        review segment-level playback in the planned test-playback UI
        and re-edit text if a segment sounds off.

        γ-compliance: this is pure audio signal processing, no Gemini /
        no TTS generation / no SegmentAligner. Safe to run inside the
        publish stage that γ dispatches.
        """
        slot_duration_ms = max(0, int(segment.end_ms) - int(segment.start_ms))
        if slot_duration_ms <= 0 or not wav_path.exists():
            return

        try:
            audio = AudioSegment.from_wav(wav_path)
        except Exception:  # unreadable / not a wav — leave untouched
            return
        actual_duration_ms = len(audio)
        if actual_duration_ms <= 0:
            return

        # Step 1 — silence-edge trim (short segments only). TTS outputs
        # padding at both ends; stretching the padded wav burns the atempo
        # budget on silence. Long segments (>3s) skip this step: their
        # leading/trailing quiet may be intentional multi-phrase pauses
        # rather than pure TTS padding.
        if actual_duration_ms <= _SILENCE_TRIM_MAX_MS:
            trimmed = _trim_silence_edges(audio)
            trimmed_duration_ms = len(trimmed)
            if (
                0 < trimmed_duration_ms < actual_duration_ms
                and trimmed_duration_ms != actual_duration_ms
            ):
                # Persist trimmed wav in place BEFORE atempo stage — the
                # ffmpeg call below will re-read this wav, and atempo works
                # off the shortened duration for a better (closer-to-1.0x)
                # ratio. Use .replace for hardlink-safety.
                trim_tmp = wav_path.with_name(
                    f".{wav_path.stem}.trimmed.wav"
                )
                trimmed.export(trim_tmp, format="wav")
                trim_tmp.replace(wav_path)
                actual_duration_ms = trimmed_duration_ms

        if abs(actual_duration_ms - slot_duration_ms) <= _FIT_TOLERANCE_MS:
            return  # within tolerance — preserve bit-identity

        speed_ratio = actual_duration_ms / slot_duration_ms
        filter_value = _build_atempo_filter(speed_ratio)

        if speed_ratio < 0.5 or speed_ratio > 2.0:
            logger.warning(
                "segment %s: atempo stretch ratio=%.2fx "
                "(actual=%dms → slot=%dms) exceeds the quality-safe "
                "[0.5x, 2.0x] window; output wav is valid at target "
                "duration but audio quality degrades — user reviews "
                "in test-playback UI and re-edits if unhappy (方案 A, "
                "γ publish-only resume契约)",
                segment.segment_id, speed_ratio,
                actual_duration_ms, slot_duration_ms,
            )

        # Keep .wav suffix so ffmpeg auto-detects output format; prefix
        # with a dot so we can identify it as a tmp artifact.
        stretched_path = wav_path.with_name(f".{wav_path.stem}.stretched.wav")
        command = [
            "ffmpeg",
            "-i", str(wav_path),
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
        except FileNotFoundError:  # no ffmpeg in PATH
            logger.warning(
                "segment %s: ffmpeg not in PATH, skipping atempo stretch",
                segment.segment_id,
            )
            if stretched_path.exists():
                stretched_path.unlink()
            return

        if completed.returncode != 0 or not stretched_path.exists():
            logger.warning(
                "segment %s: atempo stretch failed (rc=%s stderr=%r); "
                "leaving original wav untouched",
                segment.segment_id, completed.returncode,
                (completed.stderr or "")[:200],
            )
            if stretched_path.exists():
                stretched_path.unlink()
            return

        # Atomic replace — preserves inode semantics (important if wav
        # is still hardlinked to source in copy_as_new target).
        stretched_path.replace(wav_path)

    def _sorted_segments(self, output: ProjectOutput) -> list[AlignedSegment]:
        return sorted(output.segments, key=lambda segment: (segment.start_ms, segment.segment_id))

    def _build_subtitle_slices(self, segment: AlignedSegment) -> list[_SubtitleSlice]:
        """Build subtitle slices with zh as primary split, en following proportionally."""
        zh_raw = segment.cn_text.strip()
        en_raw = (segment.en_text or "").strip()
        if not zh_raw:
            return []

        # 1. Split Chinese text on punctuation boundaries
        zh_chunks = self._split_zh_short_sentences(zh_raw)
        if not zh_chunks:
            zh_chunks = [_ZH_PUNCT.sub("", zh_raw)]

        # 2. Merge chunks that would be too short in duration
        zh_chunks = self._merge_short_subtitle_chunks(
            zh_chunks, segment.end_ms - segment.start_ms
        )

        # 3. Strip punctuation from zh display
        zh_chunks = [_ZH_PUNCT.sub("", c).strip() for c in zh_chunks]
        zh_chunks = [c for c in zh_chunks if c]
        if not zh_chunks:
            return []

        # 4. Split English into same number of slices by word count
        en_chunks = self._split_en_to_match(en_raw, len(zh_chunks))

        # 5. Distribute time proportionally by zh char count
        slices = self._distribute_time(
            zh_chunks, en_chunks, segment.start_ms, segment.end_ms
        )
        return slices

    def _split_zh_short_sentences(self, text: str) -> list[str]:
        """Split Chinese text into short sentences on punctuation boundaries."""
        parts = _ZH_SPLIT_PATTERN.split(text)
        parts = [p.strip() for p in parts if p.strip()]

        # Second pass: split chunks > _MAX_ZH_CHARS
        result: list[str] = []
        for part in parts:
            clean_len = len(_ZH_PUNCT.sub("", part))
            if clean_len <= self._MAX_ZH_CHARS:
                result.append(part)
            else:
                # Hard split at max chars
                stripped = _ZH_PUNCT.sub("", part)
                for i in range(0, len(stripped), self._MAX_ZH_CHARS):
                    chunk = stripped[i:i + self._MAX_ZH_CHARS]
                    if chunk:
                        result.append(chunk)
        return result

    def _split_en_to_match(self, en_text: str, n_slices: int) -> list[str]:
        """Split English text into exactly n_slices by distributing words evenly."""
        if n_slices <= 0:
            return []
        clean = _EN_PUNCT.sub("", en_text).strip()
        if not clean:
            return [""] * n_slices
        words = clean.split()
        if not words:
            return [""] * n_slices
        if n_slices == 1:
            return [" ".join(words)]

        # Distribute words proportionally
        total = len(words)
        chunks: list[str] = []
        cursor = 0
        for i in range(n_slices):
            # How many words for this slice
            share = round(total * (i + 1) / n_slices) - cursor
            share = max(share, 1 if cursor < total else 0)
            end = min(cursor + share, total)
            chunks.append(" ".join(words[cursor:end]))
            cursor = end
        # Pad if we somehow ended up short
        while len(chunks) < n_slices:
            chunks.append("")
        return chunks[:n_slices]

    def _distribute_time(
        self,
        zh_chunks: list[str],
        en_chunks: list[str],
        start_ms: int,
        end_ms: int,
    ) -> list[_SubtitleSlice]:
        """Distribute time range across slices proportionally by zh char count."""
        weights = [len(c) or 1 for c in zh_chunks]
        total_weight = sum(weights) or len(zh_chunks)
        total_duration = max(end_ms - start_ms, len(zh_chunks))

        slices: list[_SubtitleSlice] = []
        cursor = start_ms
        accumulated = 0
        for i, (zh, en) in enumerate(zip(zh_chunks, en_chunks)):
            accumulated += weights[i]
            if i == len(zh_chunks) - 1:
                slice_end = end_ms
            else:
                ratio = accumulated / total_weight
                slice_end = start_ms + int(total_duration * ratio)
                slice_end = max(slice_end, cursor + 1)
                remaining = len(zh_chunks) - i - 1
                slice_end = min(slice_end, end_ms - remaining)
            slices.append(_SubtitleSlice(
                start_ms=cursor, end_ms=slice_end,
                zh_text=zh, en_text=en,
            ))
            cursor = slice_end
        return slices

    def _split_subtitle_text(self, text: str) -> list[str]:
        normalized = re.sub(r"\s+", "", text).strip()
        if not normalized:
            return []

        strong_parts = self._split_with_delimiters(normalized, r"(……|[。！？；])")
        chunks: list[str] = []
        for part in strong_parts:
            if self._subtitle_weight(part) <= self._MAX_SUBTITLE_CHARS:
                chunks.append(part)
                continue

            weak_parts = self._split_with_delimiters(part, r"([，、])")
            for weak_part in weak_parts:
                if self._subtitle_weight(weak_part) <= self._MAX_SUBTITLE_CHARS:
                    chunks.append(weak_part)
                    continue
                chunks.extend(self._hard_split_text(weak_part, self._MAX_SUBTITLE_CHARS))
        return [chunk for chunk in chunks if chunk]

    def _merge_short_subtitle_chunks(self, chunks: list[str], total_duration_ms: int) -> list[str]:
        merged = list(chunks)
        while len(merged) > 1:
            weights = [self._subtitle_weight(chunk) for chunk in merged]
            total_weight = sum(weights) or len(merged)
            durations = [
                max(1, int(total_duration_ms * ((weight or 1) / total_weight)))
                for weight in weights
            ]

            short_index = next(
                (index for index, duration in enumerate(durations) if duration < self._MIN_SUBTITLE_DURATION_MS),
                None,
            )
            if short_index is None:
                break

            if short_index == 0:
                merge_index = 1
            elif short_index == len(merged) - 1:
                merge_index = short_index - 1
            else:
                left_duration = durations[short_index - 1]
                right_duration = durations[short_index + 1]
                merge_index = short_index - 1 if left_duration <= right_duration else short_index + 1

            if merge_index < short_index:
                merged[merge_index] = merged[merge_index] + merged[short_index]
                merged.pop(short_index)
            else:
                merged[short_index] = merged[short_index] + merged[merge_index]
                merged.pop(merge_index)
        return merged

    def _split_with_delimiters(self, text: str, delimiter_pattern: str) -> list[str]:
        if not text:
            return []
        pieces = re.split(delimiter_pattern, text)
        chunks: list[str] = []
        current = ""
        for piece in pieces:
            if not piece:
                continue
            current += piece
            if re.fullmatch(delimiter_pattern, piece):
                chunks.append(current)
                current = ""
        if current:
            chunks.append(current)
        return chunks

    def _hard_split_text(self, text: str, max_chars: int) -> list[str]:
        stripped = text.strip()
        if not stripped:
            return []
        return [stripped[index:index + max_chars] for index in range(0, len(stripped), max_chars)]

    def _subtitle_weight(self, text: str) -> int:
        cleaned = re.sub(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]", "", text)
        return len(cleaned) or len(text.strip())

    def _collect_gaps(self, output: ProjectOutput) -> list[tuple[int, int]]:
        segments = self._sorted_segments(output)
        if not segments:
            return [(0, output.total_duration_ms)] if output.total_duration_ms >= 500 else []

        gaps: list[tuple[int, int]] = []
        cursor_ms = 0
        for segment in segments:
            if segment.start_ms - cursor_ms >= 500:
                gaps.append((cursor_ms, segment.start_ms))
            cursor_ms = max(cursor_ms, segment.end_ms)
        if output.total_duration_ms - cursor_ms >= 500:
            gaps.append((cursor_ms, output.total_duration_ms))
        return gaps

    def _format_filename_timestamp(self, ms: int) -> str:
        total_seconds = max(ms, 0) // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes:02d}m{seconds:02d}s"

    def _format_srt_timestamp(self, ms: int) -> str:
        total_ms = max(ms, 0)
        hours = total_ms // 3_600_000
        minutes = (total_ms % 3_600_000) // 60_000
        seconds = (total_ms % 60_000) // 1_000
        milliseconds = total_ms % 1_000
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

    def _format_gap_timestamp(self, ms: int) -> str:
        total_ms = max(ms, 0)
        total_seconds = total_ms // 1_000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        milliseconds = total_ms % 1_000
        return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

    def _format_clock_timestamp(self, ms: int) -> str:
        total_seconds = max(ms, 0) // 1_000
        hours = total_seconds // 3_600
        minutes = (total_seconds % 3_600) // 60
        seconds = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _format_speaker_label(self, speaker_id: str) -> str:
        normalized = speaker_id.strip().lower()
        if normalized.startswith("speaker_"):
            suffix = normalized.split("_", 1)[1]
            if len(suffix) == 1 and suffix.isalpha():
                return f"Speaker {suffix.upper()}"
            return f"Speaker {suffix.replace('_', ' ').title()}"
        return speaker_id

    def _build_review_reason(self, segment: AlignedSegment) -> str:
        if segment.alignment_method == "force_dsp":
            return "强制DSP，变速幅度过大"
        if segment.alignment_method in {"rewrite_direct", "rewrite_dsp"}:
            return "Gemini重写后仍建议复查"
        if segment.alignment_method == "dsp":
            return "DSP变速，请复查节奏"
        return "已标记人工复查"
