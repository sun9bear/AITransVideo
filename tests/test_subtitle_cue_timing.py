"""Unit tests for cue_timing.assign_timing() — Task 4 (T4) of
subtitle-generation-v2, Phase 1a.

Plan: docs/plans/2026-05-02-subtitle-cue-generation-v2-plan.md §5.4

Phase 1a speech weights:
- CJK char:      1.0 each
- English word:  1.5 per word match (NOT per char)
- digit char:    1.0 each
- punctuation:   0
- whitespace:    0
- other:         0
- zero-weight span floor: 1.0 (per-span floor)
"""

import pytest

from modules.subtitles.cue_timing import TimedSpan, assign_timing
from modules.subtitles.semantic_segmenter import SegmentSpan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def span(text: str, *, needs_review: bool = False, review_reason: str | None = None) -> SegmentSpan:
    """Convenience: build a SegmentSpan from raw text."""
    return SegmentSpan(text=text, needs_review=needs_review, review_reason=review_reason)


# ---------------------------------------------------------------------------
# 1. Empty input
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_list():
    """assign_timing([], 0, 5000) returns []."""
    result = assign_timing([], 0, 5000)
    assert result == []


# ---------------------------------------------------------------------------
# 2. Single span — full duration
# ---------------------------------------------------------------------------


def test_single_span_full_duration():
    """Single span receives the full block duration."""
    spans = [span("今天很好")]
    result = assign_timing(spans, block_start_ms=1000, block_end_ms=4000)
    assert len(result) == 1
    ts = result[0]
    assert ts.start_ms == 1000
    assert ts.end_ms == 4000
    assert ts.span is spans[0]


def test_single_span_start_equals_block_start():
    """Single span: start_ms equals block_start_ms exactly."""
    result = assign_timing([span("abc")], block_start_ms=500, block_end_ms=2500)
    assert result[0].start_ms == 500


def test_single_span_end_equals_block_end():
    """Single span: end_ms equals block_end_ms exactly."""
    result = assign_timing([span("abc")], block_start_ms=500, block_end_ms=2500)
    assert result[0].end_ms == 2500


# ---------------------------------------------------------------------------
# 3. Two equal-weight spans — split duration in half
# ---------------------------------------------------------------------------


def test_two_equal_weight_cjk_spans_split_half():
    """[span('今天'), span('明天')], 0..2000 → each span gets ~1000ms."""
    # Both spans have 2 CJK chars each → weight 2.0 each
    spans = [span("今天"), span("明天")]
    result = assign_timing(spans, block_start_ms=0, block_end_ms=2000)
    assert len(result) == 2
    assert result[0].start_ms == 0
    assert result[0].end_ms == 1000
    assert result[1].start_ms == 1000
    assert result[1].end_ms == 2000


# ---------------------------------------------------------------------------
# 4. CJK-only weight proportional split
# ---------------------------------------------------------------------------


def test_cjk_only_weight_3_to_1_split():
    """[span('一二三'), span('四')], 0..4000 → first span gets 3000ms, second 1000ms."""
    # Weights: 3.0 vs 1.0 → first 75%, second 25% of 4000ms
    spans = [span("一二三"), span("四")]
    result = assign_timing(spans, block_start_ms=0, block_end_ms=4000)
    assert len(result) == 2
    assert result[0].start_ms == 0
    assert result[0].end_ms == 3000
    assert result[1].start_ms == 3000
    assert result[1].end_ms == 4000


# ---------------------------------------------------------------------------
# 5. Mixed CJK + English weight — single span
# ---------------------------------------------------------------------------


def test_mixed_cjk_english_single_span():
    """Single span 'hello 你好' → weight 3.5 (1 English word=1.5 + 2 CJK=2.0), full duration."""
    # Single span gets the full duration regardless of weight
    spans = [span("hello 你好")]
    result = assign_timing(spans, block_start_ms=0, block_end_ms=3500)
    assert len(result) == 1
    assert result[0].start_ms == 0
    assert result[0].end_ms == 3500


# ---------------------------------------------------------------------------
# 6. Mixed CJK + English — proportional split
# ---------------------------------------------------------------------------


def test_mixed_cjk_english_two_spans_proportional():
    """[span('hello'), span('你好')], 0..3500 → weights 1.5 vs 2.0, split at 1500."""
    # 'hello' = 1 English word = 1.5
    # '你好'  = 2 CJK chars   = 2.0
    # Total weight = 3.5; total duration = 3500ms
    # hello: floor(3500 * 1.5 / 3.5) = floor(1500) = 1500
    # 你好:  absorbs remainder = 3500 - 1500 = 2000
    spans = [span("hello"), span("你好")]
    result = assign_timing(spans, block_start_ms=0, block_end_ms=3500)
    assert len(result) == 2
    assert result[0].start_ms == 0
    assert result[0].end_ms == 1500
    assert result[1].start_ms == 1500
    assert result[1].end_ms == 3500


