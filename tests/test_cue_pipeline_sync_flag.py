"""Tests for SemanticBlock.tts_input_cn_text + cue-pipeline drift detection
(Phase B of 2026-05-04-subtitle-audio-sync-plan).

When a SemanticBlock's ``merged_cn_text`` differs from
``tts_input_cn_text`` (the joined text that produced its current
audio), cue generation must:
  1. NOT silently emit timestamps from mismatched audio
  2. Emit a ``text_audio_drift`` validation issue so downstream consumers
     (Phase C whisper alignment, future UI badges) can react

Cue pipeline behavior under drift:
  - In Phase B (this commit): emit the issue, but otherwise produce cues
    via the existing proportional layout (current behavior).
  - In Phase C: skip whisper alignment for drift blocks, fall through
    to proportional layout.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# B1: dataclass field
# ---------------------------------------------------------------------------


def test_semantic_block_has_tts_input_cn_text_default_empty():
    """Default empty until the block-builder fills it from segment data.

    Empty must mean "unknown" downstream; the cue-pipeline sync check
    treats empty as in-sync (legacy backfill rule, mirrors the segment
    side from Phase A)."""
    from core.models import SemanticBlock
    b = SemanticBlock(
        block_id="b1", speaker_id="A", speaker_name="A",
        original_srt_indices=[1], first_start_ms=0, last_end_ms=1000,
        target_duration_ms=1000, merged_cn_text="hi",
    )
    assert b.tts_input_cn_text == ""


def test_semantic_block_strips_whitespace_in_post_init():
    """Mirror the existing post_init treatment of merged_cn_text — strip
    whitespace so comparison with merged_cn_text doesn't trip on stray
    leading/trailing spaces."""
    from core.models import SemanticBlock
    b = SemanticBlock(
        block_id="b1", speaker_id="A", speaker_name="A",
        original_srt_indices=[1], first_start_ms=0, last_end_ms=1000,
        target_duration_ms=1000,
        merged_cn_text="  hi  ",
        tts_input_cn_text="  hi  ",
    )
    assert b.merged_cn_text == "hi"
    assert b.tts_input_cn_text == "hi"


# ---------------------------------------------------------------------------
# B2: pipeline _build_blocks populates field from segments
# ---------------------------------------------------------------------------


def _make_segment(
    sid: int,
    cn_text: str,
    *,
    tts_input_cn_text: str | None = None,
    short_merge_absorbed_segment_ids: str = "",
):
    """Minimal DubbingSegment factory for block-builder tests."""
    from services.gemini.translator import DubbingSegment
    return DubbingSegment(
        segment_id=sid,
        speaker_id="A", display_name="A", voice_id="v",
        start_ms=sid * 1000, end_ms=(sid + 1) * 1000,
        target_duration_ms=1000,
        source_text=f"src{sid}",
        cn_text=cn_text,
        tts_input_cn_text=tts_input_cn_text if tts_input_cn_text is not None else cn_text,
        short_merge_absorbed_segment_ids=short_merge_absorbed_segment_ids,
    )


def test_build_blocks_one_to_one_propagates_tts_input_cn_text():
    """Single-segment block: block.tts_input_cn_text == segment.tts_input_cn_text."""
    from pipeline.process import ProcessPipeline
    seg = _make_segment(1, "你好", tts_input_cn_text="你好")
    blocks = ProcessPipeline._build_process_output_blocks(
        ProcessPipeline.__new__(ProcessPipeline), [seg]
    )
    assert len(blocks) == 1
    assert blocks[0].merged_cn_text == "你好"
    assert blocks[0].tts_input_cn_text == "你好"


def test_build_blocks_drift_segment_propagates_drift_to_block():
    """Segment with cn_text != tts_input_cn_text (drift) makes the block
    inherit the drift state — block.merged_cn_text is the new text,
    block.tts_input_cn_text is the audio's original text."""
    from pipeline.process import ProcessPipeline
    seg = _make_segment(1, "用户改后的新文本", tts_input_cn_text="原始合成文本")
    blocks = ProcessPipeline._build_process_output_blocks(
        ProcessPipeline.__new__(ProcessPipeline), [seg]
    )
    assert blocks[0].merged_cn_text == "用户改后的新文本"
    assert blocks[0].tts_input_cn_text == "原始合成文本"


def test_build_blocks_legacy_segment_with_empty_tts_input_backfills_from_cn():
    """Defense-in-depth: even after Phase A's load-time backfill, if a
    segment somehow lands with tts_input_cn_text="" and cn_text non-empty
    (e.g. fresh dataclass construction in tests, manual API usage), the
    block builder treats it as in-sync rather than triggering false drift
    detection."""
    from pipeline.process import ProcessPipeline
    seg = _make_segment(1, "正常文本", tts_input_cn_text="")
    blocks = ProcessPipeline._build_process_output_blocks(
        ProcessPipeline.__new__(ProcessPipeline), [seg]
    )
    assert blocks[0].merged_cn_text == "正常文本"
    assert blocks[0].tts_input_cn_text == "正常文本"  # backfilled


def test_short_merge_join_includes_tts_input_cn_text_in_parallel():
    """When short_merge collapses multiple segments into one base, both
    base.cn_text AND base.tts_input_cn_text get joined the same way.

    Two-segment merge case:
      seg_1: cn='A', tts_input='A'
      seg_2: cn='B', tts_input='B'  (both in sync)
    After merge:
      base.cn_text == "A B"
      base.tts_input_cn_text == "A B"  (also synced)
    """
    from pipeline.process import ProcessPipeline
    seg_1 = _make_segment(1, "A", tts_input_cn_text="A")
    seg_2 = _make_segment(2, "B", tts_input_cn_text="B")
    base = ProcessPipeline._materialize_short_merge_group([seg_1, seg_2])
    assert base.cn_text == "A B"
    assert base.tts_input_cn_text == "A B"


def test_short_merge_preserves_drift_when_one_member_is_drift():
    """If one of the merged members has cn_text != tts_input_cn_text, the
    merged base inherits that drift: base.cn_text uses the new texts but
    base.tts_input_cn_text reflects the old (audio's) texts. Cue pipeline
    will then detect drift on the resulting block."""
    from pipeline.process import ProcessPipeline
    seg_1 = _make_segment(1, "A_new", tts_input_cn_text="A_old")  # drift
    seg_2 = _make_segment(2, "B", tts_input_cn_text="B")          # sync
    base = ProcessPipeline._materialize_short_merge_group([seg_1, seg_2])
    assert base.cn_text == "A_new B"
    assert base.tts_input_cn_text == "A_old B"
    # The assert that matters downstream:
    assert base.cn_text != base.tts_input_cn_text


def test_short_merge_single_segment_returns_unchanged():
    """A 'group' of one is a no-op and tts_input_cn_text passes through."""
    from pipeline.process import ProcessPipeline
    seg = _make_segment(1, "X", tts_input_cn_text="X")
    base = ProcessPipeline._materialize_short_merge_group([seg])
    assert base is seg  # same instance
    assert base.tts_input_cn_text == "X"
