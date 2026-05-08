"""Tests for cross-block cue overlap clamping in cue_pipeline.

Regression for 2026-05-08 Charlie Munger SegmentOverlap incident:
real-audio segments can legitimately overlap when speakers interrupt
each other (e.g. interview banter). Audio renders fine on per-speaker
tracks, but the SUBTITLE track is single-laned — pyJianYingDraft's
SegmentOverlap check rejects two cues whose time windows touch.

The pipeline-level fix walks all cues in start_ms order and clamps
any later cue's start to ``prev_end + 1ms``. If that crushes the
later cue's duration below ``min_display_ms``, also extend its end
so the cue stays readable. The ``cue_validator``'s existing
timing_overlap check only catches within-block overlap; this layer
addresses the cross-block case the validator misses.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _make_cue(cue_id: str, block_id: str, start_ms: int, end_ms: int, *, text: str = "x"):
    from modules.subtitles.cue_models import SubtitleCue
    return SubtitleCue(
        cue_id=cue_id,
        block_id=block_id,
        speaker_id="A",
        speaker_name="A",
        text=text,
        en_text="x",
        start_ms=start_ms,
        end_ms=end_ms,
        source="semantic_block_v2_whisper_aligned",
    )


# ---------------------------------------------------------------------------
# Single-cue / empty / no-overlap baselines
# ---------------------------------------------------------------------------


def test_clamp_no_op_on_empty_input():
    from modules.subtitles.cue_pipeline import _clamp_cross_block_cue_overlaps
    out = _clamp_cross_block_cue_overlaps([], min_display_ms=500)
    assert out == []


def test_clamp_no_op_on_single_cue():
    from modules.subtitles.cue_pipeline import _clamp_cross_block_cue_overlaps
    cue = _make_cue("c1", "b1", 0, 1000)
    out = _clamp_cross_block_cue_overlaps([cue], min_display_ms=500)
    assert len(out) == 1
    assert out[0].start_ms == 0 and out[0].end_ms == 1000


def test_clamp_no_op_when_no_overlap():
    """Adjacent cues with a gap → no change. Identity preservation."""
    from modules.subtitles.cue_pipeline import _clamp_cross_block_cue_overlaps
    a = _make_cue("a", "b1", 0, 1000)
    b = _make_cue("b", "b2", 1500, 2500)  # 500ms gap after a ends
    out = _clamp_cross_block_cue_overlaps([a, b], min_display_ms=500)
    assert out[0].start_ms == 0 and out[0].end_ms == 1000
    assert out[1].start_ms == 1500 and out[1].end_ms == 2500


# ---------------------------------------------------------------------------
# Cross-block overlap — the Charlie Munger case
# ---------------------------------------------------------------------------


def test_clamp_cross_block_small_overlap():
    """The exact pattern from job_cd2a366e3beb4d01b0d4f461fe53f220:
      block 361 cue ends at 5939332ms
      block 362 cue starts at 5939284ms (48ms before block 361 ends)
    Clamp must push block 362 cue start to 5939333ms (5939332+1ms gap).
    The cue is long (3840ms), so duration stays well above min_display_ms."""
    from modules.subtitles.cue_pipeline import _clamp_cross_block_cue_overlaps
    a = _make_cue("a", "block_361", 5938835, 5939332)
    b = _make_cue("b", "block_362", 5939284, 5943124)  # 48ms cross-block overlap
    out = _clamp_cross_block_cue_overlaps([a, b], min_display_ms=500)
    assert out[0].start_ms == 5938835
    assert out[0].end_ms == 5939332          # earlier cue untouched
    assert out[1].start_ms == 5939333        # clamped to prev_end + 1ms
    assert out[1].end_ms == 5943124          # end untouched (cue still long)
    # Result: zero overlap.
    assert out[0].end_ms < out[1].start_ms


def test_clamp_extends_end_when_clamping_would_crush_duration():
    """If clamping start_ms would shrink the cue below min_display_ms,
    push end_ms out to preserve readability."""
    from modules.subtitles.cue_pipeline import _clamp_cross_block_cue_overlaps
    a = _make_cue("a", "b1", 0, 1500)
    # short cue starting INSIDE a, would be only 100ms after clamp
    b = _make_cue("b", "b2", 1400, 1600)  # original 200ms duration
    out = _clamp_cross_block_cue_overlaps([a, b], min_display_ms=500)
    assert out[0].end_ms == 1500
    assert out[1].start_ms == 1501           # clamped
    # Original duration (1600 - 1501 = 99ms) < min_display_ms → end pushed out
    assert out[1].end_ms == 2001             # 1501 + 500ms min
    assert out[1].end_ms - out[1].start_ms == 500


def test_clamp_cascades_when_extension_creates_new_overlap():
    """When end-extension makes cue B reach into cue C, the next iter
    clamps C too. Verifies sequential pass handles cascade."""
    from modules.subtitles.cue_pipeline import _clamp_cross_block_cue_overlaps
    a = _make_cue("a", "b1", 0, 1500)
    b = _make_cue("b", "b2", 1400, 1600)   # short, will get extended
    c = _make_cue("c", "b3", 1700, 3000)   # would have overlapped with extended b
    out = _clamp_cross_block_cue_overlaps([a, b, c], min_display_ms=500)
    # b: 1501..2001 (extended)
    assert out[1].start_ms == 1501 and out[1].end_ms == 2001
    # c: clamped because b now ends at 2001 > c.start 1700
    assert out[2].start_ms == 2002
    assert out[2].end_ms == 3000  # c is already 998ms long after clamp, no extend needed


def test_clamp_preserves_cue_identity_fields():
    """cue_id, block_id, text, source, etc. must be identical pre-/post-clamp.
    Only start_ms and end_ms can change."""
    from modules.subtitles.cue_pipeline import _clamp_cross_block_cue_overlaps
    a = _make_cue("a", "b1", 0, 1000, text="你好")
    b = _make_cue("b", "b2", 500, 1500, text="世界")  # overlaps
    out = _clamp_cross_block_cue_overlaps([a, b], min_display_ms=500)
    assert out[0].text == "你好"
    assert out[0].cue_id == "a"
    assert out[0].block_id == "b1"
    assert out[1].text == "世界"
    assert out[1].cue_id == "b"
    assert out[1].block_id == "b2"
    assert out[1].source == "semantic_block_v2_whisper_aligned"


def test_clamp_does_not_mutate_input_list():
    """Input cues must not be mutated. Pipeline relies on this so
    block_specs / report-bound state stays consistent."""
    from modules.subtitles.cue_pipeline import _clamp_cross_block_cue_overlaps
    a = _make_cue("a", "b1", 0, 1000)
    b = _make_cue("b", "b2", 500, 1500)
    inputs = [a, b]
    _ = _clamp_cross_block_cue_overlaps(inputs, min_display_ms=500)
    # original cues unchanged
    assert inputs[0].start_ms == 0 and inputs[0].end_ms == 1000
    assert inputs[1].start_ms == 500 and inputs[1].end_ms == 1500


# ---------------------------------------------------------------------------
# End-to-end through build_subtitle_cues_for_blocks
# ---------------------------------------------------------------------------


def test_pipeline_emits_zero_overlapping_cues_after_clamp(monkeypatch, tmp_path):
    """Two SemanticBlocks whose timing windows overlap must produce
    cues that DO NOT overlap after the pipeline runs end-to-end.
    This is the runtime contract the cue_validator's timing_overlap
    check fails to enforce across blocks."""
    from core.models import SemanticBlock, SubtitleLine
    from modules.subtitles.cue_pipeline import build_subtitle_cues_for_blocks

    # Two blocks where block_2 starts before block_1 ends — exactly
    # the source overlap that triggered Charlie Munger's failure.
    block_1 = SemanticBlock(
        block_id="b1", speaker_id="A", speaker_name="A",
        original_srt_indices=[1],
        first_start_ms=5938835,
        last_end_ms=5939332,
        target_duration_ms=497,
        merged_cn_text="你父亲的儿子，对吧？",
        tts_input_cn_text="你父亲的儿子，对吧？",
    )
    block_2 = SemanticBlock(
        block_id="b2", speaker_id="B", speaker_name="B",
        original_srt_indices=[2],
        first_start_ms=5939284,  # 48ms BEFORE block_1 ends
        last_end_ms=5942000,
        target_duration_ms=2716,
        merged_cn_text="没错完全相同。",
        tts_input_cn_text="没错完全相同。",
    )
    lines = [
        SubtitleLine(index=1, start_ms=5938835, end_ms=5939332,
                     speaker_id="A", speaker_name="A", en_text="x", cn_text="你父亲的儿子，对吧？"),
        SubtitleLine(index=2, start_ms=5939284, end_ms=5942000,
                     speaker_id="B", speaker_name="B", en_text="y", cn_text="没错完全相同。"),
    ]

    result = build_subtitle_cues_for_blocks([block_1, block_2], lines)

    # Sort cues by start to verify zero overlap.
    sorted_cues = sorted(result.cues, key=lambda c: c.start_ms)
    overlap_pairs = []
    for i in range(len(sorted_cues) - 1):
        if sorted_cues[i].end_ms > sorted_cues[i+1].start_ms:
            overlap_pairs.append(
                (sorted_cues[i].cue_id, sorted_cues[i+1].cue_id,
                 sorted_cues[i].end_ms, sorted_cues[i+1].start_ms),
            )
    assert overlap_pairs == [], (
        f"Pipeline emitted overlapping cues across blocks (would trigger "
        f"jianying SegmentOverlap): {overlap_pairs}"
    )
