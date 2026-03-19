import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from typing import Protocol, runtime_checkable
import wave

from core.exceptions import (
    MediaUnderstandingConfigurationError,
    MediaUnderstandingError,
    MediaUnderstandingExtractedTranscriptOutputError,
    MediaUnderstandingInvalidSourcePathError,
    MediaUnderstandingOutputError,
    MediaUnderstandingProviderUnavailableError,
    MediaUnderstandingTranscriptExtractionModelError,
    MediaUnderstandingTranscriptExtractionNoResultError,
    MediaUnderstandingTranscriptExtractionRuntimeError,
    MediaUnderstandingTranscriptExtractionUnavailableError,
    MediaUnderstandingUnsupportedSourceKindError,
)
from modules.ingestion.srt_loader import SRTSubtitleLoader
from modules.media_understanding.models import (
    AttributedTranscriptLine,
    MediaSource,
    MediaSourceKind,
    TranscriptExtractionRequest,
    TranscriptExtractionResult,
    TranscriptLine,
)
from modules.media_understanding.normalizer import AttributedTranscriptNormalizer
from services import config_loader


@runtime_checkable
class MediaUnderstandingProvider(Protocol):
    """Replaceable provider boundary for attributed transcript generation."""

    def load_attributed_transcript(self, source: MediaSource) -> list[AttributedTranscriptLine]:
        """Resolve one source into attributed transcript lines."""


@runtime_checkable
class TranscriptExtractionProvider(Protocol):
    """Extract plain transcript lines from media inputs before attribution."""

    def extract_transcript(self, request: TranscriptExtractionRequest) -> TranscriptExtractionResult:
        """Resolve one validated local-media request into transcript lines."""


@dataclass(slots=True)
class MockMediaUnderstandingProviderConfig:
    provider_name: str = "mock_media_understanding"
    default_speaker_id: str = "speaker_default"
    default_speaker_name: str | None = "Speaker 1"
    stub_lines: list[AttributedTranscriptLine] = field(default_factory=list)

    def validate(self) -> None:
        if not self.provider_name.strip():
            raise MediaUnderstandingConfigurationError("Mock media understanding provider_name is required.")
        _validate_default_speaker_id(self.default_speaker_id)


@dataclass(slots=True)
class LocalTranscriptProviderConfig:
    provider_name: str = "local_transcript"
    default_speaker_id: str = "speaker_default"
    default_speaker_name: str | None = None

    def validate(self) -> None:
        if not self.provider_name.strip():
            raise MediaUnderstandingConfigurationError("Local transcript provider_name is required.")
        _validate_default_speaker_id(self.default_speaker_id)


@dataclass(slots=True)
class LocalSRTProviderConfig:
    provider_name: str = "local_srt"
    default_speaker_id: str = "speaker_default"
    default_speaker_name: str | None = None

    def validate(self) -> None:
        if not self.provider_name.strip():
            raise MediaUnderstandingConfigurationError("Local SRT provider_name is required.")
        _validate_default_speaker_id(self.default_speaker_id)


@dataclass(slots=True)
class FutureMultimodalMediaUnderstandingProviderConfig:
    provider_name: str = "gemini_like_multimodal"
    supported_source_kinds: tuple[MediaSourceKind, ...] = (
        MediaSourceKind.YOUTUBE_URL,
        MediaSourceKind.LOCAL_VIDEO,
        MediaSourceKind.LOCAL_AUDIO,
    )

    def validate(self) -> None:
        if not self.provider_name.strip():
            raise MediaUnderstandingConfigurationError("Future multimodal provider_name is required.")
        if not self.supported_source_kinds:
            raise MediaUnderstandingConfigurationError(
                "Future multimodal provider must declare at least one supported media source kind."
            )


@dataclass(slots=True)
class TranscriptExtractionMediaUnderstandingProviderConfig:
    provider_name: str = "transcript_extraction_adapter"
    provider_variant: str = "transcript_extraction_adapter_v1"
    default_speaker_id: str = "speaker_default"
    default_speaker_name: str | None = None

    def validate(self) -> None:
        if not self.provider_name.strip():
            raise MediaUnderstandingConfigurationError("Transcript extraction adapter provider_name is required.")
        if not self.provider_variant.strip():
            raise MediaUnderstandingConfigurationError("Transcript extraction adapter provider_variant is required.")
        _validate_default_speaker_id(self.default_speaker_id)


@dataclass(slots=True)
class LocalASRTranscriptExtractionProviderConfig:
    provider_name: str = "system_speech_local_asr"
    provider_mode: str = "real"
    provider_variant: str = "system_speech_local_asr_v1"
    model_name: str = "system_speech_dictation"
    language: str = "auto"
    task: str = "transcribe"
    powershell_executable: str = "powershell"
    command_timeout_ms: int = 120_000
    supported_extensions: tuple[str, ...] = (".wav", ".wave")
    supported_source_kinds: tuple[MediaSourceKind, ...] = (
        MediaSourceKind.LOCAL_AUDIO,
    )

    def validate(self) -> None:
        if not self.provider_name.strip():
            raise MediaUnderstandingConfigurationError("Local ASR transcript extraction provider_name is required.")
        if not self.provider_mode.strip():
            raise MediaUnderstandingConfigurationError("Local ASR transcript extraction provider_mode is required.")
        if not self.provider_variant.strip():
            raise MediaUnderstandingConfigurationError("Local ASR transcript extraction provider_variant is required.")
        if not self.model_name.strip():
            raise MediaUnderstandingConfigurationError("Local ASR transcript extraction model_name is required.")
        if not self.language.strip():
            raise MediaUnderstandingConfigurationError("Local ASR transcript extraction language is required.")
        if not self.task.strip():
            raise MediaUnderstandingConfigurationError("Local ASR transcript extraction task is required.")
        if self.task.strip().lower() != "transcribe":
            raise MediaUnderstandingConfigurationError("Local ASR transcript extraction task must be transcribe.")
        if not self.powershell_executable.strip():
            raise MediaUnderstandingConfigurationError(
                "Local ASR transcript extraction powershell_executable is required."
            )
        if self.command_timeout_ms <= 0:
            raise MediaUnderstandingConfigurationError(
                "Local ASR transcript extraction command_timeout_ms must be positive."
            )
        if not self.supported_extensions:
            raise MediaUnderstandingConfigurationError(
                "Local ASR transcript extraction supported_extensions must not be empty."
            )
        if any((not ext.startswith(".")) or (len(ext) <= 1) for ext in self.supported_extensions):
            raise MediaUnderstandingConfigurationError(
                "Local ASR transcript extraction supported_extensions must use dot-prefixed extensions."
            )
        if not self.supported_source_kinds:
            raise MediaUnderstandingConfigurationError(
                "Local ASR transcript extraction provider must declare supported source kinds."
            )


@dataclass(slots=True)
class FutureMultimodalTranscriptExtractionProviderConfig:
    provider_name: str = "gemini_like_multimodal_extraction"
    provider_variant: str = "future_multimodal_transcript_extraction_v1"
    supported_source_kinds: tuple[MediaSourceKind, ...] = (
        MediaSourceKind.LOCAL_VIDEO,
        MediaSourceKind.LOCAL_AUDIO,
    )

    def validate(self) -> None:
        if not self.provider_name.strip():
            raise MediaUnderstandingConfigurationError(
                "Future multimodal transcript extraction provider_name is required."
            )
        if not self.provider_variant.strip():
            raise MediaUnderstandingConfigurationError(
                "Future multimodal transcript extraction provider_variant is required."
            )
        if not self.supported_source_kinds:
            raise MediaUnderstandingConfigurationError(
                "Future multimodal transcript extraction provider must declare supported source kinds."
            )


