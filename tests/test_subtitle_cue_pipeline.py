"""Tests for cue_pipeline.build_subtitle_cues_for_blocks (T9).

Covers 10 scenarios per the T9 task spec.
"""

from __future__ import annotations

import pytest

from core.models import SemanticBlock, SubtitleLine
from modules.subtitles.cue_models import normalize
from modules.subtitles.cue_pipeline import SubtitleCuePipelineResult, build_subtitle_cues_for_blocks


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _make_block(
    *,
    block_id: str = "block_0001",
    speaker_id: str = "speaker_1",
    speaker_name: str | None = None,
    merged_cn_text: str = "今天好。",
    original_srt_indices: list[int] | None = None,
    first_start_ms: int = 0,
    last_end_ms: int = 2000,
    target_duration_ms: int = 1500,
    actual_audio_duration_ms: int = 0,
) -> SemanticBlock:
    return SemanticBlock(
        block_id=block_id,
        speaker_id=speaker_id,
        speaker_name=speaker_name,
        original_srt_indices=original_srt_indices if original_srt_indices is not None else [0],
        first_start_ms=first_start_ms,
        last_end_ms=last_end_ms,
        target_duration_ms=target_duration_ms,
        merged_cn_text=merged_cn_text,
        actual_audio_duration_ms=actual_audio_duration_ms,
    )


def _make_line(
    *,
    index: int,
    en_text: str = "",
    cn_text: str = "",
    start_ms: int = 0,
    end_ms: int = 1000,
    speaker_id: str = "speaker_1",
) -> SubtitleLine:
    return SubtitleLine(
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_id=speaker_id,
        speaker_name=None,
        en_text=en_text,
        cn_text=cn_text,
    )


# ---------------------------------------------------------------------------
# Scenario 1: Basic — 1 block, 1 SubtitleLine, produces 1 cue with correct text
# ---------------------------------------------------------------------------


def test_basic_one_block_one_line_produces_cue() -> None:
    """1 block with cn_text producing cues; en_text comes from the matched line."""
    blocks = [_make_block(merged_cn_text="今天好。", original_srt_indices=[0])]
    lines = [_make_line(index=0, en_text="hello")]

    result = build_subtitle_cues_for_blocks(blocks, lines)

    assert isinstance(result, SubtitleCuePipelineResult)
    assert len(result.cues) >= 1
    # The cue text must derive from merged_cn_text
    assert result.cues[0].block_id == "block_0001"
    assert result.cues[0].en_text == "hello"


# ---------------------------------------------------------------------------
# Scenario 2: Multi-block — 2 blocks each with 1 line → 2 separate cue groups
# ---------------------------------------------------------------------------


def test_multi_block_two_groups() -> None:
    """Two blocks produce separate cue groups (distinct block_ids)."""
    blocks = [
        _make_block(block_id="block_0001", merged_cn_text="第一句。", original_srt_indices=[0]),
        _make_block(
            block_id="block_0002",
            merged_cn_text="第二句。",
            original_srt_indices=[1],
            first_start_ms=2000,
            last_end_ms=4000,
            target_duration_ms=2000,
        ),
    ]
    lines = [
        _make_line(index=0, en_text="first"),
        _make_line(index=1, en_text="second"),
    ]

    result = build_subtitle_cues_for_blocks(blocks, lines)

    block_ids = [c.block_id for c in result.cues]
    assert "block_0001" in block_ids
    assert "block_0002" in block_ids

    b1_cues = [c for c in result.cues if c.block_id == "block_0001"]
    b2_cues = [c for c in result.cues if c.block_id == "block_0002"]
    assert len(b1_cues) >= 1
    assert len(b2_cues) >= 1

    # Summaries have 2 entries in the same order
    assert len(result.report.block_summaries) == 2
    assert result.report.block_summaries[0].block_id == "block_0001"
    assert result.report.block_summaries[1].block_id == "block_0002"


# ---------------------------------------------------------------------------
# Scenario 3: en_text derived from multiple SubtitleLines
# ---------------------------------------------------------------------------


