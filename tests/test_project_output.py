from pathlib import Path
import re

from pydub import AudioSegment
from pydub.generators import Sine
import pytest

from modules.output.project_output import (
    AlignedSegment,
    ProjectOutput,
    ProjectOutputWriter,
)


def _export_silent_wav(path: Path, *, duration_ms: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    AudioSegment.silent(duration=duration_ms).export(path, format="wav")
    return path


def _export_tone_wav(path: Path, *, duration_ms: int, frequency: int = 440) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Sine(frequency).to_audio_segment(duration=duration_ms).export(path, format="wav")
    return path


def _export_custom_wav(
    path: Path,
    *,
    duration_ms: int,
    frame_rate: int,
    channels: int,
    sample_width: int,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    audio = AudioSegment.silent(duration=duration_ms, frame_rate=frame_rate)
    audio = audio.set_channels(channels).set_sample_width(sample_width)
    audio.export(path, format="wav")
    return path


def _build_segment(
    *,
    segment_id: int | str,
    speaker_id: str,
    display_name: str,
    start_ms: int,
    end_ms: int,
    cn_text: str,
    en_text: str = "",
    aligned_audio_path: Path,
    alignment_method: str = "direct",
    needs_review: bool = False,
) -> AlignedSegment:
    return AlignedSegment(
        segment_id=segment_id,
        speaker_id=speaker_id,
        display_name=display_name,
        start_ms=start_ms,
        end_ms=end_ms,
        cn_text=cn_text,
        en_text=en_text,
        aligned_audio_path=str(aligned_audio_path),
        actual_duration_ms=end_ms - start_ms,
        alignment_method=alignment_method,
        needs_review=needs_review,
    )


def _build_output(
    tmp_path: Path,
    *,
    segments: list[AlignedSegment],
    total_duration_ms: int,
    project_name: str = "project_output_test",
) -> ProjectOutput:
    project_dir = tmp_path / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    return ProjectOutput(
        project_id=project_name,
        youtube_url="https://youtube.example/watch?v=demo",
        video_title="Demo Video",
        total_duration_ms=total_duration_ms,
        segments=segments,
        output_dir=str(project_dir),
    )


def _parse_srt_blocks(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []

    blocks: list[dict[str, object]] = []
    for raw_block in re.split(r"\n\s*\n", text):
        lines = raw_block.splitlines()
        if len(lines) < 3:
            continue
        blocks.append(
            {
                "index": int(lines[0]),
                "times": lines[1],
                "text": "".join(lines[2:]),
            }
        )
    return blocks


def _srt_timestamp_to_ms(value: str) -> int:
    hours, minutes, second_part = value.split(":")
    seconds, milliseconds = second_part.split(",")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(milliseconds)
    )


def test_project_output_single_speaker_creates_only_speaker_a_directory(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "speaker_a.wav", duration_ms=45_000)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=45_000,
                cn_text="今天我们来聊聊输出模块。",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=50_000,
        project_name="single_speaker",
    )

    result = ProjectOutputWriter().write(output)
    speaker_a_dir = Path(result.segments_dir) / "speaker_a"

    assert speaker_a_dir.exists()
    assert speaker_a_dir.is_dir()
    assert not (Path(result.segments_dir) / "speaker_b").exists()
    assert len(list(speaker_a_dir.glob("*.wav"))) == 1


def test_project_output_accepts_split_string_segment_ids(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "segment_70_a.wav", duration_ms=800)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id="70_a",
                speaker_id="speaker_a",
                display_name="Speaker A",
                start_ms=1_000,
                end_ms=1_800,
                cn_text="拆分后的第一段。",
                aligned_audio_path=source_audio,
                needs_review=True,
            )
        ],
        total_duration_ms=2_500,
        project_name="split_string_segment_id",
    )

    result = ProjectOutputWriter().write(output)

    exported_paths = list((Path(result.segments_dir) / "speaker_a").glob("segment_70_a_*.wav"))
    assert len(exported_paths) == 1
    assert "segment_70_a" in Path(result.alignment_report_path).read_text(encoding="utf-8")


