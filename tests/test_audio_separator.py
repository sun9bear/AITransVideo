from pathlib import Path

from pydub import AudioSegment
from pydub.generators import Sine

from services.audio.separator import AudioStemSeparator


def _export_audio(path: Path, audio: AudioSegment) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    audio.export(path, format="wav")
    return path


def test_audio_stem_separator_creates_speech_and_ambient_tracks_for_stereo_input(tmp_path: Path) -> None:
    centered = Sine(440).to_audio_segment(duration=1_200).apply_gain(-12.0)
    side_only = Sine(880).to_audio_segment(duration=1_200).apply_gain(-15.0)
    left = centered.overlay(side_only)
    right = centered
    stereo = AudioSegment.from_mono_audiosegments(left, right).set_frame_rate(44_100).set_sample_width(2)
    source_path = _export_audio(tmp_path / "audio" / "original.wav", stereo)

    result = AudioStemSeparator().separate(str(source_path), str(source_path.parent))

    speech_audio = AudioSegment.from_wav(result.speech_audio_path)
    ambient_audio = AudioSegment.from_wav(result.ambient_audio_path)

    assert result.reused_cache is False
    assert speech_audio.channels == 1
    assert speech_audio.frame_rate == 16_000
    assert len(speech_audio) == 1_200
    assert ambient_audio.channels == 2
    assert len(ambient_audio) == 1_200
    assert ambient_audio.rms > 0


def test_audio_stem_separator_uses_silent_ambient_fallback_for_mono_input(tmp_path: Path) -> None:
    mono = Sine(440).to_audio_segment(duration=900).apply_gain(-10.0).set_channels(1).set_frame_rate(22_050)
    source_path = _export_audio(tmp_path / "audio" / "original.wav", mono)

    result = AudioStemSeparator().separate(str(source_path), str(source_path.parent))

    speech_audio = AudioSegment.from_wav(result.speech_audio_path)
    ambient_audio = AudioSegment.from_wav(result.ambient_audio_path)

    assert speech_audio.channels == 1
    assert speech_audio.frame_rate == 16_000
    assert ambient_audio.channels == 2
    assert len(ambient_audio) == 900
    assert ambient_audio.rms == 0
