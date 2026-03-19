from pathlib import Path
import re

from core.exceptions import IngestionError
from core.models import SubtitleLine
from modules.ingestion.models import SubtitleSeed
from modules.ingestion.normalizer import SubtitleNormalizer


TIMECODE_PATTERN = re.compile(
    r"^(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2},\d{3})$"
)


class SRTSubtitleLoader:
    """Load a local SRT file and normalize it into SubtitleLine items."""

    def __init__(self, normalizer: SubtitleNormalizer | None = None) -> None:
        self.normalizer = normalizer or SubtitleNormalizer()

    def load(
        self,
        srt_path: str,
        default_speaker_id: str = "speaker_default",
        default_speaker_name: str | None = None,
    ) -> list[SubtitleLine]:
        file_path = Path(srt_path)
        if not file_path.exists():
            raise IngestionError(f"SRT file not found: {srt_path}")

        content = file_path.read_text(encoding="utf-8-sig")
        seeds = self.parse_content(
            content=content,
            default_speaker_id=default_speaker_id,
            default_speaker_name=default_speaker_name,
        )
        return self.normalizer.normalize(seeds)

    def parse_content(
        self,
        content: str,
        default_speaker_id: str = "speaker_default",
        default_speaker_name: str | None = None,
    ) -> list[SubtitleSeed]:
        if not default_speaker_id.strip():
            raise IngestionError("default_speaker_id must be provided for SRT loading.")

        seeds: list[SubtitleSeed] = []
        blocks = re.split(r"\r?\n\r?\n+", content.strip())

        for raw_block in blocks:
            lines = [line.strip() for line in raw_block.splitlines() if line.strip()]
            if not lines:
                continue
            if len(lines) < 3:
                raise IngestionError(f"Invalid SRT block: {raw_block}")
            if not lines[0].isdigit():
                raise IngestionError(f"Invalid SRT cue index: {lines[0]}")

            match = TIMECODE_PATTERN.match(lines[1])
            if match is None:
                raise IngestionError(f"Invalid SRT timecode line: {lines[1]}")

            seeds.append(
                SubtitleSeed(
                    index=int(lines[0]),
                    start_ms=self._timecode_to_ms(match.group("start")),
                    end_ms=self._timecode_to_ms(match.group("end")),
                    en_text=" ".join(lines[2:]).strip(),
                    speaker_id=default_speaker_id.strip(),
                    speaker_name=default_speaker_name,
                    cn_text="",
                )
            )

        return seeds

    def _timecode_to_ms(self, timecode: str) -> int:
        hours_text, minutes_text, seconds_text, millis_text = timecode.replace(",", ":").split(":")
        return (
            int(hours_text) * 3_600_000
            + int(minutes_text) * 60_000
            + int(seconds_text) * 1_000
            + int(millis_text)
        )
