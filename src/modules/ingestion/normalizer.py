from core.exceptions import IngestionError
from core.models import SubtitleLine
from modules.ingestion.models import SubtitleSeed


class SubtitleNormalizer:
    """Normalize in-memory subtitle inputs into SubtitleLine records."""

    def normalize(self, seeds: list[SubtitleSeed]) -> list[SubtitleLine]:
        normalized_lines: list[SubtitleLine] = []
        used_indices: set[int] = set()

        for position, seed in enumerate(seeds, start=1):
            speaker_id = seed.speaker_id.strip()
            if not speaker_id:
                raise IngestionError("speaker_id is required and cannot be derived from speaker_name.")
            if seed.end_ms <= seed.start_ms:
                raise IngestionError("end_ms must be greater than start_ms for every subtitle line.")

            index = seed.index if seed.index is not None else position
            if index in used_indices:
                raise IngestionError(f"Duplicate subtitle index detected: {index}")
            used_indices.add(index)

            normalized_lines.append(
                SubtitleLine(
                    index=index,
                    start_ms=seed.start_ms,
                    end_ms=seed.end_ms,
                    speaker_id=speaker_id,
                    speaker_name=seed.speaker_name,
                    en_text=seed.en_text.strip(),
                    cn_text=seed.cn_text.strip(),
                )
            )

        return normalized_lines
