from dataclasses import dataclass

from core.exceptions import DraftError
from core.models import SemanticBlock, SubtitleLine


@dataclass(slots=True)
class CaptionRetimingConfig:
    min_caption_duration_ms: int = 400

    def __post_init__(self) -> None:
        if self.min_caption_duration_ms <= 0:
            raise ValueError("min_caption_duration_ms must be positive.")


@dataclass(slots=True)
class RetimedCaption:
    caption_id: str
    block_id: str
    source_srt_index: int
    speaker_id: str
    speaker_name: str | None
    text: str
    start_ms: int
    end_ms: int


class CaptionRetimer:
    """Retimes captions by linearly scaling original intra-block timing."""

    def __init__(self, config: CaptionRetimingConfig | None = None) -> None:
        self.config = config or CaptionRetimingConfig()

    def retime_block(
        self,
        block: SemanticBlock,
        source_lines: list[SubtitleLine],
    ) -> list[RetimedCaption]:
        ordered_lines = sorted(source_lines, key=lambda line: (line.start_ms, line.index))
        if not ordered_lines:
            raise DraftError(f"Cannot retime block without source lines: {block.block_id}")

        caption_texts = self._resolve_caption_texts(block, ordered_lines)
        if len(caption_texts) != len(ordered_lines):
            raise DraftError(
                f"Caption text count does not match source line count for {block.block_id}."
            )

        aligned_duration_ms = block.actual_audio_duration_ms or block.target_duration_ms
        if aligned_duration_ms <= 0:
            raise DraftError(f"Aligned duration must be positive for {block.block_id}.")

        block_start_ms = block.first_start_ms
        block_end_ms = block_start_ms + aligned_duration_ms
        source_span_ms = max(1, block.last_end_ms - block.first_start_ms)
        effective_min_duration_ms = min(
            self.config.min_caption_duration_ms,
            max(1, aligned_duration_ms // len(ordered_lines)),
        )

        provisional_ranges = self._build_provisional_ranges(
            ordered_lines=ordered_lines,
            block_start_ms=block_start_ms,
            aligned_duration_ms=aligned_duration_ms,
            source_span_ms=source_span_ms,
        )

        retimed_captions: list[RetimedCaption] = []
        previous_end_ms = block_start_ms
        total_lines = len(ordered_lines)

        for position, (line, text, provisional_range) in enumerate(
            zip(ordered_lines, caption_texts, provisional_ranges),
            start=1,
        ):
            provisional_start_ms, provisional_end_ms = provisional_range
            remaining_lines = total_lines - position + 1
            latest_start_ms = block_end_ms - effective_min_duration_ms * remaining_lines
            start_ms = max(provisional_start_ms, previous_end_ms)
            start_ms = min(start_ms, latest_start_ms)

            latest_end_ms = block_end_ms - effective_min_duration_ms * (remaining_lines - 1)
            end_ms = max(provisional_end_ms, start_ms + effective_min_duration_ms)
            end_ms = min(end_ms, latest_end_ms)

            if position == total_lines:
                end_ms = block_end_ms

            if end_ms <= start_ms:
                end_ms = min(block_end_ms, start_ms + max(1, effective_min_duration_ms))
            if end_ms <= start_ms:
                raise DraftError(f"Retimed caption collapsed for {block.block_id} line {line.index}.")

            retimed_captions.append(
                RetimedCaption(
                    caption_id=f"{block.block_id}_caption_{position:02d}",
                    block_id=block.block_id,
                    source_srt_index=line.index,
                    speaker_id=line.speaker_id,
                    speaker_name=line.speaker_name,
                    text=text,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            )
            previous_end_ms = end_ms

        return retimed_captions

    def _build_provisional_ranges(
        self,
        ordered_lines: list[SubtitleLine],
        block_start_ms: int,
        aligned_duration_ms: int,
        source_span_ms: int,
    ) -> list[tuple[int, int]]:
        provisional_ranges: list[tuple[int, int]] = []
        first_line_start_ms = ordered_lines[0].start_ms

        for position, line in enumerate(ordered_lines, start=1):
            relative_start_ms = line.start_ms - first_line_start_ms
            relative_end_ms = line.end_ms - first_line_start_ms
            scaled_start_ms = block_start_ms + round(relative_start_ms / source_span_ms * aligned_duration_ms)
            scaled_end_ms = block_start_ms + round(relative_end_ms / source_span_ms * aligned_duration_ms)

            if position == 1:
                scaled_start_ms = block_start_ms
            if position == len(ordered_lines):
                scaled_end_ms = block_start_ms + aligned_duration_ms

            provisional_ranges.append((scaled_start_ms, scaled_end_ms))

        return provisional_ranges

    def _resolve_caption_texts(
        self,
        block: SemanticBlock,
        ordered_lines: list[SubtitleLine],
    ) -> list[str]:
        if block.final_cn_lines and len(block.final_cn_lines) == len(ordered_lines):
            return [text.strip() for text in block.final_cn_lines]
        if block.cn_line_texts and len(block.cn_line_texts) == len(ordered_lines):
            return [text.strip() for text in block.cn_line_texts]

        fallback_texts = [line.get_preferred_cn_text_for_caption().strip() for line in ordered_lines]
        if any(fallback_texts):
            return fallback_texts
        raise DraftError(f"No caption texts available for block {block.block_id}.")