@dataclass(slots=True)
class CommandTranscriptExtractionProviderConfig:
    provider_name: str = "command_transcript_extraction"
    provider_variant: str = "command_transcript_extraction_stub_v1"
    supported_source_kinds: tuple[MediaSourceKind, ...] = (
        MediaSourceKind.LOCAL_AUDIO,
        MediaSourceKind.LOCAL_VIDEO,
    )

    def validate(self) -> None:
        if not self.provider_name.strip():
            raise MediaUnderstandingConfigurationError(
                "Command transcript extraction provider_name is required."
            )
        if not self.provider_variant.strip():
            raise MediaUnderstandingConfigurationError(
                "Command transcript extraction provider_variant is required."
            )
        if not self.supported_source_kinds:
            raise MediaUnderstandingConfigurationError(
                "Command transcript extraction provider must declare supported source kinds."
            )


@dataclass(slots=True)
class TranscriptExtractionProviderSelectionConfig:
    provider: str = "system_speech_local_asr"
    mode: str = "real"
    local_asr: LocalASRTranscriptExtractionProviderConfig = field(
        default_factory=LocalASRTranscriptExtractionProviderConfig
    )
    command: CommandTranscriptExtractionProviderConfig = field(
        default_factory=CommandTranscriptExtractionProviderConfig
    )
    multimodal: FutureMultimodalTranscriptExtractionProviderConfig = field(
        default_factory=FutureMultimodalTranscriptExtractionProviderConfig
    )

    @classmethod
    def from_env(
        cls,
        *,
        config_path: Path | None = None,
    ) -> "TranscriptExtractionProviderSelectionConfig":
        project_config = config_loader.load_project_local_config(config_path)
        provider, _ = config_loader.resolve_text_value(
            env_keys=["AUTODUB_TRANSCRIPT_EXTRACTION_PROVIDER"],
            config=project_config,
            config_key_paths=(("media_understanding", "transcript_extraction_provider"),),
        )
        mode, _ = config_loader.resolve_text_value(
            env_keys=["AUTODUB_TRANSCRIPT_EXTRACTION_MODE"],
            config=project_config,
            config_key_paths=(("media_understanding", "transcript_extraction_mode"),),
        )
        selection = cls()
        if provider:
            selection.provider = provider.strip()
        if mode:
            selection.mode = mode.strip().lower()
        return selection


@dataclass(slots=True)
class MediaUnderstandingProviderSelectionConfig:
    mode: str = "mock"
    mock: MockMediaUnderstandingProviderConfig = field(default_factory=MockMediaUnderstandingProviderConfig)
    local_transcript: LocalTranscriptProviderConfig = field(default_factory=LocalTranscriptProviderConfig)
    local_srt: LocalSRTProviderConfig = field(default_factory=LocalSRTProviderConfig)
    multimodal: FutureMultimodalMediaUnderstandingProviderConfig = field(
        default_factory=FutureMultimodalMediaUnderstandingProviderConfig
    )
    transcript_extraction: TranscriptExtractionMediaUnderstandingProviderConfig = field(
        default_factory=TranscriptExtractionMediaUnderstandingProviderConfig
    )
    extraction: TranscriptExtractionProviderSelectionConfig = field(
        default_factory=TranscriptExtractionProviderSelectionConfig
    )

    @classmethod
    def for_transcript_extraction(
        cls,
        extraction: TranscriptExtractionProviderSelectionConfig,
        *,
        adapter: TranscriptExtractionMediaUnderstandingProviderConfig | None = None,
    ) -> "MediaUnderstandingProviderSelectionConfig":
        return cls(
            mode="transcript_extraction",
            transcript_extraction=adapter or TranscriptExtractionMediaUnderstandingProviderConfig(),
            extraction=extraction,
        )


@dataclass(slots=True)
class TranscriptExtractionProviderBinding:
    provider: TranscriptExtractionProvider
    provider_name: str
    mode: str
    version_context: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class MediaUnderstandingProviderBinding:
    provider: MediaUnderstandingProvider
    provider_name: str
    mode: str
    fallback_applied: bool = False
    fallback_reason: str | None = None
    fallback_stage: str | None = None
    version_context: dict[str, object] = field(default_factory=dict)
    extraction_provider_name: str | None = None
    extraction_provider_mode: str | None = None
    extraction_version_context: dict[str, object] = field(default_factory=dict)


class MockMediaUnderstandingProvider:
    def __init__(
        self,
        stub_lines: list[AttributedTranscriptLine] | None = None,
        *,
        default_speaker_id: str = "speaker_default",
        default_speaker_name: str | None = "Speaker 1",
        normalizer: AttributedTranscriptNormalizer | None = None,
    ) -> None:
        self.stub_lines = stub_lines or []
        self.default_speaker_id = default_speaker_id
        self.default_speaker_name = default_speaker_name
        self.normalizer = normalizer or AttributedTranscriptNormalizer()

    def load_attributed_transcript(self, source: MediaSource) -> list[AttributedTranscriptLine]:
        if self.stub_lines:
            return self.normalizer.normalize(self.stub_lines)
        if source.kind == MediaSourceKind.ATTRIBUTED_TRANSCRIPT and source.attributed_lines:
            return self.normalizer.normalize(source.attributed_lines)
        if source.kind == MediaSourceKind.TRANSCRIPT and source.transcript_lines:
            return self.normalizer.normalize(
                _build_default_attributed_lines(
                    source.transcript_lines,
                    default_speaker_id=self.default_speaker_id,
                    default_speaker_name=self.default_speaker_name,
                )
            )

        fallback_text = source.describe()
        return self.normalizer.normalize(
            [
                AttributedTranscriptLine(
                    index=1,
                    start_ms=0,
                    end_ms=1_000,
                    speaker_id=self.default_speaker_id,
                    speaker_name=self.default_speaker_name,
                    source_text=fallback_text,
                )
            ]
        )

    def get_cache_context(self) -> dict[str, object]:
        return {
            "provider_variant": "mock_media_understanding_v1",
            "default_speaker_strategy": "single_speaker_default",
        }


@dataclass(slots=True)
class LocalTranscriptProvider:
    """Resolve already-available transcript inputs into attributed transcript lines."""

    default_speaker_id: str = "speaker_default"
    default_speaker_name: str | None = None
    normalizer: AttributedTranscriptNormalizer | None = None

    def load_attributed_transcript(self, source: MediaSource) -> list[AttributedTranscriptLine]:
        normalizer = self.normalizer or AttributedTranscriptNormalizer()
        _validate_default_speaker_id(self.default_speaker_id)

        if source.kind == MediaSourceKind.ATTRIBUTED_TRANSCRIPT:
            if not source.attributed_lines:
                raise MediaUnderstandingError("Attributed transcript source is missing attributed_lines.")
            return normalizer.normalize(source.attributed_lines)

        if source.kind != MediaSourceKind.TRANSCRIPT:
            raise MediaUnderstandingUnsupportedSourceKindError(
                f"LocalTranscriptProvider only supports transcript-like sources, got {source.kind.value}."
            )
        if not source.transcript_lines:
            raise MediaUnderstandingError("Transcript source is missing transcript_lines.")

        attributed_lines = _build_default_attributed_lines(
            source.transcript_lines,
            default_speaker_id=self.default_speaker_id,
            default_speaker_name=self.default_speaker_name,
        )
        return normalizer.normalize(attributed_lines)

    def get_cache_context(self) -> dict[str, object]:
        return {
            "provider_variant": "local_transcript_v1",
            "default_speaker_strategy": "single_speaker_default",
        }


