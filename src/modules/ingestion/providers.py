from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from core.exceptions import IngestionError
from core.models import SubtitleLine
from modules.ingestion.models import SubtitleSeed
from modules.ingestion.normalizer import SubtitleNormalizer
from modules.ingestion.srt_loader import SRTSubtitleLoader


@runtime_checkable
class SubtitleSourceProvider(Protocol):
    """Replaceable subtitle ingestion boundary."""

    def load_subtitles(self) -> list[SubtitleLine]:
        """Load subtitles and return normalized SubtitleLine items."""


class MemorySubtitleProvider:
    def __init__(
        self,
        seeds: list[SubtitleSeed],
        normalizer: SubtitleNormalizer | None = None,
    ) -> None:
        self.seeds = seeds
        self.normalizer = normalizer or SubtitleNormalizer()

    def load_subtitles(self) -> list[SubtitleLine]:
        return self.normalizer.normalize(self.seeds)


@dataclass(slots=True)
class SRTFileSubtitleProvider:
    srt_path: str
    default_speaker_id: str = "speaker_default"
    default_speaker_name: str | None = None
    loader: SRTSubtitleLoader | None = None

    def load_subtitles(self) -> list[SubtitleLine]:
        loader = self.loader or SRTSubtitleLoader()
        return loader.load(
            srt_path=self.srt_path,
            default_speaker_id=self.default_speaker_id,
            default_speaker_name=self.default_speaker_name,
        )


@dataclass(slots=True)
class YouTubeSubtitleProviderSkeleton:
    video_url: str
    preferred_language: str = "en"

    def load_subtitles(self) -> list[SubtitleLine]:
        raise IngestionError(
            f"YouTube subtitle provider skeleton is not connected in Sprint 2B: {self.video_url}"
        )
