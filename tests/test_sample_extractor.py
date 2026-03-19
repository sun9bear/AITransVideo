from pathlib import Path

from pydub import AudioSegment
from pydub.generators import Sine

from services.assemblyai.transcriber import TranscriptLine
from services.voice.sample_extractor import VoiceSampleExtractor


def _build_timeline_audio(path: Path, segments: list[tuple[int, int, int]]) -> Path:
    total_duration_ms = max(end_ms for _, end_ms, _ in segments)
    audio = AudioSegment.silent(duration=total_duration_ms)
    for start_ms, end_ms, gain_db in segments:
        clip = Sine(440).to_audio_segment(duration=end_ms - start_ms).apply_gain(gain_db)
        audio = audio.overlay(clip, position=start_ms)
    path.parent.mkdir(parents=True, exist_ok=True)
    audio.export(path, format="wav")
    return path


def _line(index: int, start_ms: int, end_ms: int) -> TranscriptLine:
    return TranscriptLine(
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_id="speaker_b",
        speaker_label="B",
        source_text=f"Line {index}",
    )


def test_sample_extractor_extracts_valid_sample_in_target_range(tmp_path: Path) -> None:
    audio_path = _build_timeline_audio(
        tmp_path / "audio" / "original.wav",
        [
            (0, 10_000, 0),
            (12_000, 22_000, 0),
            (24_000, 34_000, 0),
            (36_000, 46_000, 0),
            (48_000, 58_000, 0),
        ],
    )
    lines = [
        _line(1, 0, 10_000),
        _line(2, 12_000, 22_000),
        _line(3, 24_000, 34_000),
        _line(4, 36_000, 46_000),
        _line(5, 48_000, 58_000),
    ]

    output_path = VoiceSampleExtractor().extract_sample(
        str(audio_path),
        lines,
        str(tmp_path / "sample.wav"),
    )

    sample = AudioSegment.from_wav(output_path)
    assert 30_000 <= len(sample) <= 300_000
    assert sample.frame_rate == 16_000
    assert sample.channels == 1
    assert sample.sample_width == 2


def test_sample_extractor_skips_low_volume_segments(tmp_path: Path) -> None:
    audio_path = _build_timeline_audio(
        tmp_path / "audio" / "original.wav",
        [
            (0, 10_000, 0),
            (12_000, 22_000, -65),
            (24_000, 34_000, 0),
            (36_000, 46_000, 0),
        ],
    )
    lines = [
        _line(1, 0, 10_000),
        _line(2, 12_000, 22_000),
        _line(3, 24_000, 34_000),
        _line(4, 36_000, 46_000),
    ]

    output_path = VoiceSampleExtractor().extract_sample(
        str(audio_path),
        lines,
        str(tmp_path / "sample.wav"),
        min_duration_s=35.0,
        max_duration_s=45.0,
    )

    sample = AudioSegment.from_wav(output_path)
    assert len(sample) == 30_000


def test_sample_extractor_falls_back_to_shorter_segments_when_needed(tmp_path: Path) -> None:
    audio_path = _build_timeline_audio(
        tmp_path / "audio" / "original.wav",
        [
            (0, 4_000, 0),
            (5_000, 8_000, 0),
            (9_000, 12_000, 0),
        ],
    )
    lines = [
        _line(1, 0, 4_000),
        _line(2, 5_000, 8_000),
        _line(3, 9_000, 12_000),
    ]

    output_path = VoiceSampleExtractor().extract_sample(
        str(audio_path),
        lines,
        str(tmp_path / "sample.wav"),
    )

    sample = AudioSegment.from_wav(output_path)
    assert len(sample) == 10_000


def test_sample_extractor_accumulates_toward_max_duration_when_more_audio_exists(
    tmp_path: Path,
) -> None:
    segments = []
    lines = []
    for index in range(40):
        start_ms = index * 12_000
        end_ms = start_ms + 10_000
        segments.append((start_ms, end_ms, 0))
        lines.append(_line(index + 1, start_ms, end_ms))

    audio_path = _build_timeline_audio(tmp_path / "audio" / "original.wav", segments)

    output_path = VoiceSampleExtractor().extract_sample(
        str(audio_path),
        lines,
        str(tmp_path / "sample.wav"),
    )

    sample = AudioSegment.from_wav(output_path)
    assert len(sample) == 300_000


def test_sample_extractor_validate_sample_reports_duration_and_rms(tmp_path: Path) -> None:
    sample_path = tmp_path / "sample.wav"
    audio = Sine(440).to_audio_segment(duration=10_000).append(
        AudioSegment.silent(duration=5_000),
        crossfade=0,
    )
    audio = audio.set_frame_rate(16_000).set_channels(1).set_sample_width(2)
    audio.export(sample_path, format="wav")

    result = VoiceSampleExtractor().validate_sample(str(sample_path))

    assert result["duration_s"] == 15.0
    assert isinstance(result["rms_dbfs"], float)
    assert result["silence_ratio"] > 0.3
    assert result["is_valid"] is False
    assert "样本时长不足10秒" not in result["warnings"]
    assert "静音占比超过30%" in result["warnings"]
