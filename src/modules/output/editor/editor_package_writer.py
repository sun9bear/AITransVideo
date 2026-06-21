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

from modules.output.editor.editor_package_models import (
    ALIGNMENT_METHOD_LABELS,
    AlignedSegment,
    ProjectOutput,
    ProjectOutputResult,
)
from modules.output.editor.loudness_matcher import match_segment_loudness_to_source
from utils.audio_fit import fit_audio_to_slot

logger = logging.getLogger(__name__)


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
            if segment.alignment_method not in {"capped_dsp_overflow", "keep_original"}:
                self._fit_segment_audio_to_slot(destination_path, segment)
            copied_path = Path(self._ensure_jianying_compatible(str(destination_path))).resolve(strict=False)
            if not copied_path.exists():
                raise FileNotFoundError(f"导出分段音频失败：{copied_path}")
            copied_paths[segment.segment_id] = str(copied_path)
        match_segment_loudness_to_source(
            project_dir=Path(output.output_dir).resolve(strict=False),
            segments=self._sorted_segments(output),
            segment_paths=copied_paths,
            output_root=self._resolve_output_root(output),
        )
        return copied_paths

    def _compose_full_audio(self, output: ProjectOutput, segment_paths: dict[int, str]) -> str:
        """Compose the complete dubbed audio by streaming segments via
        ffmpeg's amix/adelay graph.

        Used to fall back to a pydub overlay path when ffmpeg was
        unavailable. That fallback was a silent 3h-duration AudioSegment
        + per-segment overlays in memory — 4-6 GB RSS on long inputs →
        OOM. Ffmpeg is a hard dependency elsewhere in the pipeline
        (download → audio extraction, alignment, render), so if it's
        truly missing the task can't succeed at all; prefer a clear
        error here over a memory-unsafe fallback.
        """
        output_root = self._resolve_output_root(output)
        output_root.mkdir(parents=True, exist_ok=True)
        output_path = output_root / "dubbed_audio_complete.wav"
        try:
            self._compose_full_audio_with_ffmpeg(output, segment_paths, output_path)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ffmpeg is required to compose dubbed audio but was not found "
                "on PATH. Install ffmpeg (same binary the rest of the pipeline "
                "uses)."
            ) from exc

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
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            # Same reasoning as _compose_full_audio — the pydub fallback
            # here was a silent AudioSegment of the full duration (up to
            # ~1 GB for 3h stereo 44.1k) and OOM'd on long jobs. ffmpeg
            # is a hard dependency across the pipeline; prefer a clear
            # failure to a memory-unsafe fallback.
            raise RuntimeError(
                "ffmpeg is required to synthesize silent ambient audio but "
                "failed. Install / verify ffmpeg on PATH."
            ) from exc
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
        """Peak-normalize the dubbed audio in place, equivalent to
        ``pydub.effects.normalize(audio, headroom=1.0)`` but via ffmpeg
        so multi-hour inputs don't balloon RSS.

        2026-04-20 OOM fix: the old pydub path loaded the full WAV (~1 GB
        raw PCM for 3h stereo 44.1k), cloned for `normalize`, hitting the
        7.6 GB container limit on long jobs. ffmpeg's ``volumedetect``
        filter streams a single pass to emit max_volume; if normalization
        is needed, a second streaming pass applies ``volume=<gain> dB``.
        Python memory stays O(1).

        Target: peak at -1.0 dBFS (headroom=1.0). Skip if audio is silent,
        already close to peak (max ≥ -3 dBFS), or volumedetect fails —
        matches the original pydub skip conditions.
        """
        if not output_path.exists():
            return

        max_db = _ffmpeg_max_volume_db(output_path)
        if max_db is None:
            # volumedetect couldn't decide — leave audio untouched, same as
            # the old path's "rms == 0 or max_dBFS == -inf" early-return.
            return
        if max_db >= -3.0:
            return  # already loud enough, no normalization needed

        target_peak_db = -1.0  # headroom=1.0
        gain_db = target_peak_db - max_db
        if gain_db <= 0.0:
            return  # guard — shouldn't happen given max_db check above

        # Write normalized output to a temp file and swap atomically
        # (don't corrupt the source on ffmpeg failure / crash).
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", str(output_path),
                    "-af", f"volume={gain_db:.2f}dB",
                    "-sample_fmt", "s16",
                    "-f", "wav",
                    str(tmp_path),
                ],
                check=True,
                capture_output=True,
                timeout=1800,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            # Leave source intact on failure; loudness mismatch is a cosmetic
            # issue, not a blocker for delivery.
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            logger.warning(
                "ffmpeg normalize failed for %s; keeping original",
                output_path,
            )
            return
        tmp_path.replace(output_path)
        print("[S6] 完整配音响度校正完成")

    def _write_srt(self, output: ProjectOutput) -> tuple[str, str, str]:
        """Write 3 SRT files (zh, en, bilingual) and return their paths.

        Routes to canonical-cue path when output.subtitle_cues is non-empty,
        otherwise falls back to the segment-based path.

        Also writes subtitles.srt as a copy of zh for backward compatibility.
        Returns (zh_path, en_path, bilingual_path).

        2026-04-21 plan §12 / D8 contract — **do not add caching here**.
        This method is the single convergence point for commit-triggered
        subtitle regeneration: when the user edits segment.cn_text /
        segment.source_text in the editing UI and commits (overwrite or
        copy_as_new), the pipeline resumes at STAGE_ALIGNMENT, which flows
        straight through OutputDispatcher into this writer. Skipping the
        write on any "nothing changed" heuristic would silently leak stale
        subtitles into the downloaded SRT and is explicitly forbidden by
        the plan's §12 guarantee.

        Plan: subtitle-cue-generation-v2 (T8, Phase 1a).
        """
        if output.subtitle_cues:
            return self._write_srt_from_canonical_cues(output)
        return self._write_srt_from_segments(output)

    def _write_srt_from_canonical_cues(self, output: ProjectOutput) -> tuple[str, str, str]:
        """Write 3 SRT files using canonical SubtitleCue list via T7 srt_writer.

        Canonical path: output.subtitle_cues is non-empty. Calls
        write_zh_srt / write_en_srt / write_bilingual_srt from
        modules.subtitles.srt_writer and persists the resulting SRT strings
        to the same filenames as the segment-based path.

        Plan: subtitle-cue-generation-v2 (T8, Phase 1a).
        """
        from modules.subtitles.srt_writer import write_bilingual_srt, write_en_srt, write_zh_srt

        output_root = self._resolve_output_root(output)
        output_root.mkdir(parents=True, exist_ok=True)

        zh_text = write_zh_srt(output.subtitle_cues)
        en_text = write_en_srt(output.subtitle_cues)
        bilingual_text = write_bilingual_srt(output.subtitle_cues)

        zh_path = self._write_srt_text_to_file(zh_text, output_path=output_root / "subtitles_zh.srt")
        en_path = self._write_srt_text_to_file(en_text, output_path=output_root / "subtitles_en.srt")
        bilingual_path = self._write_srt_text_to_file(
            bilingual_text, output_path=output_root / "subtitles_bilingual.srt"
        )

        # Backward compat: subtitles.srt = the zh file, which is cue.text == the dub
        # (TARGET) language regardless of pair (the "zh" name is legacy). So this stays
        # the target subtitle for non-default pairs too — byte-identical for en->zh.
        compat_path = output_root / "subtitles.srt"
        shutil.copy2(zh_path, compat_path)

        # PR-F: script-neutral source/target SRT (see _write_source_target_srt_copies).
        self._write_source_target_srt_copies(output_root, zh_path, en_path)

        return (zh_path, en_path, bilingual_path)

    def _write_srt_from_segments(self, output: ProjectOutput) -> tuple[str, str, str]:
        """Write 3 SRT files using the legacy segment-based slice path.

        Fallback path used when output.subtitle_cues is empty. Builds
        subtitle slices from AlignedSegment objects via _build_subtitle_slices.

        Plan: subtitle-cue-generation-v2 (T8, Phase 1a) — kept as Phase 1a
        fallback; main path is _write_srt_from_canonical_cues.
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

        # Backward compat: subtitles.srt = the zh file, which is cue.text == the dub
        # (TARGET) language regardless of pair (the "zh" name is legacy). So this stays
        # the target subtitle for non-default pairs too — byte-identical for en->zh.
        compat_path = output_root / "subtitles.srt"
        shutil.copy2(zh_path, compat_path)

        # PR-F: script-neutral source/target SRT (see _write_source_target_srt_copies).
        self._write_source_target_srt_copies(output_root, zh_path, en_path)

        return (zh_path, en_path, bilingual_path)

    @staticmethod
    def _write_source_target_srt_copies(
        output_root: Path, zh_path: str, en_path: str
    ) -> tuple[str, str]:
        """PR-F: write script-neutral subtitles_source.srt / subtitles_target.srt.

        Cue text is always the dub (TARGET) language and en_text always the SOURCE,
        so the zh file always holds the target subtitle and the en file the source —
        regardless of language pair. We mirror them under script-neutral names so
        non-default pairs (e.g. zh->en, where the legacy "zh"/"en" filenames no longer
        describe the content) expose the correct language to downstream consumers.
        For the GA default (en->zh) these are byte-identical duplicates.

        Returns (source_path, target_path).
        """
        target_path = output_root / "subtitles_target.srt"
        source_path = output_root / "subtitles_source.srt"
        shutil.copy2(zh_path, target_path)  # zh file == cue.text == TARGET
        shutil.copy2(en_path, source_path)  # en file == cue.en_text == SOURCE
        return (str(source_path), str(target_path))

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

    def _write_srt_text_to_file(self, srt_text: str, *, output_path: Path) -> str:
        """Write a pre-formatted SRT string to output_path and return the resolved path.

        Used by _write_srt_from_canonical_cues. Accepts the already-serialized
        SRT string produced by T7 srt_writer functions (write_zh_srt /
        write_en_srt / write_bilingual_srt) and persists it verbatim.

        Plan: subtitle-cue-generation-v2 (T8, Phase 1a).
        """
        output_path.write_text(srt_text, encoding="utf-8")
        return str(output_path.resolve(strict=False))

    def _detect_background_sounds(self, output: ProjectOutput) -> str:
        """Detect louder-than-silence content in gaps between speech
        segments — used to surface "这一段大概是掌声/笑声/音效" hints
        for manual post-edit in 剪映.

        2026-04-20 OOM fix: the old path loaded the full original.wav
        (3h ≈ 1 GB raw, pydub clones took it to 4-6 GB). Replaced with
        per-gap ffmpeg ``astats`` probes — each gap is a few-second
        window, streaming subprocess reads only that slice, O(1) python
        memory. Per-gap subprocess cost is ~100 ms; a 3h task with ~300
        gaps adds ~30 s to publish — acceptable for a report file.
        """
        output_root = self._resolve_output_root(output)
        output_root.mkdir(parents=True, exist_ok=True)

        gaps = self._collect_gaps(output)
        reference_audio_path = self._resolve_background_reference_audio_path(output)

        findings: list[str] = []
        if reference_audio_path is None:
            for gap_start_ms, gap_end_ms in gaps:
                findings.append(
                    f"{self._format_gap_timestamp(gap_start_ms)} → {self._format_gap_timestamp(gap_end_ms)}  "
                    "[未检测：缺少 original.wav]"
                )
        else:
            for gap_start_ms, gap_end_ms in gaps:
                rms_db = _ffmpeg_gap_rms_db(
                    reference_audio_path, gap_start_ms, gap_end_ms,
                )
                if rms_db is None:
                    continue  # silent or probe failed
                if rms_db > -40:
                    findings.append(
                        f"{self._format_gap_timestamp(gap_start_ms)} → {self._format_gap_timestamp(gap_end_ms)}  "
                        f"[RMS: {round(rms_db)}dBFS，可能是掌声/笑声/音效]"
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
        for method in (
            "direct",
            "dsp",
            "rewrite_direct",
            "rewrite_dsp",
            "force_dsp",
            "capped_dsp_overflow",
        ):
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
        """Time-align ``wav_path`` to the segment's slot duration.

        Thin wrapper over ``utils.audio_fit.fit_audio_to_slot``; see that
        module for the trim / clamp / pad-or-truncate policy. γ-compliant
        (pure signal processing, no Gemini / TTS / SegmentAligner calls).
        """
        slot_duration_ms = max(0, int(segment.end_ms) - int(segment.start_ms))
        fit_audio_to_slot(wav_path, slot_duration_ms=slot_duration_ms)

    def _sorted_segments(self, output: ProjectOutput) -> list[AlignedSegment]:
        return sorted(output.segments, key=lambda segment: (segment.start_ms, segment.segment_id))

    def _build_subtitle_slices(self, segment: AlignedSegment) -> list[_SubtitleSlice]:
        """Build subtitle slices with zh as primary split, en following proportionally.

        DEPRECATED: Phase 1a fallback path. Main path now uses canonical SubtitleCue
        via _write_srt_from_canonical_cues. Plan: subtitle-cue-generation-v2 (T8).
        """
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
        if segment.alignment_method == "capped_dsp_overflow":
            return "短段听感保护，音频可能轻微跨段"
        if segment.alignment_method in {"rewrite_direct", "rewrite_dsp"}:
            return "Gemini重写后仍建议复查"
        if segment.alignment_method == "dsp":
            return "DSP变速，请复查节奏"
        return "已标记人工复查"


# ---------------------------------------------------------------------------
# ffmpeg streaming helpers (2026-04-20 OOM-safe rewrite of what were
# pydub full-buffer operations). All of these run one or more ffmpeg
# subprocesses that stream through the audio; python memory is O(1)
# regardless of source length.
# ---------------------------------------------------------------------------


_MAX_VOLUME_RE = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", re.IGNORECASE)
_RMS_LEVEL_RE = re.compile(r"RMS\s*level\s*dB:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def _ffmpeg_max_volume_db(source_path: Path) -> float | None:
    """Return peak-level (max_volume) in dBFS, or None if probe failed /
    audio is silent. Streaming — O(1) memory regardless of input length."""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostats", "-hide_banner",
                "-i", str(source_path),
                "-af", "volumedetect",
                "-vn",
                "-f", "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    # volumedetect writes to stderr regardless of returncode
    stderr = result.stderr or ""
    match = _MAX_VOLUME_RE.search(stderr)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _ffmpeg_gap_rms_db(
    source_path: Path, start_ms: int, end_ms: int,
) -> float | None:
    """RMS level (dBFS) for the `[start_ms, end_ms]` slice of ``source_path``.

    Returns None on silence, probe failure, or zero-duration windows
    (caller should treat that as "skip this gap"). Each call is a single
    short ffmpeg invocation (typically <200 ms for a few-second slice);
    python memory is O(1)."""
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
    # astats reports -inf for silent windows — caller should skip.
    if rms == float("-inf") or rms <= -120.0:
        return None
    return rms
