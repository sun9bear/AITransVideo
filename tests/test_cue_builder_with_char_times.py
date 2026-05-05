"""Tests for ``cue_builder.build_cues_with_char_times`` (Phase C C3 helper).

Mirrors ``build_cues_for_block`` but takes a per-char timestamp list
(produced by ``whisper_align.align_chars_to_words``) instead of a flat
``[block_start_ms, block_end_ms]`` slot.  The cue pipeline integration
in C3 uses this only when:
  - whisper alignment ran successfully
  - DTW produced a non-empty char_times series
Otherwise it falls back to the existing ``build_cues_for_block``.

The helper itself is required to:
  - Reuse ``segment_text(cn_text)`` so cue boundaries / needs_review /
    review_reason / English-text split match the existing path exactly
    — the ONLY difference is timing.
  - Treat ``char_times`` as WAV-local. Final cue times are
    ``block_start_ms + local_time``, clamped to ``[block_start_ms, block_end_ms]``.
  - Return ``[]`` on any anomaly (length mismatch, non-monotonic input,
    times outside [0, block_end_ms - block_start_ms]) so the cue
    pipeline can fall back. Never silently produce broken cues.

CodeX guardrail: helper must NEVER raise to publish — return [] only.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _make_char_times(cn_text: str, *, base_ms: int = 0, step_ms: int = 100) -> list[dict]:
    """Synthetic per-char times: char i → [base + i*step, base + (i+1)*step].
    Lets tests assert specific time placements.

    All times are WAV-LOCAL (zero-relative to the block's audio).
    """
    return [
        {"start_ms": base_ms + i * step_ms,
         "end_ms": base_ms + (i + 1) * step_ms,
         "text": cn_text[i]}
        for i in range(len(cn_text))
    ]


# ---------------------------------------------------------------------------
# Happy path: cues use whisper-driven timing
# ---------------------------------------------------------------------------


def test_cues_use_char_times_for_span_boundaries():
    """A two-sentence cn_text "你好。世界。" → segment_text produces 2 spans
    "你好。" (3 chars) and "世界。" (3 chars). With char_times that put
    those sentences at 100-400ms and 500-800ms locally, the cues' global
    times are block_start_ms + local_time."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    cn_text = "你好。世界。"
    char_times = [
        {"start_ms": 100, "end_ms": 200, "text": "你"},
        {"start_ms": 200, "end_ms": 300, "text": "好"},
        {"start_ms": 300, "end_ms": 400, "text": "。"},
        {"start_ms": 500, "end_ms": 600, "text": "世"},
        {"start_ms": 600, "end_ms": 700, "text": "界"},
        {"start_ms": 700, "end_ms": 800, "text": "。"},
    ]
    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text=cn_text, en_text="hello. world.",
        block_start_ms=10_000, block_end_ms=20_000,
        char_times=char_times,
    )
    assert len(cues) == 2

    # Cue 1: span "你好。" → spans char[0..2] → local 100..400 → global 10100..10400
    assert cues[0].start_ms == 10_100
    assert cues[0].end_ms == 10_400
    assert cues[0].text == "你好。"
    # Cue 2: span "世界。" → spans char[3..5] → local 500..800 → global 10500..10800
    assert cues[1].start_ms == 10_500
    assert cues[1].end_ms == 10_800
    assert cues[1].text == "世界。"


def test_helper_returns_empty_on_char_times_far_out_of_slot_bounds():
    """char_times.end_ms exceeding the block's slot duration by MORE than
    the tolerance (~100ms ≈ one CJK char duration) is a real anomaly:
    something corrupted the upstream timing data. Treat as fallback per
    CodeX guidance ("越界 → build_cues_for_block()")."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    cn_text = "你好。"
    char_times = [
        {"start_ms": 0, "end_ms": 200, "text": "你"},
        {"start_ms": 200, "end_ms": 400, "text": "好"},
        {"start_ms": 400, "end_ms": 12_000, "text": "。"},  # 2000ms past — way beyond
    ]
    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text=cn_text, en_text="hello",
        block_start_ms=0, block_end_ms=10_000,  # 10s slot
        char_times=char_times,
    )
    assert cues == []


def test_cues_clamp_at_slot_boundary_when_char_times_at_edge():
    """When char_times sit exactly at the slot boundary (legitimate
    end-of-WAV case), the helper produces cues clamped to block_end_ms
    rather than rejecting — boundary times are not "out of bounds"."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    cn_text = "你好。"
    char_times = [
        {"start_ms": 0, "end_ms": 3_333, "text": "你"},
        {"start_ms": 3_333, "end_ms": 6_666, "text": "好"},
        {"start_ms": 6_666, "end_ms": 10_000, "text": "。"},  # exactly slot end
    ]
    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text=cn_text, en_text="hello",
        block_start_ms=0, block_end_ms=10_000,
        char_times=char_times,
    )
    assert len(cues) == 1
    assert cues[0].start_ms == 0
    assert cues[0].end_ms == 10_000


# ---------------------------------------------------------------------------
# 2026-05-05 follow-up: small-overshoot tolerance.
#
# Production rerun on the reshape task showed 2/85 segments fell back
# because DTW interpolation of the trailing unanchored char produced
# end_ms = (last_word_end + 80ms), which overshot the slot by ~17ms
# (typical: whisper's last word ends ~60ms before slot end; +80 char
# duration overshoots by ~17). Rejecting these is over-strict; the slot
# clamp via _to_global handles the small overshoot cleanly.
#
# Rule: ≤ 100ms overshoot → continue (_to_global clamps to block_end_ms).
# > 100ms overshoot → real anomaly, fall back.
# ---------------------------------------------------------------------------


