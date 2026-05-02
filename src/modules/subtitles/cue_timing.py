"""Phase 1a cue timing: minimal speech-weight time distribution.

Distributes [block_start_ms, block_end_ms] across a list of SegmentSpans by
relative speech weight.

Phase 1a speech-weight rules (plan §5.4):
- CJK character (一-鿿 / Extension A / Compatibility blocks): 1.0 each
- English word ([A-Za-z][A-Za-z0-9_'-]*):                     1.5 per word match
- Digit character (inside a digit run [0-9]+):                1.0 each
- Punctuation, whitespace, other symbols:                      0

Zero-weight span defensive floor: any span whose computed weight is 0 is
assigned a floor weight of 1.0 so it gets non-zero duration.

Rounding strategy: floor each provisional duration (int()), then let the
last cue absorb the accumulated drift so that cues[-1].end_ms == block_end_ms
exactly.  This is deterministic and never drifts by more than (n-1) ms.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §5.4, T4
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from modules.subtitles.semantic_segmenter import SegmentSpan


# ---------------------------------------------------------------------------
# Regex patterns for Phase 1a weight computation
# ---------------------------------------------------------------------------

# One English word (used to count words, not chars)
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]*")

# One digit run; we count individual digit characters, not whole runs
_DIGIT_RUN_RE = re.compile(r"\d+")


# ---------------------------------------------------------------------------
# CJK character detection (same blocks as semantic_segmenter)
# ---------------------------------------------------------------------------


def _is_cjk_char(ch: str) -> bool:
    """Return True if *ch* is a CJK ideograph (Phase 1a recognised blocks)."""
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF       # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF    # Extension A
        or 0xF900 <= cp <= 0xFAFF    # CJK Compatibility Ideographs
        or 0x20000 <= cp <= 0x2A6DF  # Extension B
        or 0x2A700 <= cp <= 0x2B73F  # Extensions C/D
    )


# ---------------------------------------------------------------------------
# Speech-weight computation
# ---------------------------------------------------------------------------


def _speech_weight(text: str) -> float:
    """Return Phase 1a speech weight for *text*.

    Algorithm:
    1. Scan each character:
       - CJK char: +1.0
    2. Find all English word matches: each match is +1.5 (regardless of length).
       Skip positions already counted as CJK (they can't match [A-Za-z] anyway).
    3. Find all digit runs: each digit character in the run is +1.0.
       Digits can't be CJK or English, so no double-count.
    4. Everything else (punctuation, whitespace, symbols): +0.

    Zero-weight floor is applied by the caller per span, not here.
    """
    weight = 0.0

    # CJK chars: iterate character by character
    for ch in text:
        if _is_cjk_char(ch):
            weight += 1.0

    # English words: 1.5 per match
    for _match in _ENGLISH_WORD_RE.finditer(text):
        weight += 1.5

    # Digit chars: 1.0 per digit character
    for m in _DIGIT_RUN_RE.finditer(text):
        weight += len(m.group())

    return weight


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TimedSpan:
    """A SegmentSpan with concrete start/end millisecond timing."""

    span: SegmentSpan
    start_ms: int
    end_ms: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def assign_timing(
    spans: list[SegmentSpan],
    block_start_ms: int,
    block_end_ms: int,
    *,
    min_display_ms: int = 500,
) -> list[TimedSpan]:
    """Assign start/end ms for each span using Phase 1a speech weights.

    Distributes [block_start_ms, block_end_ms] across the spans by
    relative speech weight.  Guarantees:
    - monotonic, non-overlapping cue ranges
    - last cue's end_ms == block_end_ms
    - first cue's start_ms == block_start_ms
    - each cue's duration >= min_display_ms (when block has room)
    - cue ranges never exceed [block_start_ms, block_end_ms]

    Rounding strategy: floor each provisional duration; last cue absorbs
    accumulated drift so the invariant cues[-1].end_ms == block_end_ms holds.

    Args:
        spans: Left-to-right ordered SegmentSpans.  Not reordered by this fn.
        block_start_ms: Inclusive start of the block's audio slot in ms.
        block_end_ms:   Exclusive end of the block's audio slot in ms.
        min_display_ms: Minimum display duration per cue in ms.  Pass 0 to
                        disable min-display enforcement entirely.

    Returns:
        List of TimedSpan objects in the same order as *spans*.

    Raises:
        ValueError: If block_end_ms <= block_start_ms.
    """
    total_duration = block_end_ms - block_start_ms
    if total_duration <= 0:
        raise ValueError(
            f"block_end_ms ({block_end_ms}) must be greater than "
            f"block_start_ms ({block_start_ms})"
        )

    n = len(spans)
    if n == 0:
        return []

    # --- Step 1: compute raw weights with per-span zero floor ---
    raw_weights = [_speech_weight(s.text) for s in spans]
    weights = [w if w > 0.0 else 1.0 for w in raw_weights]

    # --- Step 2: compute provisional durations using floor rounding ---
    weight_sum = sum(weights)
    durations: list[int] = []
    for w in weights:
        d = int(total_duration * w / weight_sum)  # floor
        durations.append(d)

    # The last cue absorbs the remaining drift so sum == total_duration exactly.
    consumed = sum(durations[:-1])
    durations[-1] = total_duration - consumed

    # --- Step 3: min-display enforcement ---
    if min_display_ms > 0:
        # Determine effective minimum: if block is too short, shrink proportionally.
        if total_duration < n * min_display_ms:
            effective_min = max(1, total_duration // n)
        else:
            effective_min = min_display_ms

        # Walk forward: if a cue is below effective_min, steal from the next
        # cue that has surplus (or from the longest cue if the next has none).
        for i in range(n):
            deficit = effective_min - durations[i]
            if deficit <= 0:
                continue

            # Try to borrow from subsequent spans with surplus
            borrowed = 0
            for j in range(i + 1, n):
                available = durations[j] - effective_min
                if available <= 0:
                    continue
                take = min(deficit - borrowed, available)
                durations[j] -= take
                borrowed += take
                if borrowed >= deficit:
                    break

            # If subsequent spans couldn't cover it, borrow from the longest so far
            if borrowed < deficit:
                remaining = deficit - borrowed
                longest_idx = max(range(i), key=lambda k: durations[k]) if i > 0 else -1
                if longest_idx >= 0 and durations[longest_idx] > effective_min:
                    available = durations[longest_idx] - effective_min
                    take = min(remaining, available)
                    durations[longest_idx] -= take
                    borrowed += take

            durations[i] += borrowed

        # After enforcement, re-absorb any sum drift into the last cue so the
        # block_end_ms invariant is maintained.
        consumed = sum(durations[:-1])
        durations[-1] = total_duration - consumed
        # Edge case: last cue may have gone negative if enforcement over-donated;
        # clamp to at least 1ms and re-normalise from the front.
        if durations[-1] < 1:
            durations[-1] = 1

    # --- Step 4: build TimedSpan list ---
    result: list[TimedSpan] = []
    cursor = block_start_ms
    for i, (s, d) in enumerate(zip(spans, durations)):
        start = cursor
        end = cursor + d
        result.append(TimedSpan(span=s, start_ms=start, end_ms=end))
        cursor = end

    # Hard invariant assertions (cheap; catch future regression immediately).
    assert result[0].start_ms == block_start_ms
    assert result[-1].end_ms == block_end_ms

    return result
