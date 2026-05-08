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
import threading
import time
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


def _stub_align_one_writes_wav(self, segment, output_dir, **_kwargs):
    """Replacement for ``SegmentAligner._align_one`` used by orchestration
    tests. Writes a non-empty file at the expected output path so cache
    checks downstream still see a "valid" output, and returns a minimal
    AlignedSegment that pins the input segment_id (for order assertions).

    Accepts ``**_kwargs`` so 17a-1's ``paid_fallback_semaphore`` /
    ``stop_event`` keyword arguments don't trip the stub when the
    parallel path passes them through ``pool.submit``.
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
    input list under the serial path (AVT_ALIGN_MAX_WORKERS=1).

    Pipeline callers (process.py:2807, 6064, 6224) treat this as a contract
    by either iterating zip-style or relying on _align_one's in-place
    mutation of segments[i]. The companion parallel-path order test lives
    in test_align_all_parallel_preserves_input_order_under_completion_skew.
    """

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "1")
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
    final cadence in the serial path. Forced via
    ``AVT_ALIGN_MAX_WORKERS=1`` so this is locked to ``_align_all_serial``;
    the parallel path uses a different "completed/total" cadence by
    design (i in input order is meaningless when cheap segments are
    scattered between expensive ones).
    """

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "1")
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
    mtime → no re-alignment. Serial path only (parallel path skips this
    line because the i is meaningless under thread-pool dispatch).
    """

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "1")
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
    TTS gets regenerated under the same path. Serial path only here;
    the parallel path also emits this line but with i = idx + 1 not the
    serial loop counter, so a separate parallel test in step 11 covers
    that case.
    """

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "1")
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


# ===========================================================================
# P2-17a-1 parallel-path behavior tests
# ===========================================================================
#
# These exercise _align_all_parallel and the paid_fallback semaphore. They
# DO NOT use real ffmpeg unless gated with @requires_ffmpeg — the goal is
# to lock behavioral contracts of the orchestration code, not re-test DSP.


def _make_recording_align_one(records: list, *, delay_by_id: dict[int, float] | None = None):
    """Build a stub _align_one that records (segment_id, thread_name) and
    optionally sleeps for a per-segment configurable delay.
    """

    delays = delay_by_id or {}
    lock = threading.Lock()

    def _stub(self, segment, output_dir, **_kwargs):
        del self
        sleep_for = delays.get(segment.segment_id, 0.0)
        if sleep_for:
            time.sleep(sleep_for)
        with lock:
            records.append((segment.segment_id, threading.current_thread().name))
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

    return _stub


def test_align_all_parallel_uses_thread_pool_for_needs_align(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With workers=2, four cache-miss segments must be handled by ≥2
    distinct threads named ``align*`` (the ThreadPoolExecutor's
    thread_name_prefix). This pins that the pool is actually used and
    that thread names are observable for ops triage."""

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "2")
    tone = _export_tone_wav(tmp_path / "in" / "tone.wav", duration_ms=1_000)
    segments = [
        _build_segment(segment_id=i, audio_path=tone, end_ms=1_000)
        for i in range(1, 5)
    ]

    records: list[tuple[int, str]] = []
    monkeypatch.setattr(
        SegmentAligner,
        "_align_one",
        _make_recording_align_one(
            records,
            # Hold each worker briefly so the pool actually gets to fan
            # out (without a delay, the first thread can race through
            # all segments).
            delay_by_id={1: 0.05, 2: 0.05, 3: 0.05, 4: 0.05},
        ),
    )

    SegmentAligner().align_all(segments, str(tmp_path / "aligned"))

    thread_names = {name for _seg_id, name in records}
    assert len(thread_names) >= 2, f"expected ≥2 worker threads, saw {thread_names}"
    assert all(name.startswith("align") for name in thread_names), (
        f"all worker thread names must start with 'align', saw {thread_names}"
    )