@dataclass(slots=True)
class LocalSRTProvider:
    """Resolve local SRT input into attributed transcript lines with an explicit single-speaker default."""

    default_speaker_id: str = "speaker_default"
    default_speaker_name: str | None = None
    loader: SRTSubtitleLoader | None = None
    normalizer: AttributedTranscriptNormalizer | None = None

    def load_attributed_transcript(self, source: MediaSource) -> list[AttributedTranscriptLine]:
        if source.kind != MediaSourceKind.LOCAL_SRT:
            raise MediaUnderstandingUnsupportedSourceKindError(
                f"LocalSRTProvider only supports local_srt sources, got {source.kind.value}."
            )
        if not source.locator:
            raise MediaUnderstandingError("Local SRT source requires locator path.")

        _validate_default_speaker_id(self.default_speaker_id)
        loader = self.loader or SRTSubtitleLoader()
        normalizer = self.normalizer or AttributedTranscriptNormalizer()
        srt_path = Path(source.locator)
        if not srt_path.exists():
            raise MediaUnderstandingError(f"SRT file not found: {source.locator}")

        content = srt_path.read_text(encoding="utf-8-sig")
        seeds = loader.parse_content(
            content=content,
            default_speaker_id=self.default_speaker_id,
            default_speaker_name=self.default_speaker_name,
        )
        attributed_lines = [
            AttributedTranscriptLine(
                index=seed.index if seed.index is not None else position,
                start_ms=seed.start_ms,
                end_ms=seed.end_ms,
                speaker_id=seed.speaker_id,
                speaker_name=seed.speaker_name,
                source_text=seed.en_text,
            )
            for position, seed in enumerate(seeds, start=1)
        ]
        return normalizer.normalize(attributed_lines)

    def get_cache_context(self) -> dict[str, object]:
        return {
            "provider_variant": "local_srt_v1",
            "default_speaker_strategy": "single_speaker_default",
        }


class FutureMultimodalMediaUnderstandingProviderSkeleton:
    """Direct multimodal attribution boundary for future provider work."""

    def __init__(
        self,
        config: FutureMultimodalMediaUnderstandingProviderConfig | None = None,
        *,
        provider_name: str | None = None,
    ) -> None:
        if config is not None:
            self.config = config
        elif provider_name is not None:
            self.config = FutureMultimodalMediaUnderstandingProviderConfig(provider_name=provider_name)
        else:
            self.config = FutureMultimodalMediaUnderstandingProviderConfig()
        self.config.validate()

    def load_attributed_transcript(self, source: MediaSource) -> list[AttributedTranscriptLine]:
        if source.kind not in self.config.supported_source_kinds:
            raise MediaUnderstandingUnsupportedSourceKindError(
                f"{self.config.provider_name} skeleton only targets youtube_url, local_video, and local_audio; "
                f"got {source.kind.value}."
            )
        _validate_future_multimodal_source(source)
        raise MediaUnderstandingProviderUnavailableError(
            f"{self.config.provider_name} skeleton is not connected in this sprint: {source.describe()}"
        )

    def get_cache_context(self) -> dict[str, object]:
        return {
            "provider_variant": "future_multimodal_media_understanding_v1",
            "network_connected": False,
            "supported_source_kinds": [kind.value for kind in self.config.supported_source_kinds],
        }


class SystemSpeechLocalASRTranscriptExtractionProvider:
    """Minimal real local-ASR provider using Windows System.Speech for local_audio."""

    def __init__(self, config: LocalASRTranscriptExtractionProviderConfig | None = None) -> None:
        self.config = config or LocalASRTranscriptExtractionProviderConfig()
        self.config.validate()

    def extract_transcript(self, request: TranscriptExtractionRequest) -> TranscriptExtractionResult:
        if request.source_kind not in self.config.supported_source_kinds:
            raise MediaUnderstandingUnsupportedSourceKindError(
                f"{self.config.provider_name} only supports local_audio in this sprint; "
                f"got {request.source_kind.value}."
            )
        _validate_local_audio_transcript_extraction_request(request, self.config)
        result = self._run_backend(request)
        sanitized_result = _sanitize_system_speech_transcript_extraction_result(
            result,
            expected_request=request,
            config=self.config,
        )
        return _normalize_extracted_transcript_result(sanitized_result, expected_request=request)

    def _run_backend(self, request: TranscriptExtractionRequest) -> TranscriptExtractionResult:
        return _run_system_speech_transcription(request, self.config)

    def get_cache_context(self) -> dict[str, object]:
        return {
            "provider_variant": self.config.provider_variant,
            "provider_mode": self.config.provider_mode,
            "model_name": self.config.model_name,
            "language": self.config.language,
            "task": self.config.task,
            "runtime_backend": "windows_system_speech",
            "supported_extensions": list(self.config.supported_extensions),
            "audio_input_contract": "pcm_wav_mono_16bit_non_empty",
            "timing_strategy": "recognizer_offsets_with_sequential_fallback",
            "supported_source_kinds": [kind.value for kind in self.config.supported_source_kinds],
        }


class LocalASRTranscriptExtractionProviderSkeleton:
    """Placeholder for a future local ASR-style transcript extraction provider."""

    def __init__(self, config: LocalASRTranscriptExtractionProviderConfig | None = None) -> None:
        self.config = config or LocalASRTranscriptExtractionProviderConfig()
        self.config.validate()

    def extract_transcript(self, request: TranscriptExtractionRequest) -> TranscriptExtractionResult:
        if request.source_kind not in self.config.supported_source_kinds:
            raise MediaUnderstandingUnsupportedSourceKindError(
                f"{self.config.provider_name} skeleton only targets "
                f"{', '.join(kind.value for kind in self.config.supported_source_kinds)}; "
                f"got {request.source_kind.value}."
            )
        _validate_transcript_extraction_request(request)
        raise MediaUnderstandingTranscriptExtractionUnavailableError(
            f"{self.config.provider_name} skeleton is not connected in this sprint: {request.source_path}"
        )

    def get_cache_context(self) -> dict[str, object]:
        return {
            "provider_variant": self.config.provider_variant,
            "network_connected": False,
            "supported_source_kinds": [kind.value for kind in self.config.supported_source_kinds],
        }


class CommandTranscriptExtractionProviderSkeleton:
    """Placeholder for a future command-driven transcript extraction path."""

    def __init__(self, config: CommandTranscriptExtractionProviderConfig | None = None) -> None:
        self.config = config or CommandTranscriptExtractionProviderConfig()
        self.config.validate()

    def extract_transcript(self, request: TranscriptExtractionRequest) -> TranscriptExtractionResult:
        if request.source_kind not in self.config.supported_source_kinds:
            raise MediaUnderstandingUnsupportedSourceKindError(
                f"{self.config.provider_name} skeleton only targets "
                f"{', '.join(kind.value for kind in self.config.supported_source_kinds)}; "
                f"got {request.source_kind.value}."
            )
        _validate_transcript_extraction_request(request)
        raise MediaUnderstandingTranscriptExtractionUnavailableError(
            f"{self.config.provider_name} skeleton is not connected in this sprint: {request.source_path}"
        )

    def get_cache_context(self) -> dict[str, object]:
        return {
            "provider_variant": self.config.provider_variant,
            "runtime_backend": "external_command_stub",
            "network_connected": False,
            "supported_source_kinds": [kind.value for kind in self.config.supported_source_kinds],
        }


