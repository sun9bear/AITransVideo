from dataclasses import dataclass, field
from typing import Any

from core.exceptions import IngestionError
from modules.media_understanding.models import (
    AUTHORITATIVE_MEDIA_SOURCE_KINDS,
    AttributedTranscriptLine,
    MediaSource,
    MediaSourceKind,
    TranscriptLine,
)


@dataclass(slots=True)
class AuthoritativeIntakeRequest:
    """Thin authoritative intake contract before workflow/media-understanding routing."""

    kind: MediaSourceKind
    locator: str | None = None
    transcript_lines: list[TranscriptLine] = field(default_factory=list)
    attributed_lines: list[AttributedTranscriptLine] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class AuthoritativeIntakeBuilder:
    """Normalize authoritative intake requests into MediaSource without changing stage order."""

    def build(self, request: AuthoritativeIntakeRequest) -> MediaSource:
        normalized_request = self.normalize(request)
        return MediaSource(
            kind=normalized_request.kind,
            locator=normalized_request.locator,
            transcript_lines=list(normalized_request.transcript_lines),
            attributed_lines=list(normalized_request.attributed_lines),
            metadata=dict(normalized_request.metadata),
        )

    def normalize(self, request: AuthoritativeIntakeRequest) -> AuthoritativeIntakeRequest:
        if request.kind not in AUTHORITATIVE_MEDIA_SOURCE_KINDS:
            raise IngestionError(
                f"Authoritative intake only supports authoritative source kinds, got {request.kind.value}."
            )

        locator = request.locator.strip() if isinstance(request.locator, str) else None
        transcript_lines = list(request.transcript_lines)
        attributed_lines = list(request.attributed_lines)
        metadata = dict(request.metadata)

        if request.kind == MediaSourceKind.TRANSCRIPT:
            if not transcript_lines:
                raise IngestionError("Authoritative transcript intake requires transcript_lines.")
            if locator is not None or attributed_lines:
                raise IngestionError("Transcript intake accepts transcript_lines only.")
        elif request.kind == MediaSourceKind.ATTRIBUTED_TRANSCRIPT:
            if not attributed_lines:
                raise IngestionError("Authoritative attributed transcript intake requires attributed_lines.")
            if locator is not None or transcript_lines:
                raise IngestionError("Attributed transcript intake accepts attributed_lines only.")
        else:
            if not locator:
                raise IngestionError(f"Authoritative {request.kind.value} intake requires locator.")
            if transcript_lines or attributed_lines:
                raise IngestionError(f"Authoritative {request.kind.value} intake accepts locator only.")

        return AuthoritativeIntakeRequest(
            kind=request.kind,
            locator=locator,
            transcript_lines=transcript_lines,
            attributed_lines=attributed_lines,
            metadata=metadata,
        )