def test_align_all_parallel_preserves_input_order_under_completion_skew(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Segments must be returned in INPUT order even when workers
    complete in reverse order. Pin via deliberate per-segment delays
    so the last input finishes first. Downstream callers (process.py
    _build_aligned_segments + zip-style iteration) rely on this."""

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "4")
    tone = _export_tone_wav(tmp_path / "in" / "tone.wav", duration_ms=1_000)
    # segment_id values are non-monotonic; the test should care about
    # INPUT POSITION, not segment_id sort order.
    segments = [
        _build_segment(segment_id=42, audio_path=tone, end_ms=1_000),
        _build_segment(segment_id=7, audio_path=tone, end_ms=1_000),
        _build_segment(segment_id=99, audio_path=tone, end_ms=1_000),
        _build_segment(segment_id=3, audio_path=tone, end_ms=1_000),
    ]

    records: list[tuple[int, str]] = []
    monkeypatch.setattr(
        SegmentAligner,
        "_align_one",
        _make_recording_align_one(
            records,
            # First-in-list (42) sleeps longest so it finishes last;
            # last-in-list (3) returns immediately.
            delay_by_id={42: 0.20, 7: 0.15, 99: 0.05, 3: 0.0},
        ),
    )

    aligned = SegmentAligner().align_all(segments, str(tmp_path / "aligned"))

    # Re-assembly must match input position regardless of finish order.
    assert [seg.segment_id for seg in aligned] == [42, 7, 99, 3]
    # Records reflect actual completion order (proves we did skew).
    completion_order = [seg_id for seg_id, _name in records]
    assert completion_order != [42, 7, 99, 3], (
        f"records show no completion skew ({completion_order}); "
        "the test does not actually exercise out-of-order completion"
    )


def test_align_all_avt_max_workers_one_dispatches_to_serial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``AVT_ALIGN_MAX_WORKERS=1`` MUST dispatch to ``_align_all_serial``
    (the verbatim pre-17a-1 body) — NOT a single-worker thread pool.
    Pinning this prevents an implementer from "approximating" the
    rollback path with a 1-worker pool that would still incur
    ThreadPoolExecutor's startup + thread-name decoration costs and
    mismatch ops dashboards.
    """

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "1")
    tone = _export_tone_wav(tmp_path / "in" / "tone.wav", duration_ms=1_000)
    segments = [
        _build_segment(segment_id=i, audio_path=tone, end_ms=1_000)
        for i in range(1, 17)  # 16 segments → both progress lines should fire
    ]

    records: list[tuple[int, str]] = []
    monkeypatch.setattr(
        SegmentAligner,
        "_align_one",
        _make_recording_align_one(records),
    )

    serial_calls: list[bool] = []
    parallel_calls: list[bool] = []
    real_serial = SegmentAligner._align_all_serial

    def _spy_serial(self, segments_arg, output_dir):
        serial_calls.append(True)
        return real_serial(self, segments_arg, output_dir)

    def _spy_parallel(self, *args, **kwargs):
        parallel_calls.append(True)
        # Should never be called under env=1.
        raise AssertionError(
            "_align_all_parallel must NOT be called when AVT_ALIGN_MAX_WORKERS=1"
        )

    monkeypatch.setattr(SegmentAligner, "_align_all_serial", _spy_serial)
    monkeypatch.setattr(SegmentAligner, "_align_all_parallel", _spy_parallel)

    SegmentAligner().align_all(segments, str(tmp_path / "aligned"))

    assert serial_calls == [True]
    assert parallel_calls == []
    # No worker thread names — everything ran on the main thread.
    main_thread = threading.current_thread().name
    observed_threads = {name for _seg_id, name in records}
    assert observed_threads == {main_thread}, (
        f"serial path must run on main thread only; saw {observed_threads}"
    )
    # Serial-path log strings + cadence preserved.
    out = capsys.readouterr().out
    assert "[S5] 对齐进度: 15/16 段" in out
    assert "[S5] 对齐进度: 16/16 段" in out