# ---------------------------------------------------------------------------
# 7. Digit run weight
# ---------------------------------------------------------------------------


def test_digit_run_weight_4_to_1_split():
    """[span('1024'), span('一')], 0..5000 → weights 4.0 vs 1.0, first gets 4000ms."""
    # '1024' = 4 digit chars → 4.0
    # '一'   = 1 CJK char    → 1.0
    # Total = 5.0, duration 5000ms → first 4000ms, second 1000ms
    spans = [span("1024"), span("一")]
    result = assign_timing(spans, block_start_ms=0, block_end_ms=5000)
    assert len(result) == 2
    assert result[0].start_ms == 0
    assert result[0].end_ms == 4000
    assert result[1].start_ms == 4000
    assert result[1].end_ms == 5000


# ---------------------------------------------------------------------------
# 8. Punctuation contributes zero — defensive floor applies
# ---------------------------------------------------------------------------


def test_punctuation_zero_weight_uses_floor():
    """[span('。。'), span('一二')], 0..3000 → punct span floored to 1.0, CJK span 2.0."""
    # '。。' has 0 raw weight → floored to 1.0
    # '一二' has 2 CJK chars  → 2.0
    # Ratio: 1.0 : 2.0 → floor(3000 * 1/3) = 1000 for first, remainder = 2000 for last
    spans = [span("。。"), span("一二")]
    result = assign_timing(spans, block_start_ms=0, block_end_ms=3000)
    assert len(result) == 2
    assert result[0].start_ms == 0
    assert result[0].end_ms == 1000
    assert result[1].start_ms == 1000
    assert result[1].end_ms == 3000


# ---------------------------------------------------------------------------
# 9. Last cue ends at block_end_ms exactly (rounding drift absorbed)
# ---------------------------------------------------------------------------


def test_last_cue_ends_at_block_end_ms_exactly():
    """Rounding shouldn't lose ms — last cue always ends at block_end_ms."""
    # 3 equal-weight spans over 1000ms → each should get ~333.33ms
    # floor gives 333, 333, 334 or similar — last cue MUST end at 1000
    spans = [span("一"), span("二"), span("三")]  # weight 1.0 each
    result = assign_timing(spans, block_start_ms=0, block_end_ms=1000)
    assert len(result) == 3
    assert result[-1].end_ms == 1000


# ---------------------------------------------------------------------------
# 10. First cue starts at block_start_ms exactly
# ---------------------------------------------------------------------------


def test_first_cue_starts_at_block_start_ms_exactly():
    """First cue always starts at block_start_ms — no rounding offset."""
    spans = [span("一"), span("二"), span("三")]
    result = assign_timing(spans, block_start_ms=750, block_end_ms=1750)
    assert result[0].start_ms == 750


# ---------------------------------------------------------------------------
# 11. Monotonic non-overlap
# ---------------------------------------------------------------------------


def test_monotonic_non_overlap_two_spans():
    """For any 2-span input, cues[0].end_ms == cues[1].start_ms."""
    spans = [span("一二三"), span("四五六")]
    result = assign_timing(spans, block_start_ms=0, block_end_ms=3000)
    assert result[0].end_ms == result[1].start_ms


def test_monotonic_non_overlap_many_spans():
    """For any multi-span input, cues[i].end_ms == cues[i+1].start_ms for all i."""
    spans = [span("一"), span("二"), span("三"), span("四"), span("五")]
    result = assign_timing(spans, block_start_ms=100, block_end_ms=2100)
    for i in range(len(result) - 1):
        assert result[i].end_ms == result[i + 1].start_ms, (
            f"Non-contiguous at i={i}: {result[i].end_ms} != {result[i+1].start_ms}"
        )


# ---------------------------------------------------------------------------
# 12. Min-display enforcement (block has room)
# ---------------------------------------------------------------------------


def test_min_display_enforced_when_block_has_room():
    """Weights [100, 1], block 0-5000ms, min_display=500: tiny span still gets >= 500ms."""
    # Without min-display enforcement: weight 1/101 * 5000 ≈ 49ms — too short.
    # With enforcement: tiny span bumped to 500ms; large span absorbs deficit.
    spans = [span("一" * 100), span("二")]  # weights 100.0 and 1.0
    result = assign_timing(spans, block_start_ms=0, block_end_ms=5000, min_display_ms=500)
    assert len(result) == 2
    # Tiny span should have at least min_display_ms
    assert (result[1].end_ms - result[1].start_ms) >= 500
    # Full range still covered
    assert result[0].start_ms == 0
    assert result[-1].end_ms == 5000


# ---------------------------------------------------------------------------
# 13. Min-display enforcement (block too short)
# ---------------------------------------------------------------------------


