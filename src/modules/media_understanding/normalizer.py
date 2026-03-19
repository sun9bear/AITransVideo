from core.exceptions import MediaUnderstandingOutputError
from core.models import SubtitleLine
from modules.media_understanding.models import AttributedTranscriptLine


class AttributedTranscriptNormalizer:
    """Normalize attributed transcript lines and adapt them into SubtitleLine items."""

    def normalize(self, lines: list[AttributedTranscriptLine]) -> list[AttributedTranscriptLine]:
        normalized_lines: list[AttributedTranscriptLine] = []
        used_indices: set[int] = set()

        for line in lines:
            speaker_id = line.speaker_id.strip()
            speaker_name = line.speaker_name.strip() if isinstance(line.speaker_name, str) else None
            source_text = line.source_text.strip()

            if not speaker_id:
                raise MediaUnderstandingOutputError("speaker_id is required for attributed transcript lines.")
            if not source_text:
                raise MediaUnderstandingOutputError("source_text is required for attributed transcript lines.")
            if line.end_ms <= line.start_ms:
                raise MediaUnderstandingOutputError(
                    "end_ms must be greater than start_ms for every attributed transcript line."
                )
            if line.index in used_indices:
                raise MediaUnderstandingOutputError(f"Duplicate attributed transcript index detected: {line.index}")
            used_indices.add(line.index)

            normalized_lines.append(
                AttributedTranscriptLine(
                    index=line.index,
                    start_ms=line.start_ms,
                    end_ms=line.end_ms,
                    speaker_id=speaker_id,
                    speaker_name=speaker_name or None,
                    source_text=source_text,
                )
            )

        return normalized_lines

    def to_subtitle_lines(self, lines: list[AttributedTranscriptLine]) -> list[SubtitleLine]:
        normalized_lines = self.normalize(lines)
        return [
            SubtitleLine(
                index=line.index,
                start_ms=line.start_ms,
                end_ms=line.end_ms,
                speaker_id=line.speaker_id,
                speaker_name=line.speaker_name,
                en_text=line.source_text,
                cn_text="",
            )
            for line in normalized_lines
        ]

    def from_subtitle_lines(self, lines: list[SubtitleLine]) -> list[AttributedTranscriptLine]:
        return self.normalize(
            [
                AttributedTranscriptLine(
                    index=line.index,
                    start_ms=line.start_ms,
                    end_ms=line.end_ms,
                    speaker_id=line.speaker_id,
                    speaker_name=line.speaker_name,
                    source_text=line.en_text,
                )
                for line in lines
            ]
        )