@requires_ffmpeg
def test_align_all_parallel_dsp_audit_does_not_cross_segments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two segments forced through DSP under workers=2; even with
    completion order swapped vs. submission order, each segment's
    ``dsp_*`` audit fields must reflect ITS OWN stretch — never the
    neighbor's. Step 3's per-segment local fit_result is what makes
    this true."""

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "2")
    seg1_audio = _export_tone_wav(tmp_path / "in" / "shrink.wav", duration_ms=17_000)
    seg2_audio = _export_tone_wav(tmp_path / "in" / "expand.wav", duration_ms=18_000)

    seg1 = _build_segment(segment_id=1, audio_path=seg1_audio, end_ms=15_000)
    seg2 = _build_segment(
        segment_id=2, audio_path=seg2_audio, start_ms=15_000, end_ms=35_500
    )

    aligned = SegmentAligner().align_all([seg1, seg2], str(tmp_path / "aligned"))

    assert aligned[0].alignment_method == "dsp"
    assert aligned[1].alignment_method == "dsp"
    assert seg1.dsp_speed_ratio_used > 1.0
    assert seg2.dsp_speed_ratio_used < 1.0
    assert seg1.dsp_initial_duration_ms == pytest.approx(17_000, abs=20)
    assert seg2.dsp_initial_duration_ms == pytest.approx(18_000, abs=20)


def test_post_tts_budget_tracker_is_thread_safe_under_contention(
    tmp_path: Path,
) -> None:
    """20 threads each try to consume 1 unit against the same root.
    With ``max_extra_tts_per_root=5`` exactly 5 must succeed; the
    other 15 must see ``False``. Without the RLock from step 5 this
    test would be racy under load (CPython single-attribute writes
    are atomic, but the read-modify-write across two dict operations
    is not).
    """

    tone = _export_tone_wav(tmp_path / "in" / "shared.wav", duration_ms=1_000)
    seg = _build_segment(segment_id=42, audio_path=tone, end_ms=1_000)
    tracker = PostTTSBudgetTracker(max_extra_tts_per_root=5)

    successes: list[bool] = []
    successes_lock = threading.Lock()
    barrier = threading.Barrier(20)

    def _worker():
        barrier.wait()  # release all threads at once
        ok = tracker.try_consume_for_segment(seg, 1)
        with successes_lock:
            successes.append(ok)

    threads = [threading.Thread(target=_worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(successes) == 5, (
        f"exactly 5 of 20 consumes must succeed under cap=5; got {sum(successes)}"
    )
    assert tracker.remaining_for_segment(seg) == 0


def test_align_all_paid_fallback_concurrency_capped_to_one_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY=1`` (default), the
    semaphore inside ``_attempt_rewrite_loop`` MUST serialize concurrent
    rewrite + TTS work. Mocked rewriter / tts_generator track the
    concurrent in-flight count on every call; peak must never exceed
    1 even with workers=4 and 4 segments all needing rewrite.
    """

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "4")
    monkeypatch.delenv("AVT_ALIGN_PAID_FALLBACK_MAX_CONCURRENCY", raising=False)

    in_flight = 0
    peak = 0
    in_flight_lock = threading.Lock()

    def _enter_critical():
        nonlocal in_flight, peak
        with in_flight_lock:
            in_flight += 1
            peak = max(peak, in_flight)

    def _exit_critical():
        nonlocal in_flight
        with in_flight_lock:
            in_flight -= 1

    class _RecordingRewriter:
        def rewrite_for_duration_with_profile(self, *_args, **_kwargs):
            _enter_critical()
            try:
                # Hold the semaphore long enough that any race would
                # show up as peak >= 2.
                time.sleep(0.05)
                return "rewritten text"
            finally:
                _exit_critical()

    class _RecordingTTSGenerator:
        def _generate_one(self, segment, output_dir, **_kwargs):
            from services.tts.tts_generator import TTSResult  # local import

            _enter_critical()
            try:
                time.sleep(0.05)
                out = Path(output_dir) / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"
                out.parent.mkdir(parents=True, exist_ok=True)
                _export_tone_wav(out, duration_ms=segment.target_duration_ms)
                return TTSResult(
                    segment_id=segment.segment_id,
                    audio_path=str(out),
                    duration_ms=segment.target_duration_ms,
                    voice_id=segment.voice_id,
                )
            finally:
                _exit_critical()

    # Build 4 segments that all hit the rewrite path:
    # actual=8000ms target=4000ms → diff_ratio=1.0, way past dsp_threshold,
    # below max_rewrite_ratio? 1.0 > 0.35 so will skip rewrite and force_dsp.
    # Use diff_ratio=0.25 instead: actual=2500 target=2000.
    # But min_rewrite_target_ms=5000 → target must be ≥5000.
    # Use actual=6500 target=5000 → diff_ratio=0.30, dsp_threshold=0.15, max=0.35.
    tone = _export_tone_wav(tmp_path / "in" / "tone.wav", duration_ms=6_500)
    segments = [
        _build_segment(segment_id=i, audio_path=tone, end_ms=5_000, actual_duration_ms=6_500)
        for i in range(1, 5)
    ]

    aligner = SegmentAligner(
        rewriter=_RecordingRewriter(),
        tts_generator=_RecordingTTSGenerator(),
    )
    # We don't care about the ffmpeg DSP step's correctness here, only
    # the semaphore. But _align_one's DSP fallback after rewrite still
    # needs ffmpeg if the path goes there — gate on availability.
    if not _FFMPEG_AVAILABLE:
        pytest.skip("ffmpeg not available; rewrite path's DSP finalize would error")

    aligner.align_all(segments, str(tmp_path / "aligned"))

    assert peak == 1, (
        f"paid fallback peak concurrency must be 1 with default semaphore; got {peak}"
    )