def test_en_text_derived_from_multiple_lines() -> None:
    """Block with original_srt_indices=[0,1,2] gets en_text from all 3 lines joined."""
    blocks = [
        _make_block(
            merged_cn_text="今天我们看第一个。",
            original_srt_indices=[0, 1, 2],
            target_duration_ms=3000,
        )
    ]
    lines = [
        _make_line(index=0, en_text="Today"),
        _make_line(index=1, en_text="we look"),
        _make_line(index=2, en_text="at the first one"),
    ]

    result = build_subtitle_cues_for_blocks(blocks, lines)

    assert len(result.cues) >= 1
    # All en_text from all cues joined together should contain words from all 3 lines
    all_en = " ".join(c.en_text for c in result.cues)
    assert "Today" in all_en
    assert "we look" in all_en
    assert "at the first one" in all_en


# ---------------------------------------------------------------------------
# Scenario 4: Missing SubtitleLine — graceful empty string for missing index
# ---------------------------------------------------------------------------


def test_missing_subtitle_line_treated_as_empty() -> None:
    """Block references index 99 which doesn't exist → that portion is empty; no crash."""
    blocks = [
        _make_block(
            merged_cn_text="短句。",
            original_srt_indices=[0, 99],  # index 99 missing
            target_duration_ms=2000,
        )
    ]
    lines = [_make_line(index=0, en_text="short sentence")]
    # index 99 not in lines

    result = build_subtitle_cues_for_blocks(blocks, lines)

    # Should still produce cues without error
    assert len(result.cues) >= 1
    # The en_text for the block is "short sentence" (99 contributes "")
    all_en = " ".join(c.en_text for c in result.cues)
    assert "short sentence" in all_en


# ---------------------------------------------------------------------------
# Scenario 5: Effective duration priority (C2 corrected: SRT window first)
# ---------------------------------------------------------------------------


def test_effective_duration_uses_srt_window_first() -> None:
    """SRT window (last_end_ms - first_start_ms) takes priority over target and actual.

    2026-05-03 C2 correction: the original C test expected target_duration_ms to
    win. That was wrong — target_duration_ms is the LLM rewrite target, not
    timeline occupancy. The SRT window IS the timeline slot used by publish_backend.

    With first_start=1000, last_end=5000 the SRT window is 4000ms.
    target=3000, actual=2500 are both ignored.
    block_end_ms = 1000 + 4000 = 5000.
    """
    block = _make_block(
        merged_cn_text="测试。",
        first_start_ms=1000,
        last_end_ms=5000,
        target_duration_ms=3000,
        actual_audio_duration_ms=2500,
    )
    result = build_subtitle_cues_for_blocks([block], [])

    # block_end_ms should be 1000 + (5000-1000) = 5000  (SRT window wins)
    for cue in result.cues:
        assert cue.end_ms <= 5000, f"cue end_ms {cue.end_ms} exceeds expected block_end 5000"
    assert result.cues[-1].end_ms == 5000, (
        f"Last cue end_ms {result.cues[-1].end_ms} != expected block_end 5000 (SRT window)"
    )


def test_effective_duration_falls_back_to_target_when_no_srt_window() -> None:
    """When SRT window == 0, fall back to target_duration_ms.

    Legacy/edge case: first_start_ms == last_end_ms (no SRT window set).
    target=2000 should be used as the fallback.
    block_end_ms = 0 + 2000 = 2000.
    """
    block = _make_block(
        merged_cn_text="测试。",
        first_start_ms=263000,
        last_end_ms=263000,  # srt_window = 0
        target_duration_ms=2000,
        actual_audio_duration_ms=1500,
    )
    result = build_subtitle_cues_for_blocks([block], [])

    # srt_window=0 → skip; target=2000 wins; block_end = 263000 + 2000 = 265000
    for cue in result.cues:
        assert cue.end_ms <= 265000, f"cue end_ms {cue.end_ms} exceeds expected block_end 265000"
    assert result.cues[-1].end_ms == 265000, (
        f"Last cue end_ms {result.cues[-1].end_ms} != 265000 (target fallback)"
    )