class FutureMultimodalTranscriptExtractionProviderSkeleton:
    """Placeholder for future multimodal transcript extraction from local media."""

    def __init__(self, config: FutureMultimodalTranscriptExtractionProviderConfig | None = None) -> None:
        self.config = config or FutureMultimodalTranscriptExtractionProviderConfig()
        self.config.validate()

    def extract_transcript(self, request: TranscriptExtractionRequest) -> TranscriptExtractionResult:
        if request.source_kind not in self.config.supported_source_kinds:
            raise MediaUnderstandingUnsupportedSourceKindError(
                f"{self.config.provider_name} skeleton only targets local_video and local_audio; "
                f"got {request.source_kind.value}."
            )
        _validate_transcript_extraction_request(request)
        raise MediaUnderstandingTranscriptExtractionUnavailableError(
            f"{self.config.provider_name} skeleton is not connected in this sprint: {request.source_path}"
        )

    def get_cache_context(self) -> dict[str, object]:
        return {
            "provider_variant": self.config.provider_variant,
            "network_connected": False,
            "supported_source_kinds": [kind.value for kind in self.config.supported_source_kinds],
        }


@dataclass(slots=True)
class TranscriptExtractionMediaUnderstandingProvider:
    """Adapt extracted transcript lines into attributed transcript output."""

    extraction_provider: TranscriptExtractionProvider
    provider_name: str = "transcript_extraction_adapter"
    provider_variant: str = "transcript_extraction_adapter_v1"
    default_speaker_id: str = "speaker_default"
    default_speaker_name: str | None = None
    normalizer: AttributedTranscriptNormalizer | None = None
    extraction_provider_name: str | None = None
    extraction_provider_mode: str | None = None
    extraction_version_context: dict[str, object] = field(default_factory=dict)

    def build_extraction_request(self, source: MediaSource) -> TranscriptExtractionRequest:
        if source.kind not in {MediaSourceKind.LOCAL_VIDEO, MediaSourceKind.LOCAL_AUDIO}:
            raise MediaUnderstandingUnsupportedSourceKindError(
                f"{self.provider_name} only supports local_video and local_audio sources, got {source.kind.value}."
            )
        request = _build_transcript_extraction_request(source)
        _validate_source_kind_ready_for_extraction_provider(
            request,
            extraction_provider=self.extraction_provider,
            extraction_provider_name=self.extraction_provider_name or type(self.extraction_provider).__name__,
            extraction_provider_mode=self.extraction_provider_mode,
        )
        return request

    def extract_transcript_result(self, source: MediaSource) -> TranscriptExtractionResult:
        request = self.build_extraction_request(source)
        extraction_result = self.extraction_provider.extract_transcript(request)
        return _normalize_extracted_transcript_result(extraction_result, expected_request=request)

    def load_attributed_transcript(self, source: MediaSource) -> list[AttributedTranscriptLine]:
        normalizer = self.normalizer or AttributedTranscriptNormalizer()
        _validate_default_speaker_id(self.default_speaker_id)
        extraction_result = self.extract_transcript_result(source)
        normalized_extracted_lines = extraction_result.transcript_lines
        attributed_lines = _build_default_attributed_lines(
            normalized_extracted_lines,
            default_speaker_id=self.default_speaker_id,
            default_speaker_name=self.default_speaker_name,
        )
        return normalizer.normalize(attributed_lines)

    def get_cache_context(self) -> dict[str, object]:
        return {
            "provider_variant": self.provider_variant,
            "default_speaker_strategy": "single_speaker_default",
            "extraction_provider_name": self.extraction_provider_name,
            "extraction_provider_mode": self.extraction_provider_mode,
            "extraction_version_context": dict(self.extraction_version_context),
        }


def _normalize_transcript_extraction_selection(
    selection: TranscriptExtractionProviderSelectionConfig,
) -> tuple[str, str]:
    raw_mode = selection.mode.strip().lower()
    raw_provider = selection.provider.strip().lower()

    if raw_mode == "local_asr":
        return "system_speech_local_asr", "real"
    if raw_mode == "local_asr_skeleton":
        return "system_speech_local_asr", "skeleton"
    if raw_mode in {"multimodal", "multimodal_skeleton", "future_multimodal"}:
        return "gemini_like_multimodal_extraction", "skeleton"

    provider_aliases = {
        "local_asr": "system_speech_local_asr",
        "system_speech": "system_speech_local_asr",
        "system_speech_local_asr": "system_speech_local_asr",
        "command": "command_transcript_extraction",
        "command_transcript_extraction": "command_transcript_extraction",
        "external_command": "command_transcript_extraction",
        "multimodal": "gemini_like_multimodal_extraction",
        "future_multimodal": "gemini_like_multimodal_extraction",
        "gemini_like_multimodal_extraction": "gemini_like_multimodal_extraction",
    }
    normalized_provider = provider_aliases.get(raw_provider or "system_speech_local_asr")
    if normalized_provider is None:
        raise MediaUnderstandingConfigurationError(
            f"Unsupported transcript extraction provider: {selection.provider}"
        )

    normalized_mode = raw_mode or "real"
    if normalized_mode == "stub":
        normalized_mode = "skeleton"
    if normalized_provider in {"command_transcript_extraction", "gemini_like_multimodal_extraction"}:
        normalized_mode = "skeleton"
    if normalized_mode not in {"real", "skeleton"}:
        raise MediaUnderstandingConfigurationError(
            f"Unsupported transcript extraction mode: {selection.mode}"
        )
    return normalized_provider, normalized_mode


def resolve_transcript_extraction_provider(
    selection: TranscriptExtractionProviderSelectionConfig,
) -> TranscriptExtractionProviderBinding:
    provider_key, normalized_mode = _normalize_transcript_extraction_selection(selection)

    if provider_key == "system_speech_local_asr":
        config = selection.local_asr
        config.validate()
        if normalized_mode == "real":
            provider = SystemSpeechLocalASRTranscriptExtractionProvider(config=config)
            return TranscriptExtractionProviderBinding(
                provider=provider,
                provider_name=config.provider_name,
                mode="local_asr",
                version_context=_get_transcript_extraction_cache_context(provider),
            )
        if normalized_mode == "skeleton":
            provider = LocalASRTranscriptExtractionProviderSkeleton(config=config)
            return TranscriptExtractionProviderBinding(
                provider=provider,
                provider_name=config.provider_name,
                mode="local_asr_skeleton",
                version_context=_get_transcript_extraction_cache_context(provider),
            )
        raise MediaUnderstandingConfigurationError(
            f"Unsupported transcript extraction mode for {config.provider_name}: {selection.mode}"
        )

    if provider_key == "command_transcript_extraction":
        config = selection.command
        config.validate()
        provider = CommandTranscriptExtractionProviderSkeleton(config=config)
        return TranscriptExtractionProviderBinding(
            provider=provider,
            provider_name=config.provider_name,
            mode="command_skeleton",
            version_context=_get_transcript_extraction_cache_context(provider),
        )

    if provider_key == "gemini_like_multimodal_extraction":
        config = selection.multimodal
        config.validate()
        provider = FutureMultimodalTranscriptExtractionProviderSkeleton(config=config)
        return TranscriptExtractionProviderBinding(
            provider=provider,
            provider_name=config.provider_name,
            mode="multimodal_skeleton",
            version_context=_get_transcript_extraction_cache_context(provider),
        )

    raise MediaUnderstandingConfigurationError(
        f"Unsupported transcript extraction provider: {selection.provider or selection.mode}"
    )