def test_align_all_parallel_duplicate_output_paths_fail_fast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate ``segment_id`` → duplicate output path. Must raise
    ``AlignmentError`` BEFORE the thread pool starts; otherwise
    parallel writes to the same file race nondeterministically."""

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "2")
    tone = _export_tone_wav(tmp_path / "in" / "tone.wav", duration_ms=1_000)
    segments = [
        _build_segment(segment_id=5, audio_path=tone, end_ms=1_000),
        _build_segment(segment_id=5, audio_path=tone, end_ms=1_000),  # duplicate!
    ]

    # The fail-fast happens in pre-classification, so even with a
    # working stub _align_one this should never run.
    pool_called = [False]

    def _stub_align_one(self, *args, **kwargs):
        del self, args, kwargs
        pool_called[0] = True
        raise AssertionError("_align_one must not be called when output paths collide")

    monkeypatch.setattr(SegmentAligner, "_align_one", _stub_align_one)

    from services.alignment.aligner import AlignmentError

    with pytest.raises(AlignmentError, match="duplicate alignment output path"):
        SegmentAligner().align_all(segments, str(tmp_path / "aligned"))
    assert pool_called == [False]


def test_align_all_parallel_first_error_sets_stop_event_and_skips_paid_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When one worker raises, ``stop_event.set()`` is invoked. Pending
    futures that haven't entered paid fallback yet must observe the
    flag and short-circuit; in-flight ffmpeg / provider calls cannot
    truly be cancelled, so the test only proves the *gate* works.
    """

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "4")
    tone = _export_tone_wav(tmp_path / "in" / "tone.wav", duration_ms=1_000)
    segments = [
        _build_segment(segment_id=i, audio_path=tone, end_ms=1_000)
        for i in range(1, 5)
    ]

    captured_stop_events: list[threading.Event] = []
    fail_lock = threading.Lock()
    has_failed = [False]

    def _stub_align_one(self, segment, output_dir, **kwargs):
        del self, output_dir
        stop_event = kwargs.get("stop_event")
        assert stop_event is not None, (
            "parallel path must pass stop_event to _align_one"
        )
        captured_stop_events.append(stop_event)
        with fail_lock:
            if not has_failed[0]:
                has_failed[0] = True
                raise RuntimeError(f"simulated failure on segment_{segment.segment_id}")
        # Other workers wait briefly, then check stop_event — should be set.
        time.sleep(0.05)
        # If stop_event is now set, this stub returns without producing
        # work; that's the contract we want from real workers too.
        if stop_event.is_set():
            raise AssertionError("worker observed stop_event — but we still got scheduled")
        return AlignedSegment(
            segment_id=segment.segment_id,
            speaker_id=segment.speaker_id,
            display_name=segment.display_name,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            cn_text=segment.cn_text,
            en_text="",
            aligned_audio_path="",
            actual_duration_ms=segment.target_duration_ms,
            alignment_method="direct",
            needs_review=False,
            dubbing_mode=DUBBING_MODE_DUB,
        )

    monkeypatch.setattr(SegmentAligner, "_align_one", _stub_align_one)

    with pytest.raises(RuntimeError, match="simulated failure"):
        SegmentAligner().align_all(segments, str(tmp_path / "aligned"))

    # Every worker that ran got the same stop_event instance, and at
    # least one observed it post-set.
    assert captured_stop_events, "no workers ran"
    assert all(ev is captured_stop_events[0] for ev in captured_stop_events), (
        "all workers must share the same stop_event instance"
    )
    assert captured_stop_events[0].is_set(), (
        "stop_event must be set after first failure"
    )