def test_project_output_two_speakers_creates_both_directories_and_full_audio_matches_duration(
    tmp_path: Path,
) -> None:
    speaker_a_audio = _export_silent_wav(tmp_path / "sources" / "speaker_a.wav", duration_ms=1_200)
    speaker_b_audio = _export_silent_wav(tmp_path / "sources" / "speaker_b.wav", duration_ms=1_000)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=1_200,
                cn_text="第一句。",
                aligned_audio_path=speaker_a_audio,
            ),
            _build_segment(
                segment_id=2,
                speaker_id="speaker_b",
                display_name="主持人",
                start_ms=2_000,
                end_ms=3_000,
                cn_text="第二句。",
                aligned_audio_path=speaker_b_audio,
                alignment_method="dsp",
            ),
        ],
        total_duration_ms=5_000,
        project_name="dual_speaker",
    )

    result = ProjectOutputWriter().write(output)
    composed = AudioSegment.from_wav(result.dubbed_audio_path)

    assert (Path(result.segments_dir) / "speaker_a").exists()
    assert (Path(result.segments_dir) / "speaker_b").exists()
    assert len(list((Path(result.segments_dir) / "speaker_a").glob("*.wav"))) == 1
    assert len(list((Path(result.segments_dir) / "speaker_b").glob("*.wav"))) == 1
    assert len(composed) == output.total_duration_ms


def test_project_output_trims_segment_audio_to_its_time_slot_before_export(tmp_path: Path) -> None:
    first_audio = _export_tone_wav(tmp_path / "sources" / "overflow.wav", duration_ms=1_250, frequency=440)
    second_audio = _export_tone_wav(tmp_path / "sources" / "next.wav", duration_ms=900, frequency=660)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=1_000,
                cn_text="第一句。",
                aligned_audio_path=first_audio,
            ),
            _build_segment(
                segment_id=2,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=1_000,
                end_ms=1_900,
                cn_text="第二句。",
                aligned_audio_path=second_audio,
            ),
        ],
        total_duration_ms=2_100,
        project_name="trim_overflow_segment",
    )

    result = ProjectOutputWriter().write(output)
    exported_paths = sorted((Path(result.segments_dir) / "speaker_a").glob("*.wav"))

    assert len(exported_paths) == 2
    assert len(AudioSegment.from_wav(exported_paths[0])) == 1_000
    assert len(AudioSegment.from_wav(exported_paths[1])) == 900


def test_project_output_preserves_capped_dsp_overflow_audio(tmp_path: Path) -> None:
    overflow_audio = _export_tone_wav(tmp_path / "sources" / "capped.wav", duration_ms=1_350)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=500,
                cn_text="嗯，好。",
                aligned_audio_path=overflow_audio,
                alignment_method="capped_dsp_overflow",
            ),
        ],
        total_duration_ms=2_000,
        project_name="capped_dsp_overflow",
    )

    result = ProjectOutputWriter().write(output)
    exported_paths = sorted((Path(result.segments_dir) / "speaker_a").glob("*.wav"))

    assert len(exported_paths) == 1
    assert len(AudioSegment.from_wav(exported_paths[0])) == 1_350


def test_project_output_segment_file_name_matches_expected_format(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "segment.wav", duration_ms=45_000)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=45_000,
                cn_text="命名测试。",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=45_000,
        project_name="file_name_format",
    )

    copied_paths = ProjectOutputWriter()._copy_segment_files(output)

    assert Path(copied_paths[1]).name == "segment_001_00m00s_00m45s.wav"


def test_project_output_srt_contains_hh_mm_ss_mmm_format_across_hours(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "cross_hour.wav", duration_ms=1_499)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=3_661_001,
                end_ms=3_662_500,
                cn_text="跨小时字幕。",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=3_700_000,
        project_name="srt_cross_hour",
    )

    srt_paths = ProjectOutputWriter()._write_srt(output)
    content = Path(srt_paths[0]).read_text(encoding="utf-8")

    assert "01:01:01,001 --> 01:01:02,500" in content


def test_project_output_srt_is_utf8_and_preserves_chinese_text(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "utf8.wav", duration_ms=900)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=900,
                cn_text="今天我们来聊聊编码。",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=900,
        project_name="srt_utf8",
    )

    srt_paths = ProjectOutputWriter()._write_srt(output)
    decoded = Path(srt_paths[0]).read_bytes().decode("utf-8")

    # New subtitle rule: punctuation is stripped (JianYing style)
    assert "今天我们来聊聊编码" in decoded