def resolve_media_understanding_provider(
    selection: MediaUnderstandingProviderSelectionConfig,
) -> MediaUnderstandingProviderBinding:
    mode = selection.mode.strip().lower()

    if mode == "mock":
        config = selection.mock
        config.validate()
        provider = MockMediaUnderstandingProvider(
            stub_lines=config.stub_lines,
            default_speaker_id=config.default_speaker_id,
            default_speaker_name=config.default_speaker_name,
        )
        return MediaUnderstandingProviderBinding(
            provider=provider,
            provider_name=config.provider_name,
            mode="mock",
            version_context=_get_provider_cache_context(provider),
        )

    if mode == "local_transcript":
        config = selection.local_transcript
        config.validate()
        provider = LocalTranscriptProvider(
            default_speaker_id=config.default_speaker_id,
            default_speaker_name=config.default_speaker_name,
        )
        return MediaUnderstandingProviderBinding(
            provider=provider,
            provider_name=config.provider_name,
            mode="local_transcript",
            version_context=_get_provider_cache_context(provider),
        )

    if mode == "local_srt":
        config = selection.local_srt
        config.validate()
        provider = LocalSRTProvider(
            default_speaker_id=config.default_speaker_id,
            default_speaker_name=config.default_speaker_name,
        )
        return MediaUnderstandingProviderBinding(
            provider=provider,
            provider_name=config.provider_name,
            mode="local_srt",
            version_context=_get_provider_cache_context(provider),
        )

    if mode == "multimodal_attribution_skeleton":
        config = selection.multimodal
        config.validate()
        provider = FutureMultimodalMediaUnderstandingProviderSkeleton(config=config)
        return MediaUnderstandingProviderBinding(
            provider=provider,
            provider_name=config.provider_name,
            mode="multimodal_attribution_skeleton",
            version_context=_get_provider_cache_context(provider),
        )

    if mode in {"transcript_extraction", "multimodal_skeleton", "local_asr", "local_asr_skeleton"}:
        adapter_config = selection.transcript_extraction
        adapter_config.validate()
        extraction_selection = _build_transcript_extraction_selection_from_media_selection(selection)
        if mode == "multimodal_skeleton":
            extraction_selection.mode = "multimodal_skeleton"
        if mode == "local_asr":
            extraction_selection.mode = "local_asr"
        if mode == "local_asr_skeleton":
            extraction_selection.mode = "local_asr_skeleton"
        extraction_binding = resolve_transcript_extraction_provider(extraction_selection)
        provider = TranscriptExtractionMediaUnderstandingProvider(
            extraction_provider=extraction_binding.provider,
            provider_name=adapter_config.provider_name,
            provider_variant=adapter_config.provider_variant,
            default_speaker_id=adapter_config.default_speaker_id,
            default_speaker_name=adapter_config.default_speaker_name,
            extraction_provider_name=extraction_binding.provider_name,
            extraction_provider_mode=extraction_binding.mode,
            extraction_version_context=extraction_binding.version_context,
        )
        return MediaUnderstandingProviderBinding(
            provider=provider,
            provider_name=adapter_config.provider_name,
            mode="transcript_extraction",
            version_context=_get_provider_cache_context(provider),
            extraction_provider_name=extraction_binding.provider_name,
            extraction_provider_mode=extraction_binding.mode,
            extraction_version_context=dict(extraction_binding.version_context),
        )

    raise MediaUnderstandingConfigurationError(f"Unsupported media understanding mode: {selection.mode}")


def bind_inline_media_understanding_provider(
    provider: MediaUnderstandingProvider,
) -> MediaUnderstandingProviderBinding:
    if isinstance(provider, MockMediaUnderstandingProvider):
        return MediaUnderstandingProviderBinding(
            provider=provider,
            provider_name="mock_media_understanding",
            mode="mock",
            version_context=_get_provider_cache_context(provider),
        )
    if isinstance(provider, LocalTranscriptProvider):
        return MediaUnderstandingProviderBinding(
            provider=provider,
            provider_name="local_transcript",
            mode="local_transcript",
            version_context=_get_provider_cache_context(provider),
        )
    if isinstance(provider, LocalSRTProvider):
        return MediaUnderstandingProviderBinding(
            provider=provider,
            provider_name="local_srt",
            mode="local_srt",
            version_context=_get_provider_cache_context(provider),
        )
    if isinstance(provider, FutureMultimodalMediaUnderstandingProviderSkeleton):
        return MediaUnderstandingProviderBinding(
            provider=provider,
            provider_name=provider.config.provider_name,
            mode="multimodal_attribution_skeleton",
            version_context=_get_provider_cache_context(provider),
        )
    if isinstance(provider, TranscriptExtractionMediaUnderstandingProvider):
        return MediaUnderstandingProviderBinding(
            provider=provider,
            provider_name=provider.provider_name,
            mode="transcript_extraction",
            version_context=_get_provider_cache_context(provider),
            extraction_provider_name=provider.extraction_provider_name,
            extraction_provider_mode=provider.extraction_provider_mode,
            extraction_version_context=dict(provider.extraction_version_context),
        )
    return MediaUnderstandingProviderBinding(
        provider=provider,
        provider_name=type(provider).__name__,
        mode="custom",
        version_context=_get_provider_cache_context(provider),
    )


def classify_media_understanding_error(exc: Exception) -> dict[str, object]:
    if isinstance(exc, MediaUnderstandingTranscriptExtractionModelError):
        return {"error_type": "transcript_extraction_model_error"}
    if isinstance(exc, MediaUnderstandingConfigurationError):
        return {"error_type": "configuration_error"}
    if isinstance(exc, MediaUnderstandingUnsupportedSourceKindError):
        return {"error_type": "unsupported_source_kind"}
    if isinstance(exc, MediaUnderstandingInvalidSourcePathError):
        return {"error_type": "invalid_source_path"}
    if isinstance(exc, MediaUnderstandingTranscriptExtractionNoResultError):
        return {"error_type": "transcript_extraction_no_result"}
    if isinstance(exc, MediaUnderstandingTranscriptExtractionRuntimeError):
        return {"error_type": "transcript_extraction_runtime_error"}
    if isinstance(exc, MediaUnderstandingTranscriptExtractionUnavailableError):
        return {"error_type": "transcript_extraction_unavailable"}
    if isinstance(exc, MediaUnderstandingProviderUnavailableError):
        return {"error_type": "provider_unavailable"}
    if isinstance(exc, MediaUnderstandingExtractedTranscriptOutputError):
        return {"error_type": "invalid_extracted_transcript_output"}
    if isinstance(exc, MediaUnderstandingOutputError):
        return {"error_type": "invalid_attributed_transcript_output"}
    return {"error_type": "media_understanding_error"}


