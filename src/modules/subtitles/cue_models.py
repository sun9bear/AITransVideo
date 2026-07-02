"""Canonical subtitle cue dataclass and text-normalization helper for subtitle-generation-v2.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §5.2, §8
"""

import re
import unicodedata
from dataclasses import dataclass

# CJK punctuation that NFKC does NOT map to ASCII equivalents.
# Full-width forms like （）！？，；：are already handled by NFKC; only the
# distinct CJK block (U+3000–U+303F) and corner brackets need explicit mapping.
_CJK_PUNCT_TABLE = str.maketrans(
    {
        "。": ".",   # CJK ideographic full stop   U+3002
        "、": ",",   # CJK ideographic comma        U+3001
        "「": '"',   # CJK left corner bracket      U+300C
        "」": '"',   # CJK right corner bracket     U+300D
        "『": '"',   # white corner bracket left    U+300E
        "』": '"',   # white corner bracket right   U+300F
    }
)


def normalize(text: str) -> str:
    """Normalize per plan §8 contract — for text-equivalence comparison only.

    Steps applied in order:
    1. Unicode NFKC (handles full-width ASCII, compatibility forms).
    2. Remove characters whose Unicode category is Cf (zero-width/format chars,
       BOM, direction marks, etc.).
    3. Map remaining CJK punctuation (。、「」『』) to ASCII equivalents.
    4. Collapse consecutive whitespace to a single space and strip ends.

    The return value is only for equality comparison. Display text is unchanged.
    """
    if not text:
        return ""

    # Step 1 — NFKC: full-width ASCII, compatibility ligatures, etc.
    result = unicodedata.normalize("NFKC", text)

    # Step 2 — Remove Unicode Cf-category chars (zero-width, BOM, direction marks).
    result = "".join(ch for ch in result if unicodedata.category(ch) != "Cf")

    # Step 3 — Explicit CJK punctuation mapping (not covered by NFKC).
    result = result.translate(_CJK_PUNCT_TABLE)

    # Step 4 — Collapse whitespace and strip.
    result = re.sub(r"\s+", " ", result).strip()

    return result


@dataclass(slots=True)
class SubtitleCue:
    """Single subtitle unit (cue) for a spoken semantic block.

    All SRT / draft caption / Jianying draft outputs consume this canonical
    cue representation. Text is normalized at creation.

    Field-name caveat (legacy, from the en->zh-only era): ``text`` is always
    the dub (TARGET) language and ``en_text`` always the SOURCE — regardless
    of language pair. For a zh->en job ``en_text`` therefore carries the
    Chinese source. Serialized subtitle_cues.json for non-default pairs is
    stamped with ``cue_field_roles`` so JSON consumers don't have to know
    this convention.
    """

    cue_id: str
    block_id: str
    speaker_id: str
    speaker_name: str | None
    text: str
    en_text: str
    start_ms: int
    end_ms: int
    source: str  # e.g. "semantic_block_v2"
    needs_review: bool = False
    review_reason: str | None = None

    def __post_init__(self) -> None:
        """Normalize text fields by stripping whitespace."""
        self.text = self.text.strip()
        self.en_text = self.en_text.strip()