def test_project_output_srt_splits_long_segment_into_short_entries(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "long_segment.wav", duration_ms=12_000)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=12_000,
                cn_text=(
                    "这是第一句，用来测试字幕拆分。"
                    "这是第二句，也应该被拆开。"
                    "最后这一句稍微长一点，但仍然应该保持在合理长度内。"
                ),
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=12_000,
        project_name="srt_split_long_segment",
    )

    srt_path = Path(ProjectOutputWriter()._write_srt(output)[0])
    blocks = _parse_srt_blocks(srt_path)

    assert len(blocks) >= 3
    assert all(len(str(block["text"])) <= 40 for block in blocks)
    assert [block["index"] for block in blocks] == list(range(1, len(blocks) + 1))


def test_project_output_srt_hard_splits_long_text_without_punctuation(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "no_punctuation.wav", duration_ms=8_000)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=8_000,
                cn_text="这是一个没有标点的超长字幕文本" * 6,
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=8_000,
        project_name="srt_split_no_punctuation",
    )

    srt_path = Path(ProjectOutputWriter()._write_srt(output)[0])
    blocks = _parse_srt_blocks(srt_path)

    assert len(blocks) >= 2
    assert all(len(str(block["text"])) <= 40 for block in blocks)


def test_project_output_srt_merges_too_short_subtitles(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "short_subtitles.wav", duration_ms=1_500)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=1_500,
                cn_text="短。短。短。最后一句稍长一些。",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=1_500,
        project_name="srt_merge_short_entries",
    )

    srt_path = Path(ProjectOutputWriter()._write_srt(output)[0])
    blocks = _parse_srt_blocks(srt_path)

    assert len(blocks) < 4


def test_project_output_srt_timings_are_monotonic_and_non_overlapping(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "timing.wav", duration_ms=6_000)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=6_000,
                cn_text="第一句。第二句稍微长一点。第三句再补充一点内容。",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=6_000,
        project_name="srt_timing_monotonic",
    )

    srt_path = Path(ProjectOutputWriter()._write_srt(output)[0])
    blocks = _parse_srt_blocks(srt_path)

    previous_end = None
    for block in blocks:
        start_str, end_str = str(block["times"]).split(" --> ")
        start_ms = _srt_timestamp_to_ms(start_str)
        end_ms = _srt_timestamp_to_ms(end_str)
        assert end_ms > start_ms
        if previous_end is not None:
            assert start_ms >= previous_end
        previous_end = end_ms
    assert previous_end == 6_000


def test_project_output_background_detection_degrades_when_original_audio_is_missing(
    tmp_path: Path,
) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "gap.wav", duration_ms=1_000)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=1_000,
                end_ms=2_000,
                cn_text="中间一句。",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=4_000,
        project_name="background_degrade",
    )

    report_path = ProjectOutputWriter()._detect_background_sounds(output)
    content = Path(report_path).read_text(encoding="utf-8")

    assert Path(report_path).exists()
    assert "未检测" in content


def test_project_output_alignment_report_lists_review_segments(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "review.wav", duration_ms=35_000)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=32,
                speaker_id="speaker_b",
                display_name="主持人",
                start_ms=1_125_000,
                end_ms=1_160_000,
                cn_text="这段需要人工复查。",
                aligned_audio_path=source_audio,
                alignment_method="force_dsp",
                needs_review=True,
            )
        ],
        total_duration_ms=1_200_000,
        project_name="alignment_review",
    )

    report_path = ProjectOutputWriter()._write_alignment_report(output)
    content = Path(report_path).read_text(encoding="utf-8")

    assert "⚠️ 需要手工检查的段落（共1段）：" in content
    assert "segment_032  Speaker B  00:18:45 → 00:19:20  [强制DSP，变速幅度过大]" in content


def test_project_output_composes_full_audio_with_non_silent_overlays(tmp_path: Path) -> None:
    first_audio = _export_tone_wav(tmp_path / "sources" / "tone_1.wav", duration_ms=400, frequency=440)
    second_audio = _export_tone_wav(tmp_path / "sources" / "tone_2.wav", duration_ms=500, frequency=660)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=1_000,
                end_ms=1_400,
                cn_text="第一段配音。",
                aligned_audio_path=first_audio,
            ),
            _build_segment(
                segment_id=2,
                speaker_id="speaker_b",
                display_name="主持人",
                start_ms=3_000,
                end_ms=3_500,
                cn_text="第二段配音。",
                aligned_audio_path=second_audio,
                alignment_method="rewrite_dsp",
            ),
        ],
        total_duration_ms=5_000,
        project_name="overlay_audio",
    )

    writer = ProjectOutputWriter()
    segment_paths = writer._copy_segment_files(output)
    composed_path = writer._compose_full_audio(output, segment_paths)
    composed = AudioSegment.from_wav(composed_path)

    assert composed[1_000:1_300].rms > 0
    assert composed[3_000:3_300].rms > 0
    assert composed[1_600:1_900].rms == 0