def _build_default_attributed_lines(
    transcript_lines: list[TranscriptLine],
    *,
    default_speaker_id: str,
    default_speaker_name: str | None,
) -> list[AttributedTranscriptLine]:
    return [
        AttributedTranscriptLine(
            index=line.index,
            start_ms=line.start_ms,
            end_ms=line.end_ms,
            speaker_id=default_speaker_id,
            speaker_name=default_speaker_name,
            source_text=line.source_text,
        )
        for line in transcript_lines
    ]


def _build_transcript_extraction_request(source: MediaSource) -> TranscriptExtractionRequest:
    _validate_local_media_source(source)
    assert source.locator is not None
    return TranscriptExtractionRequest(
        source_kind=source.kind,
        source_path=source.locator,
        metadata=dict(source.metadata),
    )


def _normalize_extracted_transcript_result(
    result: TranscriptExtractionResult,
    *,
    expected_request: TranscriptExtractionRequest,
) -> TranscriptExtractionResult:
    if not result.provider_name.strip():
        raise MediaUnderstandingExtractedTranscriptOutputError(
            "Transcript extraction result requires a non-empty provider_name."
        )
    if not result.provider_mode.strip():
        raise MediaUnderstandingExtractedTranscriptOutputError(
            "Transcript extraction result requires a non-empty provider_mode."
        )
    if result.request.source_kind != expected_request.source_kind:
        raise MediaUnderstandingExtractedTranscriptOutputError(
            "Transcript extraction result source_kind does not match the request."
        )
    if result.request.source_path != expected_request.source_path:
        raise MediaUnderstandingExtractedTranscriptOutputError(
            "Transcript extraction result source_path does not match the request."
        )
    return TranscriptExtractionResult(
        request=TranscriptExtractionRequest(
            source_kind=result.request.source_kind,
            source_path=result.request.source_path,
            metadata=dict(result.request.metadata),
        ),
        transcript_lines=_normalize_extracted_transcript_lines(result.transcript_lines),
        provider_name=result.provider_name.strip(),
        provider_mode=result.provider_mode.strip(),
        version_context=dict(result.version_context),
    )


def _sanitize_system_speech_transcript_extraction_result(
    result: TranscriptExtractionResult,
    *,
    expected_request: TranscriptExtractionRequest,
    config: LocalASRTranscriptExtractionProviderConfig,
) -> TranscriptExtractionResult:
    recognizer_language = str(result.version_context.get("language", config.language))
    if not result.transcript_lines:
        raise MediaUnderstandingTranscriptExtractionNoResultError(
            f"{config.provider_name} returned no recognizable speech. "
            f"The installed recognizer ({recognizer_language}) may not match the input language/content, "
            "or the audio may contain only silence/noise."
        )

    sanitized_lines = _sanitize_extracted_transcript_lines(result.transcript_lines)
    if not sanitized_lines:
        raise MediaUnderstandingTranscriptExtractionNoResultError(
            f"{config.provider_name} produced no usable transcript lines after normalization. "
            f"The installed recognizer ({recognizer_language}) may not match the input language/content, "
            "or the audio may contain only silence/noise."
        )

    version_context = dict(result.version_context)
    version_context.setdefault("provider_variant", config.provider_variant)
    version_context.setdefault("provider_mode", config.provider_mode)
    version_context.setdefault("model_name", config.model_name)
    version_context.setdefault("language", recognizer_language)
    version_context.setdefault("task", config.task)
    version_context.setdefault("runtime_backend", "windows_system_speech")
    version_context.setdefault("timing_strategy", "recognizer_offsets_with_sequential_fallback")
    version_context.setdefault("supported_extensions", list(config.supported_extensions))
    version_context.setdefault("audio_input_contract", "pcm_wav_mono_16bit_non_empty")
    return TranscriptExtractionResult(
        request=TranscriptExtractionRequest(
            source_kind=expected_request.source_kind,
            source_path=expected_request.source_path,
            metadata=dict(expected_request.metadata),
        ),
        transcript_lines=sanitized_lines,
        provider_name=result.provider_name,
        provider_mode=result.provider_mode,
        version_context=version_context,
    )


def _sanitize_extracted_transcript_lines(lines: list[TranscriptLine]) -> list[TranscriptLine]:
    sanitized_lines: list[TranscriptLine] = []
    previous_end_ms = 0
    for raw_line in lines:
        normalized_text = _normalize_extracted_source_text(raw_line.source_text)
        if not normalized_text or _looks_like_noise_only_text(normalized_text):
            continue
        start_ms, end_ms = _normalize_extracted_line_timing(
            start_ms=raw_line.start_ms,
            end_ms=raw_line.end_ms,
            previous_end_ms=previous_end_ms,
            source_text=normalized_text,
        )
        sanitized_lines.append(
            TranscriptLine(
                index=len(sanitized_lines) + 1,
                start_ms=start_ms,
                end_ms=end_ms,
                source_text=normalized_text,
            )
        )
        previous_end_ms = end_ms
    return sanitized_lines


def _normalize_extracted_source_text(source_text: str) -> str:
    collapsed_text = re.sub(r"\s+", " ", source_text).strip()
    return collapsed_text


def _looks_like_noise_only_text(source_text: str) -> bool:
    return not any(character.isalnum() for character in source_text)


def _normalize_extracted_line_timing(
    *,
    start_ms: int,
    end_ms: int,
    previous_end_ms: int,
    source_text: str,
) -> tuple[int, int]:
    estimated_duration_ms = _estimate_transcript_line_duration_ms(source_text)
    normalized_start_ms = max(0, int(start_ms))
    normalized_end_ms = int(end_ms)

    if normalized_end_ms <= normalized_start_ms:
        normalized_start_ms = max(previous_end_ms, normalized_start_ms)
        return normalized_start_ms, normalized_start_ms + estimated_duration_ms

    if normalized_start_ms < previous_end_ms:
        observed_duration_ms = max(1, normalized_end_ms - normalized_start_ms)
        normalized_start_ms = previous_end_ms
        return normalized_start_ms, normalized_start_ms + observed_duration_ms

    return normalized_start_ms, normalized_end_ms