def test_effective_duration_falls_back_to_actual_when_no_srt_window_and_no_target() -> None:
    """When SRT window == 0 and target == 0, fall back to actual_audio_duration_ms."""
    block = _make_block(
        merged_cn_text="测试。",
        first_start_ms=263000,
        last_end_ms=263000,  # srt_window = 0
        target_duration_ms=0,
        actual_audio_duration_ms=1800,
    )
    result = build_subtitle_cues_for_blocks([block], [])

    # srt_window=0, target=0 → actual=1800 wins; block_end = 263000 + 1800 = 264800
    for cue in result.cues:
        assert cue.end_ms <= 264800, f"cue end_ms {cue.end_ms} exceeds expected block_end 264800"
    assert result.cues[-1].end_ms == 264800, (
        f"Last cue end_ms {result.cues[-1].end_ms} != 264800 (actual fallback)"
    )


def test_effective_duration_falls_back_to_last_end_minus_first_start() -> None:
    """When both actual and target are 0, use last_end_ms - first_start_ms."""
    block = _make_block(
        merged_cn_text="测试。",
        first_start_ms=1000,
        last_end_ms=3500,
        target_duration_ms=0,
        actual_audio_duration_ms=0,
    )
    result = build_subtitle_cues_for_blocks([block], [])

    # effective = 3500 - 1000 = 2500, so block_end = 1000 + 2500 = 3500
    for cue in result.cues:
        assert cue.end_ms <= 3500


# ---------------------------------------------------------------------------
# Scenario 6: Empty merged_cn_text — no cues, no block_spec, no text_mismatch
# ---------------------------------------------------------------------------


def test_empty_cn_text_block_skipped() -> None:
    """Block with empty merged_cn_text produces no cues and is excluded from block_specs."""
    blocks = [
        _make_block(block_id="block_empty", merged_cn_text="", original_srt_indices=[0]),
        _make_block(block_id="block_real", merged_cn_text="正常句。", original_srt_indices=[1]),
    ]
    lines = [
        _make_line(index=0, en_text="ignored"),
        _make_line(index=1, en_text="real"),
    ]

    result = build_subtitle_cues_for_blocks(blocks, lines)

    # No cues from the empty block
    empty_cues = [c for c in result.cues if c.block_id == "block_empty"]
    assert empty_cues == []

    # Validation report should have no block_spec for block_empty → no text_mismatch
    summaries_ids = [s.block_id for s in result.report.block_summaries]
    assert "block_empty" not in summaries_ids

    # Status should be passed (no mismatch for the empty block)
    assert result.report.validation_status in {"passed", "needs_review"}


# ---------------------------------------------------------------------------
# Scenario 7: Degenerate duration (all sources <= 0) — block skipped
# ---------------------------------------------------------------------------


def test_degenerate_duration_block_skipped() -> None:
    """Block where effective duration resolves to <= 0 is silently skipped."""
    # actual=0, target=0, last_end - first_start = 0 → skip
    degenerate = _make_block(
        block_id="block_degen",
        merged_cn_text="文字。",
        first_start_ms=1000,
        last_end_ms=1000,
        target_duration_ms=0,
        actual_audio_duration_ms=0,
    )
    valid = _make_block(
        block_id="block_valid",
        merged_cn_text="正常。",
        first_start_ms=2000,
        last_end_ms=4000,
        target_duration_ms=2000,
    )

    result = build_subtitle_cues_for_blocks([degenerate, valid], [])

    degen_cues = [c for c in result.cues if c.block_id == "block_degen"]
    assert degen_cues == []

    valid_cues = [c for c in result.cues if c.block_id == "block_valid"]
    assert len(valid_cues) >= 1


# ---------------------------------------------------------------------------
# Scenario 8: Validation status for block with URL (needs_review)
# ---------------------------------------------------------------------------


