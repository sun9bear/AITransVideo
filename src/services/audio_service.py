from pathlib import Path
import wave

from core.exceptions import AudioProcessingError


class AudioService:
    def get_duration_ms(self, audio_path: str) -> int:
        file_path = Path(audio_path)
        if file_path.suffix.lower() != ".wav":
            raise AudioProcessingError("Sprint 1 only supports wav duration reading.")
        if not file_path.exists():
            raise AudioProcessingError(f"Audio file not found: {audio_path}")

        try:
            with wave.open(str(file_path), "rb") as wav_file:
                frames = wav_file.getnframes()
                frame_rate = wav_file.getframerate()
                if frame_rate <= 0:
                    raise AudioProcessingError("Invalid wav sample rate.")
                return int(round(frames / float(frame_rate) * 1_000))
        except (wave.Error, OSError) as exc:
            raise AudioProcessingError(f"Failed to read wav duration: {audio_path}") from exc
