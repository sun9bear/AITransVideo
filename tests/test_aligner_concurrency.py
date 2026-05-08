"""Characterization + concurrency-readiness tests for SegmentAligner.

Pinned in P2-17a-0 (refactor for thread-safety, no behavior change).
P2-17a-1 (thread-pool rollout) will add behavior tests for parallel
execution, cancellation, semaphore limits, etc. to this file.

Two contracts these tests lock in:
1. Serial ``align_all`` orchestration: input-order preservation, the three
   ``[S5]`` progress/skip/stale log strings, and per-segment DSP audit
   isolation.
2. ``PostTTSBudgetTracker`` single-thread invariants (root inheritance,
   global cap), so that step 5's ``threading.RLock`` does not silently
   change semantics under no-contention loads.

The serial-path tests must pass before AND after the refactor in steps
3-5. Drift here is the bisect signal for review.

ffmpeg/ffprobe note: orchestration tests mock ``_align_one`` and
``_ffprobe_duration_ms`` so they run on dev boxes without ffmpeg.
The DSP-audit isolation test (step 3's load-bearing characterization)
needs real DSP and is skipped when ffmpeg is unavailable; CI / Linux
containers run it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from pydub import AudioSegment
from pydub.generators import Sine
import pytest

from modules.output.project_output import AlignedSegment
import services.alignment.aligner as aligner_module
from services.alignment.aligner import (
    DEFAULT_MAX_POST_TTS_ADJUSTMENTS_PER_SEGMENT,
    PostTTSBudgetTracker,
    SegmentAligner,
)
from services.gemini.translator import DUBBING_MODE_DUB, DubbingSegment


_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(
    not _FFMPEG_AVAILABLE,
    reason="ffmpeg/ffprobe not on PATH (this test exercises real DSP)",
)


def _export_tone_wav(path: Path, *, duration_ms: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Sine(440).to_audio_segment(duration=duration_ms).export(path, format="wav")
    return path


def _build_segment(
    *,
    segment_id: int,
    audio_path: Path,
    start_ms: int = 0,
    end_ms: int = 1_000,
    actual_duration_ms: int | None = None,
) -> DubbingSegment:
    measured = len(AudioSegment.from_wav(audio_path))
    return DubbingSegment(
        segment_id=segment_id,
        speaker_id="speaker_a",
        display_name="Speaker A",
        voice_id="voice_demo_001",
        start_ms=start_ms,
        end_ms=end_ms,
        target_duration_ms=end_ms - start_ms,
        source_text="Demo source text.",
        cn_text="demo cn text",
        tts_audio_path=str(audio_path.resolve(strict=False)),
        actual_duration_ms=measured if actual_duration_ms is None else actual_duration_ms,
    )


def _stub_align_one_writes_wav(self, segment, output_dir):
    """Replacement for ``SegmentAligner._align_one`` used by orchestration
    tests. Writes a non-empty file at the expected output path so cache
    checks downstream still see a "valid" output, and returns a minimal
    AlignedSegment that pins the input segment_id (for order assertions).
    """

    del self
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"segment_{segment.segment_id:03d}_aligned.wav"
    out_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    return AlignedSegment(
        segment_id=segment.segment_id,
        speaker_id=segment.speaker_id,
        display_name=segment.display_name,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        cn_text=segment.cn_text,
        en_text="",
        aligned_audio_path=str(out_path),
        actual_duration_ms=segment.target_duration_ms,
        alignment_method="direct",
        needs_review=False,
        dubbing_mode=DUBBING_MODE_DUB,
    )


# ---------------------------------------------------------------------------
# 1. Input-order preservation in serial align_all
# ---------------------------------------------------------------------------


def test_align_all_preserves_input_order_in_serial_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returned ``AlignedSegment`` list must be positionally aligned with the
    input list.

    Pipeline callers (process.py:2807, 6064, 6224) treat this as a contract
    by either iterating zip-style or relying on _align_one's in-place
    mutation of segments[i]. Future parallel rollout in 17a-1 must
    preserve this even when inner _align_one calls finish out-of-order.
    """

    tone = _export_tone_wav(tmp_path / "in" / "tone.wav", duration_ms=1_000)
    # Non-monotonic segment_ids to detect any sort-by-id reordering.
    segments = [
        _build_segment(segment_id=42, audio_path=tone, end_ms=1_000),
        _build_segment(segment_id=7, audio_path=tone, end_ms=1_000),
        _build_segment(segment_id=99, audio_path=tone, end_ms=1_000),
    ]

    monkeypatch.setattr(SegmentAligner, "_align_one", _stub_align_one_writes_wav)

    aligned = SegmentAligner().align_all(segments, str(tmp_path / "aligned"))

    assert [seg.segment_id for seg in aligned] == [42, 7, 99]


# ---------------------------------------------------------------------------
# 2-4. The three ``[S5]`` log strings and their cadence
# ---------------------------------------------------------------------------