def test_cues_clamp_when_overshoot_is_within_tolerance(tmp_path):
    """char_times.end_ms exceeding slot_duration by ~17ms (production
    case from DTW trailing-char interpolation) MUST produce
    whisper-aligned cues with the last cue clamped to block_end_ms,
    not fall back to proportional. Threshold is 100ms; 17ms is well
    inside it."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    cn_text = "你好。"
    # Slot 10_000 — last char overshoots by 17ms (10_017 > 10_000).
    char_times = [
        {"start_ms": 0, "end_ms": 3_300, "text": "你"},
        {"start_ms": 3_300, "end_ms": 6_600, "text": "好"},
        {"start_ms": 6_600, "end_ms": 10_017, "text": "。"},  # +17ms overshoot
    ]
    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text=cn_text, en_text="hello",
        block_start_ms=0, block_end_ms=10_000,
        char_times=char_times,
    )
    assert len(cues) == 1
    # Last cue's end clamped to slot, not 10_017.
    assert cues[0].end_ms == 10_000
    # And it IS the whisper-aligned source (proves we didn't fall back).
    assert "whisper" in cues[0].source.lower()


def test_cues_fall_back_when_overshoot_is_beyond_tolerance(tmp_path):
    """500ms overshoot is beyond the ~100ms tolerance → fall back.
    Anything that big indicates corrupted upstream timing (DTW shouldn't
    interpolate that far), and silently clamping it would mask the bug."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    cn_text = "你好。"
    char_times = [
        {"start_ms": 0, "end_ms": 3_300, "text": "你"},
        {"start_ms": 3_300, "end_ms": 6_600, "text": "好"},
        {"start_ms": 6_600, "end_ms": 10_500, "text": "。"},  # +500ms overshoot
    ]
    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text=cn_text, en_text="hello",
        block_start_ms=0, block_end_ms=10_000,
        char_times=char_times,
    )
    assert cues == []


def test_cues_carry_needs_review_from_segment_span():
    """A span flagged ``needs_review=True`` (e.g. unknown_mixed_token)
    must surface in the cue. This is the same propagation rule as the
    proportional path — we're only replacing TIMING."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    # English-mixed text → segmenter likely flags as unknown_mixed_token
    cn_text = "Hello世界"
    char_times = _make_char_times(cn_text)
    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text=cn_text, en_text="Hello world",
        block_start_ms=0, block_end_ms=10_000,
        char_times=char_times,
    )
    # At least one cue is flagged — the exact propagation matches what
    # segment_text() returns, so we cross-check against that.
    from modules.subtitles.semantic_segmenter import segment_text
    spans = segment_text(cn_text)
    expected_review = any(s.needs_review for s in spans)
    actual_review = any(c.needs_review for c in cues)
    assert actual_review == expected_review


def test_cues_have_distinct_source_tag_from_proportional_path():
    """Cues built by the whisper-aligned path get a source like
    ``semantic_block_v2_whisper_aligned`` so the quality report can
    distinguish them from proportional-layout cues."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    cn_text = "你好。"
    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text=cn_text, en_text="hi",
        block_start_ms=0, block_end_ms=1000,
        char_times=_make_char_times(cn_text),
    )
    assert len(cues) >= 1
    assert "whisper" in cues[0].source.lower()


# ---------------------------------------------------------------------------
# Anomaly fallbacks: helper returns [] so caller drops back to proportional
# ---------------------------------------------------------------------------


def test_helper_returns_empty_when_char_times_length_mismatches_cn_text():
    """char_times must have one entry per cn_text character. If lengths
    disagree, something corrupt happened upstream — return [] so the
    cue pipeline falls back rather than producing broken cue boundaries."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    cn_text = "你好世界"  # 4 chars
    char_times = _make_char_times("你好")  # 2 entries — mismatch
    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text=cn_text, en_text="x",
        block_start_ms=0, block_end_ms=1000,
        char_times=char_times,
    )
    assert cues == []


def test_helper_returns_empty_on_non_monotonic_char_times():
    """A char_times where char[i+1].start_ms < char[i].start_ms is
    pathologically broken — refuse to build cues from it."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    cn_text = "你好。"
    # Out of order: char[2] starts BEFORE char[1] ends
    char_times = [
        {"start_ms": 100, "end_ms": 500, "text": "你"},
        {"start_ms": 500, "end_ms": 700, "text": "好"},
        {"start_ms": 200, "end_ms": 600, "text": "。"},  # back-in-time
    ]
    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text=cn_text, en_text="x",
        block_start_ms=0, block_end_ms=10_000,
        char_times=char_times,
    )
    assert cues == []


def test_helper_returns_empty_for_empty_cn_text():
    """No cn_text → no cues. Mirrors the proportional path's empty handling."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text="", en_text="",
        block_start_ms=0, block_end_ms=1000,
        char_times=[],
    )
    assert cues == []


def test_helper_returns_empty_for_empty_char_times():
    """Caller passing empty char_times should already have fallen back
    via DTW disjoint check; defense-in-depth: don't build cues."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text="你好",
        en_text="hi",
        block_start_ms=0, block_end_ms=1000,
        char_times=[],
    )
    assert cues == []


def test_helper_never_raises_on_pathological_input():
    """CodeX guardrail: any anomaly → return [] so publish doesn't fail.
    Sweep a few weird inputs and confirm none raise."""
    from modules.subtitles.cue_builder import build_cues_with_char_times

    # block_end_ms <= block_start_ms is pathological too
    cues = build_cues_with_char_times(
        block_id="b1", speaker_id="A", speaker_name="A",
        cn_text="你好",
        en_text="hi",
        block_start_ms=1000, block_end_ms=500,  # invalid window
        char_times=_make_char_times("你好"),
    )
    assert cues == []
