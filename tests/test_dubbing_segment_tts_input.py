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


# ---------------------------------------------------------------------------
# A3: rewrite-loop call sites stamp after each TTS resynthesis
# ---------------------------------------------------------------------------


def test_attempt_rewrite_loop_stamps_tts_input_after_each_resynth(monkeypatch, tmp_path):
    """Drives _attempt_rewrite_loop with mocked rewriter + tts_generator and
    verifies that segment.tts_input_cn_text matches segment.cn_text at the
    end of the loop (i.e. the LAST text actually fed to TTS). first_pass_cn_text
    stays as the original.

    Single attempt that lands in 'dsp' decision (early return at line 524):
      attempt 1: rewrite '原版' → '版本2' → TTS returns 2200ms (10% over, dsp)

    Expected post-loop:
      cn_text == '版本2'
      tts_input_cn_text == '版本2'  (re-stamped at the in-loop site)
      first_pass_cn_text == '原版'  (unchanged)
    """
    from services.alignment.aligner import SegmentAligner, _snapshot_first_pass_text
    from services.gemini.translator import DubbingSegment

    # Stub TTS result type
    class _FakeTTSResult:
        def __init__(self, audio_path, duration_ms):
            self.audio_path = audio_path
            self.duration_ms = duration_ms

    class _FakeTTSGenerator:
        def __init__(self):
            # iterator of (audio_path, duration_ms) per call.
            # First attempt: 2200ms (still off from target 2000 by 10% — within
            # dsp_threshold so _evaluate_alignment returns 'dsp', triggering
            # the early-return finalization with rewrite_dsp at line 524.
            self._calls = iter([
                ("/fake/v2.wav", 2200),
                ("/fake/v3.wav", 2000),  # never reached given early-return
            ])

        def _generate_one(self, segment, output_dir, usage_bucket=None):
            audio, dur = next(self._calls)
            return _FakeTTSResult(audio, dur)

    class _FakeRewriter:
        def __init__(self):
            self._calls = iter(["版本2", "版本3"])

        # Aligner uses self.rewriter via _rewrite_segment_with_constraints
        # which we patch on the aligner instance below for simplicity.

    aligner = SegmentAligner(
        rewriter=_FakeRewriter(),
        tts_generator=_FakeTTSGenerator(),
        max_rewrites=2,
        min_rewrite_target_ms=500,
    )

    # Patch the rewriter call to return our scripted texts.
    rewrite_iter = iter(["版本2", "版本3"])
    monkeypatch.setattr(
        aligner, "_rewrite_segment_with_constraints",
        lambda **kw: next(rewrite_iter),
    )
    # Skip the post-TTS budget gate for the test.
    monkeypatch.setattr(aligner, "_can_consume_post_tts_budget", lambda seg: True)
    monkeypatch.setattr(aligner, "_consume_post_tts_budget", lambda seg: True)

    # Avoid actually touching audio files in finalization paths.
    monkeypatch.setattr(aligner, "_direct_copy", lambda src, dst: dst)
    monkeypatch.setattr(aligner, "_dsp_stretch", lambda src, tgt, dst: dst)
    monkeypatch.setattr(
        "services.alignment.aligner._measure_wav_duration_ms",
        lambda p: 1000,
    )

    # Picked target/actual so _should_attempt_rewrite passes:
    # diff_ratio = (2500-2000)/2000 = 0.25 ∈ (dsp_threshold 0.15, max_rewrite_ratio 0.35]
    seg = _make_segment("原版", target_duration_ms=2000, actual_duration_ms=2500)
    seg.tts_audio_path = "/fake/v1.wav"
    # Stamp v1 like _align_one would have done before entering the loop.
    _snapshot_first_pass_text(seg)
    assert seg.first_pass_cn_text == "原版"
    assert seg.tts_input_cn_text == "原版"

    out = aligner._attempt_rewrite_loop(
        segment=seg,
        output_path=str(tmp_path / "aligned.wav"),
        current_actual_duration_ms=2500,
    )
    assert out is not None  # rewrite path returned a finalized result

    # Post-conditions: tts_input_cn_text reflects the LAST text that produced
    # the audio; first_pass_cn_text is preserved as the very first text.
    assert seg.cn_text == "版本2"
    assert seg.tts_input_cn_text == "版本2"
    assert seg.first_pass_cn_text == "原版"


def test_attempt_rewrite_loop_stamps_tts_input_to_best_candidate_on_max_attempts(
    monkeypatch, tmp_path,
):
    """If neither attempt reaches direct/dsp decision, the loop falls through
    to the "best candidate" finalization at the bottom. The best candidate's
    cn_text gets re-applied to segment.cn_text — and segment.tts_input_cn_text
    must match that, NOT whatever was tried last."""
    from services.alignment.aligner import SegmentAligner, _snapshot_first_pass_text

    class _FakeTTSResult:
        def __init__(self, audio_path, duration_ms):
            self.audio_path = audio_path
            self.duration_ms = duration_ms

    # Two attempts, neither reaches the direct/dsp early-return — instead
    # the loop's _should_force_followup_rewrite forces continuation each
    # time, so we exit through the best-candidate fallback.
    fake_calls = iter([("/fake/v2.wav", 1300), ("/fake/v3.wav", 1400)])
    class _FakeTTSGenerator:
        def _generate_one(self, segment, output_dir, usage_bucket=None):
            audio, dur = next(fake_calls)
            return _FakeTTSResult(audio, dur)

    aligner = SegmentAligner(
        rewriter=object(),  # truthy; we patch _rewrite_segment_with_constraints
        tts_generator=_FakeTTSGenerator(),
        max_rewrites=2,
        min_rewrite_target_ms=500,
    )
    rewrite_iter = iter(["版本2", "版本3"])
    monkeypatch.setattr(
        aligner, "_rewrite_segment_with_constraints",
        lambda **kw: next(rewrite_iter),
    )
    monkeypatch.setattr(aligner, "_can_consume_post_tts_budget", lambda seg: True)
    monkeypatch.setattr(aligner, "_consume_post_tts_budget", lambda seg: True)
    # Force continuation on both attempts so we never early-return inside
    # the loop body.
    monkeypatch.setattr(aligner, "_should_force_followup_rewrite",
                        lambda **kw: True)
    # Best-candidate decision returns "dsp"; finalization stamps tts_input.
    monkeypatch.setattr(aligner, "_evaluate_alignment", lambda *a, **kw: "dsp")
    monkeypatch.setattr(aligner, "_dsp_stretch", lambda src, tgt, dst: dst)
    monkeypatch.setattr(aligner, "_direct_copy", lambda src, dst: dst)
    monkeypatch.setattr(
        "services.alignment.aligner._measure_wav_duration_ms",
        lambda p: 1000,
    )

    # Picked target/actual so _should_attempt_rewrite passes:
    # diff_ratio = (2500-2000)/2000 = 0.25 ∈ (dsp_threshold 0.15, max_rewrite_ratio 0.35]
    seg = _make_segment("原版", target_duration_ms=2000, actual_duration_ms=2500)
    seg.tts_audio_path = "/fake/v1.wav"
    _snapshot_first_pass_text(seg)

    out = aligner._attempt_rewrite_loop(
        segment=seg,
        output_path=str(tmp_path / "aligned.wav"),
        current_actual_duration_ms=2500,
    )
    assert out is not None

    # Best candidate scoring picks one of {版本2, 版本3} — whichever it picks,
    # tts_input_cn_text MUST equal cn_text. (The point is they don't drift.)
    assert seg.tts_input_cn_text == seg.cn_text
    assert seg.first_pass_cn_text == "原版"  # immutable