def test_project_output_compose_full_audio_avoids_in_memory_silent_track(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_audio = _export_tone_wav(tmp_path / "sources" / "long_tone_1.wav", duration_ms=400, frequency=440)
    second_audio = _export_tone_wav(tmp_path / "sources" / "long_tone_2.wav", duration_ms=500, frequency=660)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=1_000,
                end_ms=1_400,
                cn_text="第一段配音。",
                aligned_audio_path=first_audio,
            ),
            _build_segment(
                segment_id=2,
                speaker_id="speaker_b",
                display_name="主持人",
                start_ms=3_000,
                end_ms=3_500,
                cn_text="第二段配音。",
                aligned_audio_path=second_audio,
            ),
        ],
        total_duration_ms=5_000,
        project_name="overlay_audio_no_memory_bed",
    )
    writer = ProjectOutputWriter()
    segment_paths = writer._copy_segment_files(output)

    def fail_if_called(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("_compose_full_audio should not create a full silent AudioSegment")

    monkeypatch.setattr("modules.output.editor.editor_package_writer.AudioSegment.silent", fail_if_called)

    composed_path = writer._compose_full_audio(output, segment_paths)
    composed = AudioSegment.from_wav(composed_path)

    assert len(composed) == output.total_duration_ms
    assert composed[1_000:1_300].rms > 0
    assert composed[3_000:3_300].rms > 0


def test_project_output_normalizes_full_output_when_peak_is_low(tmp_path: Path) -> None:
    output_path = tmp_path / "quiet_output.wav"
    quiet_audio = Sine(440).to_audio_segment(duration=1_000).apply_gain(-18)
    quiet_audio.export(output_path, format="wav")

    writer = ProjectOutputWriter()
    before = AudioSegment.from_wav(output_path).max_dBFS

    writer._normalize_full_output_audio(output_path)

    after = AudioSegment.from_wav(output_path).max_dBFS
    assert after > before
    assert after > -2.0


def test_project_output_compose_full_audio_normalizes_ffmpeg_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_audio = _export_tone_wav(tmp_path / "sources" / "ffmpeg_norm.wav", duration_ms=500, frequency=440)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=500,
                cn_text="娴嬭瘯",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=1_000,
        project_name="ffmpeg_normalize",
    )

    writer = ProjectOutputWriter()
    segment_paths = writer._copy_segment_files(output)
    normalized_paths: list[Path] = []

    def fake_compose_with_ffmpeg(
        _output: ProjectOutput,
        _segment_paths: dict[int, str],
        output_path: Path,
    ) -> None:
        quiet_audio = Sine(440).to_audio_segment(duration=1_000).apply_gain(-18)
        quiet_audio.set_channels(2).set_frame_rate(44_100).set_sample_width(2).export(output_path, format="wav")

    def record_normalization(output_path: Path) -> None:
        normalized_paths.append(output_path)

    monkeypatch.setattr(writer, "_compose_full_audio_with_ffmpeg", fake_compose_with_ffmpeg)
    monkeypatch.setattr(writer, "_normalize_full_output_audio", record_normalization)

    writer._compose_full_audio(output, segment_paths)

    assert normalized_paths == [Path(output.output_dir) / "output" / "dubbed_audio_complete.wav"]