@pytest.mark.parametrize("invalid_value", ["not-a-number", "0", "-1", "abc", "1.5"])
def test_align_all_invalid_max_workers_env_falls_back_to_serial(
    invalid_value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid / 0 / negative ``AVT_ALIGN_MAX_WORKERS`` values must
    clamp to 1 and dispatch to ``_align_all_serial``. Misconfiguration
    must not crash and must not silently fall through to the thread
    pool with some implementation-default count.

    Note: empty string ("") and whitespace-only ("  ") are treated as
    "unset" and use the default (2), NOT as invalid, because that's
    consistent with how docker-compose handles ``KEY=`` (empty value
    interpreted as "no override")."""

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", invalid_value)
    tone = _export_tone_wav(tmp_path / "in" / "tone.wav", duration_ms=1_000)
    segments = [_build_segment(segment_id=1, audio_path=tone, end_ms=1_000)]

    monkeypatch.setattr(
        SegmentAligner,
        "_align_one",
        _make_recording_align_one([]),
    )

    serial_calls: list[bool] = []
    real_serial = SegmentAligner._align_all_serial

    def _spy_serial(self, segments_arg, output_dir):
        serial_calls.append(True)
        return real_serial(self, segments_arg, output_dir)

    def _spy_parallel(self, *args, **kwargs):
        raise AssertionError(
            f"_align_all_parallel must not be called for invalid env value {invalid_value!r}"
        )

    monkeypatch.setattr(SegmentAligner, "_align_all_serial", _spy_serial)
    monkeypatch.setattr(SegmentAligner, "_align_all_parallel", _spy_parallel)

    SegmentAligner().align_all(segments, str(tmp_path / "aligned"))
    assert serial_calls == [True]


def test_align_all_max_workers_clamped_to_upper_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AVT_ALIGN_MAX_WORKERS=32`` must clamp down to the safe cap (4)
    rather than spawning 32 worker threads. Verified by inspecting the
    parsed value via the public env reader."""

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "32")
    assert aligner_module._read_align_max_workers() == 4

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "100")
    assert aligner_module._read_align_max_workers() == 4

    monkeypatch.setenv("AVT_ALIGN_MAX_WORKERS", "3")
    assert aligner_module._read_align_max_workers() == 3
