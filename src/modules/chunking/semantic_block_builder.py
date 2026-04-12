from dataclasses import dataclass

from core.enums import BlockStatus
from core.models import SemanticBlock, SubtitleLine


TERMINAL_PUNCTUATION = ("。", "！", "？")


@dataclass(slots=True)
class SemanticBlockBuilderConfig:
    """Sprint 1 test-oriented defaults, not final production tuning."""

    max_gap_ms: int = 1_200
    hard_break_gap_ms: int = 2_000
    max_block_duration_ms: int = 8_000
    max_chars_per_block: int = 80
    sentence_break_gap_ms: int = 500


class SemanticBlockBuilder:
    def __init__(self, config: SemanticBlockBuilderConfig | None = None) -> None:
        self.config = config or SemanticBlockBuilderConfig()

    def build(self, lines: list[SubtitleLine]) -> list[SemanticBlock]:
        if not lines:
            return []

        ordered_lines = sorted(lines, key=lambda item: (item.start_ms, item.index))
        blocks: list[SemanticBlock] = []
        current_group: list[SubtitleLine] = [ordered_lines[0]]

        for line in ordered_lines[1:]:
            if self._should_split(current_group, line):
                blocks.append(self._make_block(current_group, len(blocks) + 1))
                current_group = [line]
                continue
            current_group.append(line)

        blocks.append(self._make_block(current_group, len(blocks) + 1))
        return blocks

    def _should_split(self, current_group: list[SubtitleLine], next_line: SubtitleLine) -> bool:
        previous_line = current_group[-1]
        previous_text = previous_line.cn_text
        next_text = next_line.cn_text
        if next_line.speaker_id != previous_line.speaker_id:
            return True

        gap_ms = max(0, next_line.start_ms - previous_line.end_ms)
        if gap_ms >= self.config.hard_break_gap_ms:
            return True

        if previous_text.rstrip().endswith(TERMINAL_PUNCTUATION) and gap_ms > self.config.sentence_break_gap_ms:
            return True

        if gap_ms > self.config.max_gap_ms:
            return True

        merged_duration_ms = next_line.end_ms - current_group[0].start_ms
        if merged_duration_ms > self.config.max_block_duration_ms:
            return True

        merged_chars = sum(len(item.cn_text.strip()) for item in current_group) + len(next_text.strip())
        if merged_chars > self.config.max_chars_per_block:
            return True

        return False

    def _make_block(self, group: list[SubtitleLine], block_number: int) -> SemanticBlock:
        first_line = group[0]
        last_line = group[-1]
        merged_cn_text = "".join(
            line.cn_text.strip()
            for line in group
            if line.cn_text.strip()
        )

        return SemanticBlock(
            block_id=f"block_{block_number:04d}",
            speaker_id=first_line.speaker_id,
            speaker_name=first_line.speaker_name,
            original_srt_indices=[line.index for line in group],
            first_start_ms=first_line.start_ms,
            last_end_ms=last_line.end_ms,
            target_duration_ms=max(0, last_line.end_ms - first_line.start_ms),
            merged_cn_text=merged_cn_text,
            cn_line_texts=[line.cn_text for line in group],
            status=BlockStatus.PENDING.value,
        )
