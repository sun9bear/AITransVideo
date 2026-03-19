from __future__ import annotations

from core.models import SemanticBlock, SubtitleLine
from modules.draft.caption_retiming import CaptionRetimer, RetimedCaption
from modules.draft.draft_writer import DraftWriteResult, DraftWriter


class DraftBackend:
    """Editor-output sub-capability that materializes the draft scaffold."""

    def __init__(
        self,
        caption_retimer: CaptionRetimer,
        draft_writer: DraftWriter,
    ) -> None:
        self.caption_retimer = caption_retimer
        self.draft_writer = draft_writer

    def load_existing_result(self, project_id: str) -> DraftWriteResult | None:
        return self.draft_writer.load_existing_result(project_id)

    def build_retimed_captions(
        self,
        translated_lines: list[SubtitleLine],
        aligned_blocks: list[SemanticBlock],
    ) -> list[RetimedCaption]:
        translated_line_map = {line.index: line for line in translated_lines}
        retimed_captions: list[RetimedCaption] = []

        for block in aligned_blocks:
            source_lines = [
                translated_line_map[index]
                for index in block.original_srt_indices
                if index in translated_line_map
            ]
            retimed_captions.extend(self.caption_retimer.retime_block(block, source_lines))

        return retimed_captions

    def write(
        self,
        *,
        project_id: str,
        translated_lines: list[SubtitleLine],
        aligned_blocks: list[SemanticBlock],
        stage_snapshot: dict[str, object] | None = None,
    ) -> DraftWriteResult:
        retimed_captions = self.build_retimed_captions(translated_lines, aligned_blocks)
        return self.draft_writer.write(
            project_id=project_id,
            blocks=aligned_blocks,
            captions=retimed_captions,
            stage_snapshot=stage_snapshot,
        )
