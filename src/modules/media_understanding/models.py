from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.models import SubtitleLine


class MediaSourceKind(str, Enum):
    """Supported upstream entry points for media understanding."""

    YOUTUBE_URL = "youtube_url"
    LOCAL_VIDEO = "local_video"
    LOCAL_AUDIO = "local_audio"
    LOCAL_SRT = "local_srt"
    TRANSCRIPT = "transcript"
    ATTRIBUTED_TRANSCRIPT = "attributed_transcript"


AUTHORITATIVE_MEDIA_SOURCE_KINDS = frozenset(
    {
        MediaSourceKind.TRANSCRIPT,
        MediaSourceKind.ATTRIBUTED_TRANSCRIPT,
        MediaSourceKind.LOCAL_SRT,
        MediaSourceKind.LOCAL_AUDIO,
        MediaSourceKind.LOCAL_VIDEO,
    }
)

REAL_AUTHORITATIVE_MEDIA_SOURCE_KINDS = frozenset(
    {
        MediaSourceKind.TRANSCRIPT,
        MediaSourceKind.ATTRIBUTED_TRANSCRIPT,
        MediaSourceKind.LOCAL_SRT,
        MediaSourceKind.LOCAL_AUDIO,
    }
)

SKELETON_AUTHORITATIVE_MEDIA_SOURCE_KINDS = frozenset({MediaSourceKind.LOCAL_VIDEO})

TRANSCRIPT_EXTRACTION_AUTHORITATIVE_SOURCE_KINDS = frozenset(
    {
        MediaSourceKind.LOCAL_AUDIO,
        MediaSourceKind.LOCAL_VIDEO,
    }
)


@dataclass(slots=True)
class TranscriptLine:
    """Plain transcript input before speaker attribution is attached."""

    index: int
    start_ms: int
    end_ms: int
    source_text: str


@dataclass(slots=True)
class TranscriptExtractionRequest:
    """Minimal transcript extraction input contract for local media providers."""

    source_kind: MediaSourceKind
    source_path: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TranscriptExtractionResult:
    """Minimal transcript extraction output contract before attribution."""

    request: TranscriptExtractionRequest
    transcript_lines: list[TranscriptLine]
    provider_name: str
    provider_mode: str
    version_context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AttributedTranscriptLine:
    """Preferred output contract of the media understanding stage."""

    index: int
    start_ms: int
    end_ms: int
    speaker_id: str
    speaker_name: str | None
    source_text: str


@dataclass(slots=True)
class MediaSource:
    """Normalized upstream source descriptor for media understanding providers.

    Current authoritative local-file inputs are intentionally minimal:
    - `local_srt`: `locator` is the source file path
    - `local_video`: `locator` is the source file path
    - `local_audio`: `locator` is the source file path
    - `metadata` stays optional and lightweight
    """

    kind: MediaSourceKind
    locator: str | None = None
    transcript_lines: list[TranscriptLine] = field(default_factory=list)
    attributed_lines: list[AttributedTranscriptLine] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        return self.locator or self.kind.value

    def source_path(self) -> str | None:
        if self.kind in {MediaSourceKind.LOCAL_SRT, MediaSourceKind.LOCAL_VIDEO, MediaSourceKind.LOCAL_AUDIO}:
            return self.locator
        return None

    def is_authoritative_input(self) -> bool:
        return is_authoritative_media_source_kind(self.kind)


@dataclass(slots=True)
class MediaUnderstandingResult:
    """Structured stage result bridging attributed transcript to SubtitleLine."""

    source: MediaSource | None
    attributed_lines: list[AttributedTranscriptLine]
    subtitle_lines: list[SubtitleLine]
    execution_mode: str
    authoritative_input_used: bool = False
    authoritative_path_kind: str | None = None
    authoritative_flow: str | None = None
    transcript_extraction_used: bool = False
    attributed_transcript_normalized: bool = False
    subtitle_line_bridge_applied: bool = False


def is_authoritative_media_source_kind(kind: MediaSourceKind | None) -> bool:
    return kind in AUTHORITATIVE_MEDIA_SOURCE_KINDS


def is_real_authoritative_media_source_kind(kind: MediaSourceKind | None) -> bool:
    return kind in REAL_AUTHORITATIVE_MEDIA_SOURCE_KINDS


def is_skeleton_authoritative_media_source_kind(kind: MediaSourceKind | None) -> bool:
    return kind in SKELETON_AUTHORITATIVE_MEDIA_SOURCE_KINDS


def uses_transcript_extraction_authoritative_path(kind: MediaSourceKind | None) -> bool:
    return kind in TRANSCRIPT_EXTRACTION_AUTHORITATIVE_SOURCE_KINDS


def describe_authoritative_flow(kind: MediaSourceKind | None) -> str | None:
    if kind == MediaSourceKind.TRANSCRIPT:
        return "transcript -> attributed_transcript -> subtitle_line_bridge"
    if kind == MediaSourceKind.ATTRIBUTED_TRANSCRIPT:
        return "attributed_transcript -> subtitle_line_bridge"
    if kind == MediaSourceKind.LOCAL_SRT:
        return "local_srt -> attributed_transcript -> subtitle_line_bridge"
    if kind == MediaSourceKind.LOCAL_AUDIO:
        return "local_audio -> transcript_extraction -> attributed_transcript -> subtitle_line_bridge"
    if kind == MediaSourceKind.LOCAL_VIDEO:
        return "local_video -> transcript_extraction -> attributed_transcript -> subtitle_line_bridge"
    return None