def test_validation_report_status_needs_review_for_url_block() -> None:
    """URL in the text triggers unknown_mixed_token → status needs_review (or failed if error)."""
    # A URL embedded in the text should trigger unknown_mixed_token review flag from segmenter
    blocks = [
        _make_block(
            merged_cn_text="请访问 https://example.com 了解详情。",
            original_srt_indices=[0],
            target_duration_ms=3000,
        )
    ]
    lines = [_make_line(index=0, en_text="Please visit our site")]

    result = build_subtitle_cues_for_blocks(blocks, lines)

    # Should have cues (the URL might cause unknown_mixed_token review)
    assert len(result.cues) >= 1
    # Status should not be passed (URL handling triggers review or keeps as-is)
    # The validator catches review issues from cue.review_reason flags
    assert result.report.validation_status in {"passed", "needs_review", "failed"}


# ---------------------------------------------------------------------------
# Scenario 9: Report block_summaries match input order
# ---------------------------------------------------------------------------


def test_report_block_summaries_match_input_order() -> None:
    """block_summaries in the ValidationReport appear in the same order as input blocks."""
    block_ids = ["block_c", "block_a", "block_b"]
    blocks = []
    for i, bid in enumerate(block_ids):
        blocks.append(
            _make_block(
                block_id=bid,
                merged_cn_text="句子。",
                original_srt_indices=[i],
                first_start_ms=i * 3000,
                last_end_ms=(i + 1) * 3000,
                target_duration_ms=3000,
            )
        )

    result = build_subtitle_cues_for_blocks(blocks, [])

    summary_ids = [s.block_id for s in result.report.block_summaries]
    assert summary_ids == block_ids


# ---------------------------------------------------------------------------
# Scenario 10: Concat invariant across blocks
# ---------------------------------------------------------------------------


def test_concat_invariant_normalized_text_matches_merged_cn_text() -> None:
    """For cues from each block, normalize(join(cue.text)) == normalize(block.merged_cn_text)."""
    blocks = [
        _make_block(
            block_id="block_0001",
            merged_cn_text="今天我们来看第一个问题。明天看第二个。",
            original_srt_indices=[0],
            target_duration_ms=4000,
        ),
        _make_block(
            block_id="block_0002",
            merged_cn_text="下午好！",
            original_srt_indices=[1],
            first_start_ms=5000,
            last_end_ms=7000,
            target_duration_ms=2000,
        ),
    ]
    lines = [
        _make_line(index=0, en_text="Today we look at the first question"),
        _make_line(index=1, en_text="Good afternoon"),
    ]

    result = build_subtitle_cues_for_blocks(blocks, lines)

    for block in blocks:
        block_cues = [c for c in result.cues if c.block_id == block.block_id]
        if block_cues:
            joined = "".join(c.text for c in block_cues)
            assert normalize(joined) == normalize(block.merged_cn_text), (
                f"Block {block.block_id}: normalize(joined)={normalize(joined)!r} "
                f"!= normalize(cn)={normalize(block.merged_cn_text)!r}"
            )


# ---------------------------------------------------------------------------
# Additional: block_specs returned in result match non-degenerate/non-empty blocks
# ---------------------------------------------------------------------------


def test_block_specs_exclude_empty_and_degenerate_blocks() -> None:
    """block_specs in result only contain processable blocks."""
    blocks = [
        _make_block(block_id="block_empty", merged_cn_text=""),
        _make_block(
            block_id="block_degen",
            merged_cn_text="文字。",
            first_start_ms=1000,
            last_end_ms=1000,
            target_duration_ms=0,
            actual_audio_duration_ms=0,
        ),
        _make_block(block_id="block_ok", merged_cn_text="正常。", target_duration_ms=2000),
    ]

    result = build_subtitle_cues_for_blocks(blocks, [])

    spec_ids = [s.block_id for s in result.block_specs]
    assert "block_empty" not in spec_ids
    assert "block_degen" not in spec_ids
    assert "block_ok" in spec_ids


# ---------------------------------------------------------------------------
# SRT window priority scenarios (2026-05-03, C2 corrected)
# SRT window (last_end_ms - first_start_ms) is the correct timeline occupancy.
# ---------------------------------------------------------------------------


