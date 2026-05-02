"""SubtitleCueBuilder (T5) — orchestrates segmenter + cue_timing to produce
a list of SubtitleCue objects for a single semantic block.

Pipeline:
    1. segment_text(cn_text)            → list[SegmentSpan]
    2. assign_timing(spans, ...)        → list[TimedSpan]
    3. _split_en_proportionally(...)    → list[str]  (simple word-count split)
    4. Construct one SubtitleCue per TimedSpan.

English-text split is intentionally simple (Phase 1a): equal word-count
proportional, extras to front. Phase 1b can refine with semantic alignment.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §5.1, §5.4, §6, §10 Phase 1a
"""

from __future__ import annotations

from modules.subtitles.cue_models import SubtitleCue
from modules.subtitles.semantic_segmenter import segment_text
from modules.subtitles.cue_timing import assign_timing


# ---------------------------------------------------------------------------
# Private helper: proportional English text split
# ---------------------------------------------------------------------------


def _split_en_proportionally(en_text: str, n: int) -> list[str]:
    """Split *en_text* into *n* roughly-equal word-count chunks.

    Rules:
    - n == 0 → [].
    - n == 1 → [en_text.strip()].
    - empty / whitespace-only en_text → [""] * n.
    - Otherwise: split by whitespace into words, chunk into n groups by
      word count.  Extras (word_count % n) go to the FRONT chunks.
      Example: 7 words / 3 → [3, 2, 2].
    """
    if n == 0:
        return []

    stripped = en_text.strip()

    if not stripped:
        return [""] * n

    if n == 1:
        return [stripped]

    words = stripped.split()
    total_words = len(words)

    # Compute chunk sizes: base = total // n, extras distributed to front.
    base, remainder = divmod(total_words, n)
    sizes = [base + (1 if i < remainder else 0) for i in range(n)]

    chunks: list[str] = []
    pos = 0
    for size in sizes:
        chunk_words = words[pos : pos + size]
        chunks.append(" ".join(chunk_words))
        pos += size

    return chunks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_cues_for_block(
    *,
    block_id: str,
    speaker_id: str,
    speaker_name: str | None,
    cn_text: str,
    en_text: str,
    block_start_ms: int,
    block_end_ms: int,
    source: str = "semantic_block_v2",
    min_display_ms: int = 500,
) -> list[SubtitleCue]:
    """Build a list of SubtitleCue objects for a single semantic block.

    Keyword-only signature keeps callers explicit and decouples builder from
    SemanticBlock shape — T9 OutputDispatcher resolves block fields before
    calling here.

    Pipeline:
    1. segment_text(cn_text) → list[SegmentSpan]
    2. assign_timing(spans, block_start_ms, block_end_ms, min_display_ms)
       → list[TimedSpan]
    3. Split en_text proportionally by word count to match span count.
    4. Construct one SubtitleCue per timed span.

    Edge cases:
    - Empty / whitespace-only cn_text → returns [].
    - block_end_ms <= block_start_ms → propagates ValueError from assign_timing.
    - len(spans) > total_duration → propagates ValueError from assign_timing.

    needs_review and review_reason are propagated exactly from each SegmentSpan.
    The builder does NOT inject "text_audio_may_need_review" — that is a
    caller-injected concern for T9 OutputDispatcher (Studio modify-flow).

    Args:
        block_id:        Unique identifier for the semantic block.
        speaker_id:      Speaker identifier.
        speaker_name:    Human-readable speaker label, or None.
        cn_text:         Merged Chinese text for the block (TTS source of truth).
        en_text:         English translation for the block.
        block_start_ms:  Inclusive start of the block's audio slot in ms.
        block_end_ms:    Exclusive end of the block's audio slot in ms.
        source:          Provenance tag, default "semantic_block_v2".
        min_display_ms:  Passed through to assign_timing, default 500.

    Returns:
        Ordered list of SubtitleCue objects; empty list if cn_text is empty.
    """
    # Step 1: segment
    spans = segment_text(cn_text)
    if not spans:
        return []

    n = len(spans)

    # Step 2: assign timing (may raise ValueError for bad timing params)
    timed_spans = assign_timing(spans, block_start_ms, block_end_ms, min_display_ms=min_display_ms)

    # Step 3: split English text proportionally
    en_parts = _split_en_proportionally(en_text, n)

    # Step 4: build cues
    # cue_id format: "{block_id}_cue_{ix:02d}" for ix <= 99, else "{ix:03d}"
    digit_width = 3 if n > 99 else 2
    fmt = f"{{:0{digit_width}d}}"

    cues: list[SubtitleCue] = []
    for ix, (ts, en_part) in enumerate(zip(timed_spans, en_parts), start=1):
        cue_id = f"{block_id}_cue_{fmt.format(ix)}"
        cues.append(
            SubtitleCue(
                cue_id=cue_id,
                block_id=block_id,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                text=ts.span.text,
                en_text=en_part,
                start_ms=ts.start_ms,
                end_ms=ts.end_ms,
                source=source,
                needs_review=ts.span.needs_review,
                review_reason=ts.span.review_reason,
            )
        )

    return cues
