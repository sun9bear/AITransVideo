from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from services.audio.separator import AudioSeparationResult, AudioStemSeparator


@dataclass(slots=True)
class SourceAudioPreparationRequest:
    project_dir: str
    source_audio_path: str


@dataclass(slots=True)
class SourceAudioPreparationResult:
    source_audio_path: str
    speech_audio_path: str
    ambient_audio_path: str
    reused_cache: bool


class SourceAudioPreparationService:
    """Prepare reusable source-audio assets for ASR and manual ambient reuse."""

    def __init__(self, stem_separator: AudioStemSeparator | None = None) -> None:
        self.stem_separator = stem_separator or AudioStemSeparator()

    def prepare(
        self,
        request: SourceAudioPreparationRequest,
    ) -> SourceAudioPreparationResult:
        project_dir = Path(request.project_dir).expanduser().resolve(strict=False)
        source_audio_path = Path(request.source_audio_path).expanduser().resolve(strict=False)

        print("[S0] 正在分离人声和环境音...")
        separation_result = self.stem_separator.separate(
            str(source_audio_path),
            str((project_dir / "audio").resolve(strict=False)),
        )
        if separation_result.reused_cache:
            print("[S0] 复用已有分离缓存：speech_for_asr.wav / ambient.wav")
        else:
            print("[S0] 分离完成：已生成 speech_for_asr.wav / ambient.wav")
        return self._build_result(separation_result)

    @staticmethod
    def _build_result(separation_result: AudioSeparationResult) -> SourceAudioPreparationResult:
        return SourceAudioPreparationResult(
            source_audio_path=separation_result.source_audio_path,
            speech_audio_path=separation_result.speech_audio_path,
            ambient_audio_path=separation_result.ambient_audio_path,
            reused_cache=separation_result.reused_cache,
        )