def test_align_all_progress_log_cadence_every_15_and_at_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pin the exact `[S5] 对齐进度: i/N 段` string and the every-15 +
    final cadence. The current condition is
    ``total_segments > 0 and (index % 15 == 0 or index == total_segments)``.
    The parallel rollout in 17a-1 must keep emitting *equivalent* progress
    when running through the serial fallback path
    (``AVT_ALIGN_MAX_WORKERS=1`` → ``_align_all_serial()``).
    """

    tone = _export_tone_wav(tmp_path / "in" / "tone.wav", duration_ms=1_000)
    segments = [
        _build_segment(segment_id=i, audio_path=tone, end_ms=1_000)
        for i in range(1, 17)  # 16 segments
    ]

    monkeypatch.setattr(SegmentAligner, "_align_one", _stub_align_one_writes_wav)

    SegmentAligner().align_all(segments, str(tmp_path / "aligned"))

    out = capsys.readouterr().out
    # Two progress lines: i=15 (i % 15 == 0), i=16 (== total).
    assert "[S5] 对齐进度: 15/16 段" in out
    assert "[S5] 对齐进度: 16/16 段" in out
    # And nothing in between (no every-1 chatter on 16-segment runs).
    assert "[S5] 对齐进度: 14/16 段" not in out
    assert "[S5] 对齐进度: 1/16 段" not in out


def test_align_all_logs_skip_message_for_cached_fresh_segment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pin the `[S5] 跳过已完成的对齐段 i/N` log + the cache-fresh path.

    Conditions: aligned wav exists & non-empty, raw tts mtime <= aligned
    mtime → no re-alignment.
    """

    tone = _export_tone_wav(tmp_path / "in" / "cached.wav", duration_ms=1_000)
    aligned_dir = tmp_path / "aligned"
    aligned_dir.mkdir(parents=True, exist_ok=True)

    # Pre-create a valid aligned wav whose mtime is newer than the raw tts.
    cached_aligned = aligned_dir / "segment_001_aligned.wav"
    cached_aligned.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    raw_mtime = tone.stat().st_mtime
    os.utime(cached_aligned, (raw_mtime + 10, raw_mtime + 10))

    # Cache-fresh path probes aligned duration via ffprobe; mock it so
    # the test runs without ffmpeg installed.
    monkeypatch.setattr(aligner_module, "_ffprobe_duration_ms", lambda path: 1_000)

    segment = _build_segment(segment_id=1, audio_path=tone, end_ms=1_000)
    aligned_results = SegmentAligner().align_all([segment], str(aligned_dir))

    out = capsys.readouterr().out
    assert "[S5] 跳过已完成的对齐段 1/1" in out
    assert aligned_results[0].alignment_method == "checkpoint"


def test_align_all_logs_stale_cache_when_raw_newer_than_aligned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pin the `[S5] 对齐缓存已过期，重新处理段 i/N` log path.

    Conditions: aligned wav exists & non-empty, but raw tts mtime > aligned
    mtime → re-align. This triggers when a user edits a segment and its
    TTS gets regenerated under the same path.
    """

    tone = _export_tone_wav(tmp_path / "in" / "stale.wav", duration_ms=1_000)
    aligned_dir = tmp_path / "aligned"
    aligned_dir.mkdir(parents=True, exist_ok=True)

    # Pre-create a stale aligned wav (older than raw tts).
    stale_aligned = aligned_dir / "segment_001_aligned.wav"
    stale_aligned.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    aligned_mtime = stale_aligned.stat().st_mtime
    os.utime(tone, (aligned_mtime + 10, aligned_mtime + 10))

    # Stale path triggers re-align via _align_one — stub it so we don't
    # need ffmpeg here either.
    monkeypatch.setattr(SegmentAligner, "_align_one", _stub_align_one_writes_wav)

    segment = _build_segment(segment_id=1, audio_path=tone, end_ms=1_000)
    SegmentAligner().align_all([segment], str(aligned_dir))

    out = capsys.readouterr().out
    assert "[S5] 对齐缓存已过期，重新处理段 1/1" in out


# ---------------------------------------------------------------------------
# 5. Per-segment DSP audit isolation (the load-bearing characterization for
#    step 3 refactor — `_last_dsp_fit_result` removal must not break this).
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_align_all_dsp_audit_fields_isolated_between_segments(
    tmp_path: Path,
) -> None:
    """Two segments both routed through the DSP path with *different* speed
    ratios. After ``align_all`` returns, each segment's ``dsp_*`` fields
    must reflect ITS OWN fit result, not the previous segment's.

    This currently passes because ``_align_one`` resets
    ``self._last_dsp_fit_result = None`` and ``_clear_dsp_fit_audit`` at
    the start of every call — but those are *implicit* per-call resets
    that fail under any worker reuse. Step 3 makes the isolation explicit
    by removing the field; this test must keep passing.
    """

    # Segment 1: shrink (17s -> 15s). DSP route, speed_ratio > 1.
    seg1_audio = _export_tone_wav(tmp_path / "in" / "shrink.wav", duration_ms=17_000)
    # Segment 2: expand (18s -> 20.5s). DSP route, speed_ratio < 1.
    # Must be > min_abs_diff_ms (2000ms) to escape the slight-underflow direct
    # bypass in _evaluate_alignment.
    seg2_audio = _export_tone_wav(tmp_path / "in" / "expand.wav", duration_ms=18_000)

    seg1 = _build_segment(segment_id=1, audio_path=seg1_audio, end_ms=15_000)
    seg2 = _build_segment(
        segment_id=2, audio_path=seg2_audio, start_ms=15_000, end_ms=35_500
    )

    aligned = SegmentAligner().align_all([seg1, seg2], str(tmp_path / "aligned"))

    # Both should land in the dsp branch (within 15% threshold, not direct).
    assert aligned[0].alignment_method == "dsp"
    assert aligned[1].alignment_method == "dsp"

    # Speed ratios must reflect each segment's own stretch direction —
    # shrink > 1.0, expand < 1.0. If audit fields leaked from seg1 to
    # seg2, both would carry seg1's ratio.
    assert seg1.dsp_speed_ratio_used > 1.0
    assert seg2.dsp_speed_ratio_used < 1.0
    assert seg1.dsp_speed_ratio_used != pytest.approx(
        seg2.dsp_speed_ratio_used, abs=1e-6
    )

    # And the initial duration field reflects each segment's own raw
    # input, not the neighbor's.
    assert seg1.dsp_initial_duration_ms == pytest.approx(17_000, abs=20)
    assert seg2.dsp_initial_duration_ms == pytest.approx(18_000, abs=20)


# ---------------------------------------------------------------------------
# 6. PostTTSBudgetTracker single-thread invariants (pre-lock characterization
#    for step 5).
# ---------------------------------------------------------------------------


def test_post_tts_budget_tracker_default_cap_matches_constant() -> None:
    """The default cap is the named constant — pinned so step 5's lock
    refactor doesn't accidentally re-tune the budget."""

    tracker = PostTTSBudgetTracker()
    assert tracker.max_extra_tts_per_root == DEFAULT_MAX_POST_TTS_ADJUSTMENTS_PER_SEGMENT