def _estimate_transcript_line_duration_ms(source_text: str) -> int:
    token_count = max(1, len(source_text.split()))
    if token_count == 1:
        token_count = max(token_count, max(1, len(source_text) // 4))
    return max(900, min(6_000, token_count * 450))


def _normalize_extracted_transcript_lines(lines: list[TranscriptLine]) -> list[TranscriptLine]:
    if not lines:
        raise MediaUnderstandingExtractedTranscriptOutputError(
            "Transcript extraction provider returned no transcript lines."
        )

    normalized_lines: list[TranscriptLine] = []
    used_indices: set[int] = set()
    for line in lines:
        source_text = line.source_text.strip()
        if not source_text:
            raise MediaUnderstandingExtractedTranscriptOutputError(
                "Extracted transcript lines require non-empty source_text."
            )
        if line.end_ms <= line.start_ms:
            raise MediaUnderstandingExtractedTranscriptOutputError(
                "Extracted transcript lines require end_ms greater than start_ms."
            )
        if line.index in used_indices:
            raise MediaUnderstandingExtractedTranscriptOutputError(
                f"Duplicate extracted transcript index detected: {line.index}"
            )
        used_indices.add(line.index)
        normalized_lines.append(
            TranscriptLine(
                index=line.index,
                start_ms=line.start_ms,
                end_ms=line.end_ms,
                source_text=source_text,
            )
        )
    return normalized_lines


def _validate_default_speaker_id(default_speaker_id: str) -> None:
    if not default_speaker_id.strip():
        raise MediaUnderstandingConfigurationError(
            "default_speaker_id is required for single-speaker transcript inputs."
        )


def _run_system_speech_transcription(
    request: TranscriptExtractionRequest,
    config: LocalASRTranscriptExtractionProviderConfig,
) -> TranscriptExtractionResult:
    output_path = ""
    script_path = ""
    temp_output_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    temp_output_file.close()
    output_path = temp_output_file.name
    temp_script_file = tempfile.NamedTemporaryFile(suffix=".ps1", delete=False, mode="w", encoding="utf-8")
    temp_script_file.write(_build_system_speech_transcription_script())
    temp_script_file.close()
    script_path = temp_script_file.name
    env = os.environ.copy()
    env.update(
        {
            "AUTOVIDEO_ASR_SOURCE_PATH": request.source_path,
            "AUTOVIDEO_ASR_OUTPUT_PATH": output_path,
            "AUTOVIDEO_ASR_LANGUAGE": config.language,
            "AUTOVIDEO_ASR_PROVIDER_NAME": config.provider_name,
            "AUTOVIDEO_ASR_PROVIDER_MODE": config.provider_mode,
            "AUTOVIDEO_ASR_PROVIDER_VARIANT": config.provider_variant,
            "AUTOVIDEO_ASR_MODEL_NAME": config.model_name,
            "AUTOVIDEO_ASR_TASK": config.task,
        }
    )

    try:
        try:
            completed = subprocess.run(
                [
                    config.powershell_executable,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    script_path,
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=config.command_timeout_ms / 1000,
                check=False,
            )
        except FileNotFoundError as exc:
            raise MediaUnderstandingTranscriptExtractionUnavailableError(
                f"{config.provider_name} requires a local PowerShell runtime."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaUnderstandingTranscriptExtractionRuntimeError(
                f"{config.provider_name} timed out after {config.command_timeout_ms} ms."
            ) from exc

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        details = stdout or stderr

        if completed.returncode != 0:
            if "NO_RECOGNIZER_FOR_LANGUAGE:" in details:
                raise MediaUnderstandingTranscriptExtractionModelError(
                    f"{config.provider_name} has no installed recognizer for language {config.language}."
                )
            if "SYSTEM_SPEECH_UNAVAILABLE:" in details:
                raise MediaUnderstandingTranscriptExtractionUnavailableError(
                    f"{config.provider_name} could not load Windows System.Speech."
                )
            raise MediaUnderstandingTranscriptExtractionRuntimeError(
                f"{config.provider_name} failed while processing local_audio: {details or 'unknown runtime error'}"
            )

        payload_text = Path(output_path).read_text(encoding="utf-8-sig").strip()
        if not payload_text:
            raise MediaUnderstandingTranscriptExtractionRuntimeError(
                f"{config.provider_name} returned an empty transcript response."
            )

        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise MediaUnderstandingTranscriptExtractionRuntimeError(
                f"{config.provider_name} returned invalid JSON transcript output."
            ) from exc
    finally:
        if script_path and Path(script_path).exists():
            Path(script_path).unlink(missing_ok=True)
        if output_path and Path(output_path).exists():
            Path(output_path).unlink(missing_ok=True)

    transcript_items = payload.get("transcript_lines")
    if not isinstance(transcript_items, list):
        raise MediaUnderstandingExtractedTranscriptOutputError(
            "Transcript extraction result requires transcript_lines."
        )
    if not transcript_items:
        raise MediaUnderstandingTranscriptExtractionNoResultError(
            f"{config.provider_name} returned no recognizable speech. "
            "The installed recognizer may not match the input language/content, or the audio may contain only silence/noise."
        )

    transcript_lines: list[TranscriptLine] = []
    for item in transcript_items:
        if not isinstance(item, dict):
            raise MediaUnderstandingExtractedTranscriptOutputError(
                "Transcript extraction transcript_lines must contain objects."
            )
        try:
            transcript_lines.append(
                TranscriptLine(
                    index=int(item["index"]),
                    start_ms=int(item["start_ms"]),
                    end_ms=int(item["end_ms"]),
                    source_text=str(item["source_text"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaUnderstandingExtractedTranscriptOutputError(
                "Transcript extraction transcript_lines contain invalid fields."
            ) from exc

    provider_name = payload.get("provider_name")
    provider_mode = payload.get("provider_mode")
    if not isinstance(provider_name, str) or not isinstance(provider_mode, str):
        raise MediaUnderstandingExtractedTranscriptOutputError(
            "Transcript extraction result requires provider_name and provider_mode."
        )

    version_context = {
        "provider_variant": str(payload.get("provider_variant", config.provider_variant)),
        "provider_mode": provider_mode,
        "model_name": str(payload.get("model_name", config.model_name)),
        "language": str(payload.get("language", config.language)),
        "task": str(payload.get("task", config.task)),
        "runtime_backend": "windows_system_speech",
        "timing_strategy": "recognizer_offsets_with_sequential_fallback",
        "supported_extensions": list(config.supported_extensions),
        "audio_input_contract": "pcm_wav_mono_16bit_non_empty",
    }
    return TranscriptExtractionResult(
        request=request,
        transcript_lines=transcript_lines,
        provider_name=provider_name,
        provider_mode=provider_mode,
        version_context=version_context,
    )


def _build_system_speech_transcription_script() -> str:
    return """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

$sourcePath = $env:AUTOVIDEO_ASR_SOURCE_PATH
$outputPath = $env:AUTOVIDEO_ASR_OUTPUT_PATH
$language = $env:AUTOVIDEO_ASR_LANGUAGE
$providerName = $env:AUTOVIDEO_ASR_PROVIDER_NAME
$providerMode = $env:AUTOVIDEO_ASR_PROVIDER_MODE
$providerVariant = $env:AUTOVIDEO_ASR_PROVIDER_VARIANT
$modelName = $env:AUTOVIDEO_ASR_MODEL_NAME
$task = $env:AUTOVIDEO_ASR_TASK

$recognizers = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
if (-not $recognizers -or $recognizers.Count -eq 0) {
    throw 'SYSTEM_SPEECH_UNAVAILABLE:no_installed_recognizers'
}

$recognizer = $null
if ($language -eq 'auto') {
    $recognizer = $recognizers | Select-Object -First 1
}
else {
    $recognizer = $recognizers | Where-Object { $_.Culture.Name -eq $language } | Select-Object -First 1
}
if (-not $recognizer) {
    throw ('NO_RECOGNIZER_FOR_LANGUAGE:' + $language)
}

$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($recognizer)
try {
    $engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
    $engine.SetInputToWaveFile($sourcePath)

    $lines = @()
    $index = 1
    while ($true) {
        try {
            $result = $engine.Recognize()
        }
        catch [System.InvalidOperationException] {
            $recognizeMessage = $_.Exception.Message
            if (
                $recognizeMessage -like '*No audio input is supplied*' -or
                $recognizeMessage -like '*没有将任何音频输入提供给此识别器*' -or
                $recognizeMessage -like '*SetInputToWaveFile*'
            ) {
                break
            }
            throw
        }
        if ($null -eq $result) { break }
        if ([string]::IsNullOrWhiteSpace($result.Text)) { continue }

        $startMs = [int][math]::Round($result.Audio.AudioPosition.TotalMilliseconds)
        $endMs = [int][math]::Round(($result.Audio.AudioPosition + $result.Audio.Duration).TotalMilliseconds)
        $lines += [pscustomobject]@{
            index = $index
            start_ms = $startMs
            end_ms = $endMs
            source_text = $result.Text
        }
        $index += 1
    }

    $payload = [pscustomobject]@{
        provider_name = $providerName
        provider_mode = $providerMode
        provider_variant = $providerVariant
        model_name = $modelName
        language = $recognizer.Culture.Name
        task = $task
        transcript_lines = @($lines)
    }
    Set-Content -Path $outputPath -Value ($payload | ConvertTo-Json -Depth 5 -Compress) -Encoding UTF8
}
finally {
    $engine.Dispose()
}
""".strip()


def _validate_future_multimodal_source(source: MediaSource) -> None:
    if source.kind == MediaSourceKind.YOUTUBE_URL:
        if not source.locator or not source.locator.strip():
            raise MediaUnderstandingInvalidSourcePathError("YouTube source requires a non-empty locator.")
        return
    _validate_local_media_source(source)


def _validate_local_audio_transcript_extraction_request(
    request: TranscriptExtractionRequest,
    config: LocalASRTranscriptExtractionProviderConfig,
) -> None:
    _validate_transcript_extraction_request(request)
    if request.source_kind != MediaSourceKind.LOCAL_AUDIO:
        raise MediaUnderstandingUnsupportedSourceKindError(
            f"Real local ASR currently supports local_audio only, got {request.source_kind.value}."
        )
    if Path(request.source_path).suffix.lower() not in config.supported_extensions:
        raise MediaUnderstandingTranscriptExtractionRuntimeError(
            "The current local ASR provider only supports WAV/WAVE local_audio inputs in this sprint."
        )
    try:
        with wave.open(request.source_path, "rb") as wave_reader:
            if wave_reader.getnframes() <= 0:
                raise MediaUnderstandingTranscriptExtractionRuntimeError(
                    "The current local ASR provider requires non-empty local_audio input."
                )
            if wave_reader.getnchannels() != 1:
                raise MediaUnderstandingTranscriptExtractionRuntimeError(
                    "The current local ASR provider requires mono local_audio input."
                )
            if wave_reader.getsampwidth() != 2:
                raise MediaUnderstandingTranscriptExtractionRuntimeError(
                    "The current local ASR provider requires 16-bit PCM local_audio input."
                )
            if wave_reader.getcomptype() != "NONE":
                raise MediaUnderstandingTranscriptExtractionRuntimeError(
                    "The current local ASR provider requires uncompressed PCM WAV/WAVE local_audio input."
                )
    except (wave.Error, EOFError) as exc:
        raise MediaUnderstandingTranscriptExtractionRuntimeError(
            "The current local ASR provider requires a readable PCM WAV/WAVE local_audio input."
        ) from exc


def _validate_transcript_extraction_request(request: TranscriptExtractionRequest) -> None:
    if request.source_kind not in {MediaSourceKind.LOCAL_VIDEO, MediaSourceKind.LOCAL_AUDIO}:
        raise MediaUnderstandingUnsupportedSourceKindError(
            f"Transcript extraction only supports local_video and local_audio, got {request.source_kind.value}."
        )
    if not request.source_path.strip():
        raise MediaUnderstandingInvalidSourcePathError(
            f"{request.source_kind.value} source requires a local file path locator."
        )

    path = Path(request.source_path)
    if not path.exists():
        raise MediaUnderstandingInvalidSourcePathError(
            f"{request.source_kind.value} source file not found: {request.source_path}"
        )
    if not path.is_file():
        raise MediaUnderstandingInvalidSourcePathError(
            f"{request.source_kind.value} source path must point to a file: {request.source_path}"
        )


def _validate_local_media_source(source: MediaSource) -> None:
    if source.kind not in {MediaSourceKind.LOCAL_VIDEO, MediaSourceKind.LOCAL_AUDIO}:
        raise MediaUnderstandingUnsupportedSourceKindError(
            f"Local media transcript extraction only supports local_video and local_audio, got {source.kind.value}."
        )

    source_path = source.source_path()
    if not source_path or not source_path.strip():
        raise MediaUnderstandingInvalidSourcePathError(
            f"{source.kind.value} source requires a local file path locator."
        )

    path = Path(source_path)
    if not path.exists():
        raise MediaUnderstandingInvalidSourcePathError(
            f"{source.kind.value} source file not found: {source_path}"
        )
    if not path.is_file():
        raise MediaUnderstandingInvalidSourcePathError(
            f"{source.kind.value} source path must point to a file: {source_path}"
        )


def _build_transcript_extraction_selection_from_media_selection(
    selection: MediaUnderstandingProviderSelectionConfig,
) -> TranscriptExtractionProviderSelectionConfig:
    return TranscriptExtractionProviderSelectionConfig(
        provider=selection.extraction.provider,
        mode=selection.extraction.mode,
        local_asr=selection.extraction.local_asr,
        command=selection.extraction.command,
        multimodal=selection.extraction.multimodal,
    )


def _get_provider_cache_context(provider: MediaUnderstandingProvider) -> dict[str, object]:
    context_getter = getattr(provider, "get_cache_context", None)
    if callable(context_getter):
        context = context_getter()
        if isinstance(context, dict):
            return context
    return {}


def _get_transcript_extraction_cache_context(provider: TranscriptExtractionProvider) -> dict[str, object]:
    context_getter = getattr(provider, "get_cache_context", None)
    if callable(context_getter):
        context = context_getter()
        if isinstance(context, dict):
            return context
    return {}


def _validate_source_kind_ready_for_extraction_provider(
    request: TranscriptExtractionRequest,
    *,
    extraction_provider: TranscriptExtractionProvider,
    extraction_provider_name: str,
    extraction_provider_mode: str | None,
) -> None:
    if request.source_kind != MediaSourceKind.LOCAL_VIDEO:
        return

    supported_source_kinds = _read_supported_source_kinds(extraction_provider)
    if supported_source_kinds is None or MediaSourceKind.LOCAL_VIDEO in supported_source_kinds:
        return

    supported_kind_values = ", ".join(kind.value for kind in supported_source_kinds) or "none"
    mode_label = extraction_provider_mode or "unknown_mode"
    raise MediaUnderstandingTranscriptExtractionUnavailableError(
        "local_video transcript extraction path is not connected in this sprint. "
        f"Configured provider: {extraction_provider_name} ({mode_label}); "
        f"supported source kinds: {supported_kind_values}."
    )


def _read_supported_source_kinds(
    extraction_provider: TranscriptExtractionProvider,
) -> tuple[MediaSourceKind, ...] | None:
    provider_config = getattr(extraction_provider, "config", None)
    candidate_kinds = getattr(provider_config, "supported_source_kinds", None)
    if not isinstance(candidate_kinds, tuple):
        return None
    normalized_kinds: list[MediaSourceKind] = []
    for candidate in candidate_kinds:
        if isinstance(candidate, MediaSourceKind):
            normalized_kinds.append(candidate)
    return tuple(normalized_kinds) if normalized_kinds else None