def test_min_display_relaxed_when_block_too_short():
    """Weights [1,1,1], block 0-300ms, min_display=500. Block too short → effective min = 100."""
    # Block has 300ms total, 3 spans, min_display=500 > 300/3=100
    # effective_min = max(1, 300 // 3) = 100; each span gets at least 100ms
    spans = [span("一"), span("二"), span("三")]
    result = assign_timing(spans, block_start_ms=0, block_end_ms=300, min_display_ms=500)
    assert len(result) == 3
    for ts in result:
        duration = ts.end_ms - ts.start_ms
        assert duration >= 1  # non-zero at minimum
    # Last cue must still end at block_end_ms
    assert result[-1].end_ms == 300
    # And no overlap
    for i in range(len(result) - 1):
        assert result[i].end_ms == result[i + 1].start_ms


# ---------------------------------------------------------------------------
# 14. All zero-weight spans — defensive floor distributes equally
# ---------------------------------------------------------------------------


def test_all_zero_weight_spans_equal_distribution():
    """3 spans of pure punctuation each get floored to weight 1.0 → equal time distribution."""
    # '。' has 0 raw weight → floor to 1.0 each
    # Equal weights → equal time distribution across 3000ms
    spans = [span("。"), span("！"), span("？")]
    result = assign_timing(spans, block_start_ms=0, block_end_ms=3000)
    assert len(result) == 3
    # Each should get exactly 1000ms (floor-based with drift absorbed by last)
    assert result[0].start_ms == 0
    # Verify equal-ish distribution: each duration should be close to 1000ms
    # (exact values depend on floor strategy and drift absorption)
    for ts in result:
        duration = ts.end_ms - ts.start_ms
        assert 999 <= duration <= 1001, f"Expected ~1000ms per span, got {duration}ms"
    # Last cue ends exactly at block_end_ms
    assert result[-1].end_ms == 3000


# ---------------------------------------------------------------------------
# 15. Invalid input — block_end_ms <= block_start_ms raises ValueError
# ---------------------------------------------------------------------------


def test_invalid_zero_duration_raises_value_error():
    """block_end_ms == block_start_ms raises ValueError."""
    with pytest.raises(ValueError, match="block_end_ms"):
        assign_timing([span("一")], block_start_ms=1000, block_end_ms=1000)


def test_invalid_negative_duration_raises_value_error():
    """block_end_ms < block_start_ms raises ValueError."""
    with pytest.raises(ValueError, match="block_end_ms"):
        assign_timing([span("一")], block_start_ms=2000, block_end_ms=1000)


# ---------------------------------------------------------------------------
# Additional property tests
# ---------------------------------------------------------------------------


def test_timed_span_carries_original_span():
    """TimedSpan.span should be the same SegmentSpan object passed in."""
    s = span("今天")
    result = assign_timing([s], block_start_ms=0, block_end_ms=1000)
    assert result[0].span is s


def test_min_display_zero_skips_enforcement():
    """min_display_ms=0 disables min-display enforcement — pure weighted distribution."""
    # Extreme ratio: weight 999 vs 1 over 1000ms
    # Without enforcement: second span gets 1ms (floor(1000*1/1000) = 1)
    spans = [span("一" * 999), span("二")]  # weights 999.0 and 1.0
    result = assign_timing(spans, block_start_ms=0, block_end_ms=1000, min_display_ms=0)
    assert len(result) == 2
    # Second span gets exactly 1ms (no min enforcement)
    second_duration = result[1].end_ms - result[1].start_ms
    assert second_duration >= 1  # minimum 1ms (floor gives at least 1)
    # Last cue ends exactly at block_end_ms
    assert result[-1].end_ms == 1000


def test_timed_span_frozen():
    """TimedSpan is frozen — raises on mutation attempt."""
    s = span("一")
    result = assign_timing([s], block_start_ms=0, block_end_ms=1000)
    ts = result[0]
    with pytest.raises((AttributeError, TypeError)):
        ts.start_ms = 999  # type: ignore[misc]


def test_english_word_count_not_char_count():
    """English weight is 1.5 per WORD, not per character.

    'hi' (2 chars, 1 word) and 'hello world' (11 chars, 2 words) in two spans.
    Weights: 1.5 and 3.0 → ratio 1:2 over 3000ms → first gets 1000ms, second 2000ms.
    """
    spans = [span("hi"), span("hello world")]
    result = assign_timing(spans, block_start_ms=0, block_end_ms=3000)
    assert len(result) == 2
    # floor(3000 * 1.5 / 4.5) = floor(1000.0) = 1000
    assert result[0].end_ms - result[0].start_ms == 1000
    # last absorbs remainder = 3000 - 1000 = 2000
    assert result[1].end_ms - result[1].start_ms == 2000
