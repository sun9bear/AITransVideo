from pathlib import Path

from pydub import AudioSegment
from pydub.generators import Sine

from services.audio.source_audio_preparation import (
    SourceAudioPreparationRequest,
    SourceAudioPreparationService,
)


def _export_audio(path: Path, audio: AudioSegment) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    audio.export(path, format="wav")
    return path


def test_source_audio_preparation_service_creates_expected_assets(tmp_path: Path) -> None:
    stereo = AudioSegment.from_mono_audiosegments(
        Sine(440).to_audio_segment(duration=1_000).apply_gain(-12.0),
        Sine(440).to_audio_segment(duration=1_000).apply_gain(-12.0),
    ).set_frame_rate(44_100).set_sample_width(2)
    source_path = _export_audio(tmp_path / "project" / "audio" / "original.wav", stereo)

    result = SourceAudioPreparationService().prepare(
        SourceAudioPreparationRequest(
            project_dir=str(tmp_path / "project"),
            source_audio_path=str(source_path),
        )
    )

    assert Path(result.source_audio_path) == source_path.resolve(strict=False)
    assert Path(result.speech_audio_path).name == "speech_for_asr.wav"
    assert Path(result.ambient_audio_path).name == "ambient.wav"
    assert Path(result.speech_audio_path).exists()
    assert Path(result.ambient_audio_path).exists()
    assert result.reused_cache is False


def test_source_audio_preparation_service_reuses_existing_cache(tmp_path: Path) -> None:
    mono = Sine(440).to_audio_segment(duration=800).apply_gain(-10.0).set_channels(1).set_frame_rate(22_050)
    source_path = _export_audio(tmp_path / "project_cache" / "audio" / "original.wav", mono)
    service = SourceAudioPreparationService()

    first_result = service.prepare(
        SourceAudioPreparationRequest(
            project_dir=str(tmp_path / "project_cache"),
            source_audio_path=str(source_path),
        )
    )
    second_result = service.prepare(
        SourceAudioPreparationRequest(
            project_dir=str(tmp_path / "project_cache"),
            source_audio_path=str(source_path),
        )
    )

    assert first_result.reused_cache is False
    assert second_result.reused_cache is True
    assert second_result.speech_audio_path == first_result.speech_audio_path
    assert second_result.ambient_audio_path == first_result.ambient_audio_path
