class PipelineError(Exception):
    """Base exception for the Sprint 1 pipeline."""


class ChunkingError(PipelineError):
    """Raised when semantic block construction fails."""


class IngestionError(PipelineError):
    """Raised when subtitle ingestion or normalization fails."""


class MediaUnderstandingError(PipelineError):
    """Raised when media understanding or transcript attribution cannot proceed."""


class MediaUnderstandingConfigurationError(MediaUnderstandingError):
    """Raised when media understanding provider configuration is missing or invalid."""


class MediaUnderstandingUnsupportedSourceKindError(MediaUnderstandingError):
    """Raised when a provider receives a source kind it does not support."""


class MediaUnderstandingProviderUnavailableError(MediaUnderstandingError):
    """Raised when a media understanding provider skeleton is not available yet."""


class MediaUnderstandingInvalidSourcePathError(MediaUnderstandingError):
    """Raised when a local media source path is missing, invalid, or inaccessible."""


class MediaUnderstandingTranscriptExtractionUnavailableError(MediaUnderstandingProviderUnavailableError):
    """Raised when a transcript extraction provider skeleton is not available yet."""


class MediaUnderstandingTranscriptExtractionModelError(MediaUnderstandingConfigurationError):
    """Raised when a transcript extraction model/runtime selection is invalid or unavailable locally."""


class MediaUnderstandingTranscriptExtractionRuntimeError(MediaUnderstandingError):
    """Raised when a real transcript extraction provider fails while processing local media."""


class MediaUnderstandingTranscriptExtractionNoResultError(MediaUnderstandingTranscriptExtractionRuntimeError):
    """Raised when transcript extraction completes but produces no usable transcript lines."""


class MediaUnderstandingOutputError(MediaUnderstandingError):
    """Raised when attributed transcript output is invalid or unusable."""


class MediaUnderstandingExtractedTranscriptOutputError(MediaUnderstandingOutputError):
    """Raised when extracted transcript output is invalid before attribution."""


class AudioProcessingError(PipelineError):
    """Raised when an audio file cannot be read."""


class TranslationError(PipelineError):
    """Raised when subtitle translation cannot proceed."""


class TranslationValidationError(TranslationError):
    """Raised when translated output does not match input expectations."""


class TranslationConfigurationError(TranslationError):
    """Raised when translation provider configuration is missing or invalid."""


class TranslationProviderUnavailableError(TranslationError):
    """Raised when a translation provider cannot be reached or used."""


class TranslationProviderResponseFormatError(TranslationError):
    """Raised when a provider response shape does not match the expected protocol."""


class TranslationProviderOutputError(TranslationValidationError):
    """Raised when a provider returns unusable translation output."""


class TranslationProviderLineCountError(TranslationProviderOutputError):
    """Raised when provider output does not preserve one-to-one line correspondence."""


class TTSError(PipelineError):
    """Raised when mock TTS synthesis fails."""


class TTSConfigurationError(TTSError):
    """Raised when TTS provider configuration is missing or invalid."""


class TTSProviderUnavailableError(TTSError):
    """Raised when a TTS provider cannot be reached or used."""


class TTSProviderTimeoutError(TTSProviderUnavailableError):
    """Raised when a TTS provider request times out."""


class TTSProviderNetworkError(TTSProviderUnavailableError):
    """Raised when a TTS provider request fails due to a network-like issue."""


class TTSProviderResponseFormatError(TTSError):
    """Raised when a TTS provider response shape is invalid."""


class TTSProviderOutputError(TTSError):
    """Raised when a TTS provider returns unusable output."""


class TTSInvalidAudioPayloadError(TTSProviderOutputError):
    """Raised when a TTS provider returns audio content that cannot be consumed."""


class TTSOutputFileWriteError(TTSError):
    """Raised when synthesized audio cannot be written to the local output path."""


class RewriteError(PipelineError):
    """Raised when text rewriting fails."""


class AlignmentError(PipelineError):
    """Raised when block alignment cannot proceed."""


class StateError(PipelineError):
    """Raised when project state cannot be read or written."""


class DraftError(PipelineError):
    """Raised when draft scaffold generation fails."""


class PublishError(PipelineError):
    """Raised when publish-output generation fails."""


class WorkflowError(PipelineError):
    """Raised when project workflow orchestration fails."""
