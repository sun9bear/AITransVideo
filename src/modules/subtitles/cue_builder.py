"""SubtitleCueBuilder (T5) — orchestrates segmenter + cue_timing to produce
a list of SubtitleCue objects for a single semantic block.

Pipeline:
    1. segment_text(cn_text)            → list[SegmentSpan]
    2. assign_timing(spans, ...)        → list[TimedSpan]
    3. _split_en_proportionally(...)    → list[str]  (simple word-count split)
    4. Construct one SubtitleCue per TimedSpan.

English-text split is intentionally simple (Phase 1a): equal word-count
proportional, extras to front. Phase 1b can refine with semantic alignment.

2026-05-04 Phase C addition: ``build_cues_with_char_times`` is the
whisper-aligned twin of ``build_cues_for_block``. Same segment_text +
en split + cue construction; only the per-span timing changes (looked
up from a per-cn-char timestamp series produced by whisper + DTW).
The cue_pipeline integration in C3 calls this only when whisper ran
successfully and DTW produced a non-empty char_times. ANY anomaly →
returns ``[]`` so caller falls back to the proportional path.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §5.1, §5.4, §6, §10 Phase 1a
      docs/plans/2026-05-04-subtitle-audio-sync-plan.md Phase C Task C3
"""

from __future__ import annotations

import logging

from modules.subtitles.cue_models import SubtitleCue
from modules.subtitles.semantic_segmenter import segment_text
from modules.subtitles.cue_timing import assign_timing

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Phase C C3: whisper-aligned variant — same segmentation, swap in timing.
# ---------------------------------------------------------------------------

# Source tag used to distinguish whisper-aligned cues from the proportional
# path in subtitle_quality_report and downstream consumers.
_WHISPER_ALIGNED_SOURCE = "semantic_block_v2_whisper_aligned"

# Tolerance for char_times overshooting the block's slot duration.
# DTW's trailing-char interpolation adds ~80ms when the last cn char
# has no whisper anchor (common: trailing punctuation that ASR drops);
# whisper's last word also tends to end a few hundred ms before the
# WAV's actual end (silence padding). Net overshoot is typically
# 0-100ms. Anything above this threshold indicates corrupted upstream
# timing — fall back to proportional rather than silently clamping
# (which would mask the bug).
_SLOT_OVERSHOOT_TOLERANCE_MS = 100