def test_post_tts_budget_tracker_register_child_inherits_root_quota(
    tmp_path: Path,
) -> None:
    """When ``register_child_segments(parent, children)`` is called, the
    children share the parent's root quota. Total consumption across the
    parent + children must never exceed ``max_extra_tts_per_root``.

    Step 5 (RLock around register/consume/remaining) must keep this
    invariant under no-contention single-thread access too.
    """

    tone = _export_tone_wav(tmp_path / "in" / "p.wav", duration_ms=1_000)
    parent = _build_segment(segment_id=1, audio_path=tone, end_ms=1_000)
    child_a = _build_segment(segment_id=11, audio_path=tone, end_ms=500)
    child_b = _build_segment(
        segment_id=12, audio_path=tone, start_ms=500, end_ms=1_000
    )

    tracker = PostTTSBudgetTracker(max_extra_tts_per_root=3)
    root_id = tracker.register_child_segments(
        parent_segment=parent, child_segments=[child_a, child_b]
    )
    # The root_id is the parent's segment_id, and both children resolve to
    # the same root.
    assert root_id == parent.segment_id
    assert tracker.root_id_for_segment(child_a) == parent.segment_id
    assert tracker.root_id_for_segment(child_b) == parent.segment_id

    # Consume 2 against parent, 1 against child_a → total 3 → cap reached.
    assert tracker.try_consume_for_segment(parent, 2) is True
    assert tracker.try_consume_for_segment(child_a, 1) is True
    assert tracker.remaining_for_segment(parent) == 0
    assert tracker.remaining_for_segment(child_a) == 0
    assert tracker.remaining_for_segment(child_b) == 0
    # Any further consume — by parent OR child — must fail.
    assert tracker.try_consume_for_segment(child_b, 1) is False
    assert tracker.try_consume_for_segment(parent, 1) is False


def test_post_tts_budget_tracker_unregistered_segment_uses_self_as_root(
    tmp_path: Path,
) -> None:
    """A segment never passed to ``register_child_segments`` is its own
    root. This is what gives independent quota to top-level segments
    that never get split."""

    tone = _export_tone_wav(tmp_path / "in" / "lone.wav", duration_ms=1_000)
    seg_a = _build_segment(segment_id=10, audio_path=tone, end_ms=1_000)
    seg_b = _build_segment(segment_id=20, audio_path=tone, end_ms=1_000)

    tracker = PostTTSBudgetTracker(max_extra_tts_per_root=2)

    assert tracker.root_id_for_segment(seg_a) == 10
    assert tracker.root_id_for_segment(seg_b) == 20
    assert tracker.try_consume_for_segment(seg_a, 2) is True
    # seg_a is exhausted but seg_b's bucket is independent.
    assert tracker.try_consume_for_segment(seg_a, 1) is False
    assert tracker.try_consume_for_segment(seg_b, 2) is True