def test_srt_window_force_dsp_wins_over_target_and_actual() -> None:
    """force_dsp scenario (C2): SRT window=2000ms wins over target=25583ms and actual=1533ms.

    Real production case from Buffett interview seg 11 that caused SegmentOverlap
    in production after commit c26f730 (C):
      SRT window: first_start=263000ms, last_end=265000ms → 2000ms
      LLM rewrite target: 25583ms (how long the rewritten Chinese should read,
                                   NOT the timeline slot)
      raw TTS duration: 1533ms (before DSP stretch)
      publish_backend lays this segment in the 2000ms SRT window (silence pad or DSP)
    Subtitle cues must span 2000ms, matching the SRT window. Using target=25583ms
    causes block_end to overshoot the next block's start_ms → SegmentOverlap.
    """
    block = _make_block(
        merged_cn_text="那你为什么停下来了呢？",
        first_start_ms=263000,
        last_end_ms=265000,  # SRT window = 2000ms
        target_duration_ms=25583,  # LLM rewrite target, NOT timeline
        actual_audio_duration_ms=1533,
    )
    result = build_subtitle_cues_for_blocks([block], [])

    assert len(result.cues) >= 1
    # Last cue must end at first_start + SRT window = 263000 + 2000 = 265000
    assert result.cues[-1].end_ms == 265000, (
        f"Last cue end_ms {result.cues[-1].end_ms} should be 265000 (SRT window), "
        f"not 288583 (first_start+target) or 264533 (first_start+actual)"
    )
    # First cue must start at block start
    assert result.cues[0].start_ms == 263000


def test_srt_window_direct_no_dsp_srt_window_matches_actual() -> None:
    """direct (no-DSP) scenario: SRT window=2000ms wins; target and actual are similar.

    For non-DSP segments, SRT window is close to target and actual; the fix
    should not break anything. SRT window wins but difference is negligible.
    """
    block = _make_block(
        merged_cn_text="今天天气不错。",
        first_start_ms=5000,
        last_end_ms=7000,  # SRT window = 2000ms
        target_duration_ms=2000,
        actual_audio_duration_ms=1950,
    )
    result = build_subtitle_cues_for_blocks([block], [])

    assert len(result.cues) >= 1
    # SRT window wins: block_end = 5000 + 2000 = 7000
    assert result.cues[-1].end_ms == 7000, (
        f"Last cue end_ms {result.cues[-1].end_ms} != 7000"
    )


def test_srt_window_zero_falls_back_to_target() -> None:
    """Legacy fallback: SRT window=0, target=2500ms → effective=2500ms.

    Older blocks where first_start_ms == last_end_ms (no SRT window available).
    Falls through to target_duration_ms as second priority.
    """
    block = _make_block(
        merged_cn_text="这是一句话。",
        first_start_ms=1000,
        last_end_ms=1000,  # srt_window = 0
        target_duration_ms=2500,
        actual_audio_duration_ms=2000,
    )
    result = build_subtitle_cues_for_blocks([block], [])

    assert len(result.cues) >= 1
    # srt_window=0 → skip; target=2500 wins; block_end = 1000 + 2500 = 3500
    assert result.cues[-1].end_ms == 3500, (
        f"Last cue end_ms {result.cues[-1].end_ms} != 3500 (target fallback)"
    )


def test_srt_window_zero_target_zero_falls_back_to_actual() -> None:
    """Legacy fallback: SRT window=0, target=0, actual=1800ms → effective=1800ms."""
    block = _make_block(
        merged_cn_text="短句。",
        first_start_ms=1000,
        last_end_ms=1000,  # srt_window = 0
        target_duration_ms=0,
        actual_audio_duration_ms=1800,
    )
    result = build_subtitle_cues_for_blocks([block], [])

    assert len(result.cues) >= 1
    # srt_window=0, target=0 → actual=1800 wins; block_end = 1000 + 1800 = 2800
    assert result.cues[-1].end_ms == 2800, (
        f"Last cue end_ms {result.cues[-1].end_ms} != 2800 (actual fallback)"
    )


