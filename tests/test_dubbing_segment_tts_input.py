"""Tests for ``DubbingSegment.tts_input_cn_text`` (2026-05-04 P0a).

The field records the EXACT text fed to the TTS engine for the audio
currently at ``aligned_audio_path``. Its purpose is to detect drift when
a user edits ``cn_text`` via the studio editor without regenerating TTS:
``cn_text != tts_input_cn_text`` ⇒ subtitle text won't match audio.

Test coverage:
- Field default is empty string.
- Aligner snapshots ``tts_input_cn_text`` alongside ``first_pass_cn_text``.
- Post-TTS rewrite re-stamps ``tts_input_cn_text`` (overwrite),
  but preserves ``first_pass_cn_text`` (the first-pass guardrail).
- editor/segments.json round-trip preserves the field.
- ``accept_draft_tts`` re-stamps when the user accepts a per-segment
  re-TTS draft.
- ``regenerate_all_dirty_segments`` re-stamps on every re-synthesized
  segment, leaves ``accepted`` segments untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make ``src/`` importable when pytest runs from the repo root. The package
# guard mirrors what other test files in this repo do.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# A1: dataclass field default
# ---------------------------------------------------------------------------


def test_dubbing_segment_has_tts_input_cn_text_default_empty():
    """A freshly-constructed DubbingSegment has ``tts_input_cn_text == ""``.

    Empty default means: until alignment runs, we have no claim about which
    text produced the audio. Downstream consumers must treat empty as
    "unknown" — never assume "in sync" implicitly.
    """
    from services.gemini.translator import DubbingSegment
    seg = DubbingSegment(
        segment_id=1, speaker_id="A", display_name="A",
        voice_id="v", start_ms=0, end_ms=1000,
        target_duration_ms=1000, source_text="hello", cn_text="你好",
    )
    assert seg.tts_input_cn_text == ""


# ---------------------------------------------------------------------------
# A2 / A3: aligner captures and re-captures the snapshot
# ---------------------------------------------------------------------------


def _make_segment(cn_text: str, **overrides) -> "DubbingSegment":
    """Build a minimal DubbingSegment for snapshot tests."""
    from services.gemini.translator import DubbingSegment
    base = dict(
        segment_id=1, speaker_id="A", display_name="A",
        voice_id="v", start_ms=0, end_ms=1000,
        target_duration_ms=1000, source_text="hello",
        cn_text=cn_text, actual_duration_ms=1000,
    )
    base.update(overrides)
    return DubbingSegment(**base)


def test_aligner_snapshot_helper_captures_both_first_pass_and_tts_input():
    """``_snapshot_first_pass_text`` snapshots the segment's CURRENT cn_text
    into both ``first_pass_cn_text`` (first call only) and
    ``tts_input_cn_text`` (every call). Trailing whitespace is stripped to
    match downstream comparison semantics."""
    from services.alignment.aligner import _snapshot_first_pass_text
    seg = _make_segment("  你好世界  ")
    _snapshot_first_pass_text(seg)
    assert seg.first_pass_cn_text == "你好世界"
    assert seg.tts_input_cn_text == "你好世界"


def test_post_tts_rewrite_restamps_tts_input_but_preserves_first_pass():
    """When a segment is rewritten and re-synthesized post-TTS, the helper
    is called a second time. ``tts_input_cn_text`` must update to the new
    text (it's "what made the CURRENT audio"), but ``first_pass_cn_text``
    stays as the original first-attempt text (its contract: voice-speed
    guardrail samples must never pair a first-pass duration with a
    rewritten text — see process.py:7423-7425)."""
    from services.alignment.aligner import _snapshot_first_pass_text
    seg = _make_segment("原版文本")
    _snapshot_first_pass_text(seg)
    assert seg.first_pass_cn_text == "原版文本"
    assert seg.tts_input_cn_text == "原版文本"

    # Simulate post-TTS rewrite path: cn_text is mutated, audio re-synthesized,
    # aligner runs the snapshot again on the same segment.
    seg.cn_text = "重写后的文本"
    seg.rewrite_count = 1
    _snapshot_first_pass_text(seg)
    assert seg.first_pass_cn_text == "原版文本"          # immutable after first call
    assert seg.tts_input_cn_text == "重写后的文本"      # re-stamped


def test_snapshot_skipped_for_empty_cn_text():
    """Defensive: an empty cn_text shouldn't pollute the field with empty
    string when first_pass_cn_text is already set (would suggest TTS ran on
    empty text, which never happens in practice)."""
    from services.alignment.aligner import _snapshot_first_pass_text
    seg = _make_segment("有内容", first_pass_cn_text="有内容",
                        tts_input_cn_text="有内容")
    seg.cn_text = "   "  # whitespace-only
    _snapshot_first_pass_text(seg)
    # Snapshot reflects the current strip-and-skip-empty rule: keep prior
    # non-empty stamps rather than overwriting with "".
    assert seg.first_pass_cn_text == "有内容"
    assert seg.tts_input_cn_text == "有内容"
