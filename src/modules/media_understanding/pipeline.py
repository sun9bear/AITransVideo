from core.models import SubtitleLine
from modules.media_understanding.models import (
    MediaSource,
    MediaUnderstandingResult,
    describe_authoritative_flow,
    is_authoritative_media_source_kind,
    uses_transcript_extraction_authoritative_path,
)
from modules.media_understanding.normalizer import AttributedTranscriptNormalizer
from modules.media_understanding.providers import (
    MediaUnderstandingProvider,
    MediaUnderstandingProviderBinding,
    MediaUnderstandingProviderSelectionConfig,
    bind_inline_media_understanding_provider,
    resolve_media_understanding_provider,
)


class MediaUnderstandingPipeline:
    """Bridge media understanding providers to downstream SubtitleLine consumers."""

    def __init__(
        self,
        provider: MediaUnderstandingProvider,
        normalizer: AttributedTranscriptNormalizer | None = None,
        provider_binding: MediaUnderstandingProviderBinding | None = None,
    ) -> None:
        self.provider = provider
        self.normalizer = normalizer or AttributedTranscriptNormalizer()
        self.provider_binding = provider_binding or bind_inline_media_understanding_provider(provider)

    @classmethod
    def from_selection(
        cls,
        selection: MediaUnderstandingProviderSelectionConfig,
        normalizer: AttributedTranscriptNormalizer | None = None,
    ) -> "MediaUnderstandingPipeline":
        binding = resolve_media_understanding_provider(selection)
        return cls(binding.provider, normalizer=normalizer, provider_binding=binding)

    def get_provider_audit(self) -> dict[str, object]:
        return {
            "provider_name": self.provider_binding.provider_name,
            "provider_mode": self.provider_binding.mode,
            "extraction_provider_name": self.provider_binding.extraction_provider_name,
            "extraction_provider_mode": self.provider_binding.extraction_provider_mode,
            "extraction_version_context": dict(self.provider_binding.extraction_version_context),
            "fallback_applied": self.provider_binding.fallback_applied,
            "fallback_reason": self.provider_binding.fallback_reason,
            "fallback_stage": self.provider_binding.fallback_stage,
            "version_context": dict(self.provider_binding.version_context),
        }

    def run(self, source: MediaSource) -> MediaUnderstandingResult:
        attributed_lines = self.provider.load_attributed_transcript(source)
        normalized_attributed_lines = self.normalizer.normalize(attributed_lines)
        subtitle_lines = self.normalizer.to_subtitle_lines(normalized_attributed_lines)
        authoritative_input_used = is_authoritative_media_source_kind(source.kind)
        return MediaUnderstandingResult(
            source=source,
            attributed_lines=normalized_attributed_lines,
            subtitle_lines=subtitle_lines,
            execution_mode="provider_run",
            authoritative_input_used=authoritative_input_used,
            authoritative_path_kind=source.kind.value if authoritative_input_used else None,
            authoritative_flow=describe_authoritative_flow(source.kind),
            transcript_extraction_used=uses_transcript_extraction_authoritative_path(source.kind),
            attributed_transcript_normalized=True,
            subtitle_line_bridge_applied=True,
        )

    def passthrough_subtitle_lines(self, subtitle_lines: list[SubtitleLine]) -> MediaUnderstandingResult:
        attributed_lines = self.normalizer.from_subtitle_lines(subtitle_lines)
        return MediaUnderstandingResult(
            source=None,
            attributed_lines=attributed_lines,
            subtitle_lines=list(subtitle_lines),
            execution_mode="passthrough",
            authoritative_input_used=False,
            authoritative_path_kind=None,
            authoritative_flow=None,
            transcript_extraction_used=False,
            attributed_transcript_normalized=False,
            subtitle_line_bridge_applied=False,
        )