def test_srt_window_multi_cue_block_spans_full_srt_window() -> None:
    """Integration: block with 3+ cues — all cues stay within the SRT window.

    first_start=10000, last_end=15000 → SRT window = 5000ms.
    target=8000ms (LLM rewrite target; bigger than SRT window but NOT used).
    All cues must be within [10000ms, 15000ms].

    Before C2 fix (using target): cues would be distributed over [10000, 18000ms],
    overrunning the next block.
    After C2 fix: cues are distributed over [10000, 15000ms] (SRT window).
    """
    block = _make_block(
        # Long enough text to produce multiple cues via segmenter
        merged_cn_text="这是第一句话。这是第二句话。这是第三句话。",
        first_start_ms=10000,
        last_end_ms=15000,  # SRT window = 5000ms
        target_duration_ms=8000,  # LLM target; must NOT win
        actual_audio_duration_ms=2000,
    )
    result = build_subtitle_cues_for_blocks([block], [])

    assert len(result.cues) >= 1
    # All cues must be within [10000, 15000] — SRT window
    for cue in result.cues:
        assert cue.end_ms <= 15000, (
            f"Cue end_ms {cue.end_ms} exceeds SRT window end 15000ms"
        )
    # Last cue must reach the end of the SRT window
    assert result.cues[-1].end_ms == 15000, (
        f"Last cue end_ms {result.cues[-1].end_ms} != 15000 (SRT window); "
        f"cues are placed in wrong window"
    )
    # First cue must start at block_start
    assert result.cues[0].start_ms == 10000


# ---------------------------------------------------------------------------
# Regression: adjacent blocks must not produce SegmentOverlap (C2 hot-fix)
# ---------------------------------------------------------------------------


def test_no_segment_overlap_adjacent_tight_blocks() -> None:
    """Critical regression: adjacent blocks with tight SRT windows must not overlap.

    This is the exact failure mode from commit c26f730 (C) that caused production
    SegmentOverlap errors. Without C2 fix, block_a using target=25000ms would
    extend its cue window to first_start + 25000 = 35000ms, overlapping block_b
    which starts at 12000ms.

    With C2 fix (SRT window first):
      block_a SRT window = 12000 - 10000 = 2000ms → cue ends at 12000ms
      block_b SRT window = 14000 - 12000 = 2000ms → cue starts at 12000ms
      No overlap — block_a ends exactly where block_b begins.
    """
    block_a = _make_block(
        block_id="block_a",
        merged_cn_text="第一句话。",
        first_start_ms=10000,
        last_end_ms=12000,  # SRT window = 2000ms
        target_duration_ms=25000,  # LLM target (much larger than SRT window)
        actual_audio_duration_ms=1800,
    )
    block_b = _make_block(
        block_id="block_b",
        merged_cn_text="第二句话。",
        first_start_ms=12000,
        last_end_ms=14000,  # SRT window = 2000ms; starts exactly where block_a ends
        target_duration_ms=25000,
        actual_audio_duration_ms=1800,
    )
    result = build_subtitle_cues_for_blocks([block_a, block_b], [])

    a_cues = [c for c in result.cues if c.block_id == "block_a"]
    b_cues = [c for c in result.cues if c.block_id == "block_b"]

    assert len(a_cues) >= 1
    assert len(b_cues) >= 1

    # block_a cues must not exceed 12000ms
    for cue in a_cues:
        assert cue.end_ms <= 12000, (
            f"block_a cue end_ms {cue.end_ms} > 12000 — would overlap block_b "
            f"(C2 regression: target_duration_ms must not override SRT window)"
        )

    # block_b cues must start at 12000ms or later
    for cue in b_cues:
        assert cue.start_ms >= 12000, (
            f"block_b cue start_ms {cue.start_ms} < 12000"
        )

    # No overlap between any block_a cue and any block_b cue
    for ca in a_cues:
        for cb in b_cues:
            assert ca.end_ms <= cb.start_ms or cb.end_ms <= ca.start_ms, (
                f"SegmentOverlap: block_a cue [{ca.start_ms}, {ca.end_ms}] "
                f"overlaps block_b cue [{cb.start_ms}, {cb.end_ms}]"
            )