def test_project_output_compose_full_audio_normalizes_fallback_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_audio = _export_tone_wav(tmp_path / "sources" / "fallback_norm.wav", duration_ms=500, frequency=440)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=500,
                cn_text="娴嬭瘯",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=1_000,
        project_name="fallback_normalize",
    )

    writer = ProjectOutputWriter()
    segment_paths = writer._copy_segment_files(output)
    normalized_paths: list[Path] = []

    def fail_ffmpeg(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise FileNotFoundError("ffmpeg missing")

    def record_normalization(output_path: Path) -> None:
        normalized_paths.append(output_path)

    monkeypatch.setattr(writer, "_compose_full_audio_with_ffmpeg", fail_ffmpeg)
    monkeypatch.setattr(writer, "_normalize_full_output_audio", record_normalization)

    writer._compose_full_audio(output, segment_paths)

    assert normalized_paths == [Path(output.output_dir) / "output" / "dubbed_audio_complete.wav"]


def test_project_output_copy_segment_files_does_not_normalize_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_audio = _export_tone_wav(tmp_path / "sources" / "segment_copy.wav", duration_ms=500, frequency=440)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=500,
                cn_text="娴嬭瘯",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=1_000,
        project_name="copy_without_normalize",
    )

    def fail_if_called(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("segment export should not normalize copied audio")

    monkeypatch.setattr("modules.output.editor.editor_package_writer.normalize", fail_if_called)

    copied_paths = ProjectOutputWriter()._copy_segment_files(output)

    assert Path(copied_paths[1]).exists()


def test_project_output_exports_jianying_compatible_wav_format(tmp_path: Path) -> None:
    source_audio = _export_custom_wav(
        tmp_path / "sources" / "compatibility.wav",
        duration_ms=1_000,
        frame_rate=24_000,
        channels=1,
        sample_width=1,
    )
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=1_000,
                cn_text="格式兼容测试。",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=1_200,
        project_name="jianying_compatible",
    )

    result = ProjectOutputWriter().write(output)
    dubbed_audio = AudioSegment.from_wav(result.dubbed_audio_path)
    copied_segment_path = next((Path(result.segments_dir) / "speaker_a").glob("*.wav"))
    copied_segment_audio = AudioSegment.from_wav(copied_segment_path)

    assert dubbed_audio.frame_rate == 44_100
    assert dubbed_audio.channels == 2
    assert dubbed_audio.sample_width == 2
    assert copied_segment_audio.frame_rate == 44_100
    assert copied_segment_audio.channels == 2
    assert copied_segment_audio.sample_width == 2


def test_project_output_exports_ambient_audio_track(tmp_path: Path) -> None:
    source_audio = _export_silent_wav(tmp_path / "sources" / "segment.wav", duration_ms=900)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=900,
                cn_text="环境音导出测试。",
                aligned_audio_path=source_audio,
            )
        ],
        total_duration_ms=1_200,
        project_name="ambient_export",
    )
    ambient_source = _export_custom_wav(
        Path(output.output_dir) / "audio" / "ambient.wav",
        duration_ms=1_200,
        frame_rate=32_000,
        channels=2,
        sample_width=2,
    )

    result = ProjectOutputWriter().write(output)
    ambient_audio = AudioSegment.from_wav(result.ambient_audio_path)

    assert Path(result.ambient_audio_path).exists()
    assert len(ambient_audio) == 1_200
    assert ambient_audio.frame_rate == 44_100
    assert Path(result.ambient_audio_path).name == "ambient_audio.wav"
    assert Path(result.ambient_audio_path) != ambient_source


def test_project_output_skips_ffmpeg_when_wav_is_already_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compatible_wav = _export_custom_wav(
        tmp_path / "sources" / "already_compatible.wav",
        duration_ms=1_000,
        frame_rate=44_100,
        channels=2,
        sample_width=2,
    )

    def fail_if_called(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("ffmpeg should not be called for already-compatible WAV files")

    monkeypatch.setattr("modules.output.editor.editor_package_writer.subprocess.run", fail_if_called)

    result_path = ProjectOutputWriter()._ensure_jianying_compatible(str(compatible_wav))

    assert result_path == str(compatible_wav.resolve(strict=False))


def test_project_output_alignment_report_includes_rewrite_method_labels(tmp_path: Path) -> None:
    first_audio = _export_silent_wav(tmp_path / "sources" / "rewrite_direct.wav", duration_ms=1_000)
    second_audio = _export_silent_wav(tmp_path / "sources" / "rewrite_dsp.wav", duration_ms=1_000)
    output = _build_output(
        tmp_path,
        segments=[
            _build_segment(
                segment_id=1,
                speaker_id="speaker_a",
                display_name="Dan Koe",
                start_ms=0,
                end_ms=1_000,
                cn_text="字幕文本保持不变。",
                aligned_audio_path=first_audio,
                alignment_method="rewrite_direct",
            ),
            _build_segment(
                segment_id=2,
                speaker_id="speaker_b",
                display_name="主持人",
                start_ms=1_000,
                end_ms=2_000,
                cn_text="第二条字幕。",
                aligned_audio_path=second_audio,
                alignment_method="rewrite_dsp",
            ),
        ],
        total_duration_ms=2_500,
        project_name="alignment_rewrite_labels",
    )

    report_path = ProjectOutputWriter()._write_alignment_report(output)
    content = Path(report_path).read_text(encoding="utf-8")

    assert "Gemini重写后直接使用：1段（50%）" in content
    assert "Gemini重写后DSP对齐：1段（50%）" in content
