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
