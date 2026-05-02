"""Canonical subtitle cue dataclass for subtitle-generation-v2.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §5.2
"""

from dataclasses import dataclass


@dataclass(slots=True)
class SubtitleCue:
    """Single subtitle unit (cue) for a spoken semantic block.

    All SRT / draft caption / Jianying draft outputs consume this canonical
    cue representation. Text is normalized at creation.
    """

    cue_id: str
    block_id: str
    speaker_id: str
    speaker_name: str | None
    text: str
    en_text: str
    start_ms: int
    end_ms: int
    source: str  # semantic_block_v2
    needs_review: bool = False
    review_reason: str | None = None

    def __post_init__(self) -> None:
        """Normalize text fields by stripping whitespace."""
        self.text = self.text.strip()
        self.en_text = self.en_text.strip()
