from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydub import AudioSegment


DEFAULT_SPEECH_FRAME_RATE = 16_000


class AudioSeparationError(Exception):
    pass


@dataclass(slots=True)
class AudioSeparationResult:
    source_audio_path: str
    speech_audio_path: str
    ambient_audio_path: str
    reused_cache: bool


class AudioStemSeparator:
    """Create a speech-focused stem for ASR plus an ambient stem for manual reuse."""

    speech_filename = "speech_for_asr.wav"
    ambient_filename = "ambient.wav"

    def separate(
        self,
        source_audio_path: str,
        output_dir: str,
        *,
        skip_if_exists: bool = True,
    ) -> AudioSeparationResult:
        source_path = Path(source_audio_path).expanduser().resolve(strict=False)
        if not source_path.exists():
            raise AudioSeparationError(f"Source audio file not found: {source_path}")

        output_root = Path(output_dir).expanduser().resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)
        speech_path = output_root / self.speech_filename
        ambient_path = output_root / self.ambient_filename

        if skip_if_exists and self._is_cache_valid(source_path, speech_path, ambient_path):
            return AudioSeparationResult(
                source_audio_path=str(source_path),
                speech_audio_path=str(speech_path),
                ambient_audio_path=str(ambient_path),
                reused_cache=True,
            )

        try:
            if source_path.suffix.casefold() == ".wav":
                source_audio = AudioSegment.from_wav(str(source_path))
            else:
                source_audio = AudioSegment.from_file(str(source_path))
        except Exception as exc:
            raise AudioSeparationError(f"Failed to load source audio: {source_path}") from exc

        source_audio = source_audio.set_sample_width(2)
        speech_audio, ambient_audio = self._split_stems(source_audio)
        speech_audio.export(speech_path, format="wav")
        ambient_audio.export(ambient_path, format="wav")

        return AudioSeparationResult(
            source_audio_path=str(source_path),
            speech_audio_path=str(speech_path.resolve(strict=False)),
            ambient_audio_path=str(ambient_path.resolve(strict=False)),
            reused_cache=False,
        )

    def _split_stems(self, source_audio: AudioSegment) -> tuple[AudioSegment, AudioSegment]:
        if source_audio.channels <= 1:
            speech_audio = (
                source_audio.set_channels(1)
                .set_frame_rate(DEFAULT_SPEECH_FRAME_RATE)
                .set_sample_width(2)
            )
            ambient_audio = (
                AudioSegment.silent(duration=len(source_audio), frame_rate=max(source_audio.frame_rate, 44_100))
                .set_channels(2)
                .set_sample_width(2)
            )
            return speech_audio, ambient_audio

        stereo_audio = source_audio.set_channels(2).set_sample_width(2)
        left_channel, right_channel = stereo_audio.split_to_mono()

        # Approximate centered speech by summing both channels with headroom.
        centered_speech = left_channel.apply_gain(-6.0).overlay(right_channel.apply_gain(-6.0))
        speech_audio = (
            centered_speech.set_channels(1)
            .set_frame_rate(DEFAULT_SPEECH_FRAME_RATE)
            .set_sample_width(2)
        )

        # Remove the centered content from each side to keep applause/laughter/music as a stem.
        centered_inverse = centered_speech.invert_phase()
        ambient_left = left_channel.overlay(centered_inverse)
        ambient_right = right_channel.overlay(centered_inverse)
        ambient_audio = (
            AudioSegment.from_mono_audiosegments(ambient_left, ambient_right)
            .set_frame_rate(stereo_audio.frame_rate)
            .set_sample_width(2)
        )
        return speech_audio, ambient_audio

    def _is_cache_valid(self, source_path: Path, speech_path: Path, ambient_path: Path) -> bool:
        if not speech_path.exists() or not ambient_path.exists():
            return False
        source_mtime = source_path.stat().st_mtime
        return (
            speech_path.stat().st_mtime >= source_mtime
            and ambient_path.stat().st_mtime >= source_mtime
        )
