from dataclasses import dataclass
from pathlib import Path
import wave

from core.exceptions import TTSError
from core.models import SemanticBlock


@dataclass(slots=True)
class MockTTSConfig:
    ms_per_char: int = 100
    min_duration_ms: int = 800
    sample_rate_hz: int = 16_000


class MockTTSService:
    def __init__(self, output_dir: str, config: MockTTSConfig | None = None) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or MockTTSConfig()

    def synthesize(self, block: SemanticBlock) -> str:
        selected_cn_text = block.merged_cn_text.strip()
        if not selected_cn_text:
            raise TTSError("Cannot synthesize empty block text.")

        duration_ms = self._estimate_duration_ms(selected_cn_text)
        output_path = self.output_dir / f"{block.block_id}_r{block.rewrite_count}.wav"
        frame_count = max(1, int(self.config.sample_rate_hz * duration_ms / 1_000))
        silence_frame = b"\x00\x00"

        try:
            with wave.open(str(output_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.config.sample_rate_hz)
                wav_file.writeframes(silence_frame * frame_count)
        except OSError as exc:
            raise TTSError(f"Failed to synthesize mock wav for {block.block_id}") from exc

        return str(output_path)

    def get_cache_context(self) -> dict[str, object]:
        return {"provider_variant": "mock_tts_v1", "audio_format": "wav"}

    def _estimate_duration_ms(self, text: str) -> int:
        compact_text = "".join(text.split())
        estimated_duration_ms = len(compact_text) * self.config.ms_per_char
        return max(self.config.min_duration_ms, estimated_duration_ms)