def build_cues_with_char_times(
    *,
    block_id: str,
    speaker_id: str,
    speaker_name: str | None,
    cn_text: str,
    en_text: str,
    block_start_ms: int,
    block_end_ms: int,
    char_times: list[dict],
    min_display_ms: int = 500,  # noqa: ARG001  (reserved for future use; kept for API symmetry)
) -> list[SubtitleCue]:
    """Build cues using per-cn-char whisper timestamps for boundary placement.

    Drop-in twin of ``build_cues_for_block`` that takes ``char_times``
    (one entry per cn_text character, output of
    ``whisper_align.align_chars_to_words``) instead of relying on the
    proportional ``assign_timing`` placement. Reuses the SAME
    ``segment_text`` segmenter so cue boundaries / needs_review /
    review_reason / English-text split match the proportional path
    exactly — the ONLY difference is per-span start/end ms.

    Time semantics (CodeX guardrail #3): ``char_times`` is WAV-LOCAL
    (zero-relative to the block's audio). Final cue times are
    ``block_start_ms + local_time``, clamped to
    ``[block_start_ms, block_end_ms]``.

    Returns ``[]`` (never raises) on any anomaly so the cue pipeline
    can fall back to the proportional path:
      - char_times length != len(cn_text)
      - char_times non-monotonic in start_ms or end_ms
      - block_end_ms <= block_start_ms (degenerate slot)
      - empty cn_text or empty char_times

    Plan: docs/plans/2026-05-04-subtitle-audio-sync-plan.md Phase C Task C3.
    """
    # --- Anomaly gates (return [] not raise) ---
    if not cn_text or not char_times:
        return []
    if len(char_times) != len(cn_text):
        logger.debug(
            "build_cues_with_char_times: char_times length %d != cn_text "
            "length %d for block %s; falling back",
            len(char_times), len(cn_text), block_id,
        )
        return []
    if block_end_ms <= block_start_ms:
        logger.debug(
            "build_cues_with_char_times: degenerate slot [%d, %d] for block %s",
            block_start_ms, block_end_ms, block_id,
        )
        return []
    # Monotonic check on input char_times
    for i in range(1, len(char_times)):
        try:
            prev_start = int(char_times[i - 1].get("start_ms", 0))
            cur_start = int(char_times[i].get("start_ms", 0))
            cur_end = int(char_times[i].get("end_ms", 0))
            if cur_start < prev_start or cur_end < cur_start:
                logger.debug(
                    "build_cues_with_char_times: non-monotonic char_times at "
                    "index %d (block %s); falling back", i, block_id,
                )
                return []
        except (TypeError, ValueError):
            return []

    # --- Step 1: segment (same as proportional path) ---
    spans = segment_text(cn_text)
    if not spans:
        return []

    # Verify the segmenter invariant: concat of span texts == cn_text. If
    # someone changes the segmenter to mutate text, our char-index math
    # breaks — fall back rather than mis-time cues.
    rejoined = "".join(s.text for s in spans)
    if rejoined != cn_text:
        logger.debug(
            "build_cues_with_char_times: segment_text invariant broken "
            "(rejoined != cn_text) for block %s; falling back", block_id,
        )
        return []

    n = len(spans)

    # --- Step 2: per-span timing from char_times ---
    # For span i covering cn_text chars [start_char, end_char), the cue's
    # local time is char_times[start_char].start_ms .. char_times[end_char-1].end_ms.
    timed_spans: list[tuple] = []  # list of (span, start_ms, end_ms)
    char_cursor = 0
    slot_duration = block_end_ms - block_start_ms

    def _to_global(local_ms: int) -> int:
        """WAV-local → global timeline + clamp to slot."""
        gl = block_start_ms + max(0, int(local_ms))
        if gl < block_start_ms:
            gl = block_start_ms
        if gl > block_end_ms:
            gl = block_end_ms
        return gl

    for span in spans:
        span_len = len(span.text)
        if span_len == 0:
            # Pathological — should never happen given segment_text contract.
            return []
        start_idx = char_cursor
        end_idx = char_cursor + span_len
        char_cursor = end_idx
        if end_idx > len(char_times):
            return []
        try:
            local_start = int(char_times[start_idx]["start_ms"])
            local_end = int(char_times[end_idx - 1]["end_ms"])
        except (KeyError, TypeError, ValueError):
            return []
        # Sanity: local times must fit within the WAV slot duration.
        # Small overshoots (typically ≤100ms) come from DTW's trailing
        # char interpolation when the last cn char has no whisper anchor;
        # they're handled by the _to_global clamp below. Larger overshoots
        # indicate corrupted upstream timing — fall back. (2026-05-05:
        # threshold raised from +1ms to +100ms after a reshape-task rerun
        # showed legitimate ~17ms overshoots being rejected.)
        if local_start < 0 or local_end > slot_duration + _SLOT_OVERSHOOT_TOLERANCE_MS:
            return []
        if local_end < local_start:
            return []
        global_start = _to_global(local_start)
        global_end = _to_global(local_end)
        # Min-duration safety: empty cue (start == end) is invalid; bump end
        # by 1ms (validator will still flag short_display_duration as review).
        if global_end <= global_start:
            global_end = min(global_start + 1, block_end_ms)
        timed_spans.append((span, global_start, global_end))

    # Final monotonicity check on output cue boundaries — if something
    # squeezed two consecutive cues to the same instant, fall back.
    for i in range(1, len(timed_spans)):
        prev_end = timed_spans[i - 1][2]
        cur_start = timed_spans[i][1]
        if cur_start < prev_end:
            # Touch-up: snap to prev_end (clamped).
            new_start = min(prev_end, block_end_ms)
            cur_end = max(timed_spans[i][2], new_start + 1)
            cur_end = min(cur_end, block_end_ms)
            timed_spans[i] = (timed_spans[i][0], new_start, cur_end)

    # --- Step 3: split English text proportionally (same as proportional path) ---
    en_parts = _split_en_proportionally(en_text, n)

    # --- Step 4: build cues ---
    digit_width = 3 if n > 99 else 2
    fmt = f"{{:0{digit_width}d}}"

    cues: list[SubtitleCue] = []
    for ix, ((span, start_ms, end_ms), en_part) in enumerate(
        zip(timed_spans, en_parts), start=1,
    ):
        cue_id = f"{block_id}_cue_{fmt.format(ix)}"
        cues.append(
            SubtitleCue(
                cue_id=cue_id,
                block_id=block_id,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                text=span.text,
                en_text=en_part,
                start_ms=start_ms,
                end_ms=end_ms,
                source=_WHISPER_ALIGNED_SOURCE,
                needs_review=span.needs_review,
                review_reason=span.review_reason,
            )
        )
    return cues
