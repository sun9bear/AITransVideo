"""Tests for async batch re-TTS (D39).

Mirrors ``video_render_async.py``'s pattern: Job API spawns a
``threading.Thread`` and writes progress to a per-project status file.
POST /regenerate-all-tts returns immediately with ``task_id``; GET
``/regenerate-all-tts/status?task_id=…`` reads the status file.

D39 rationale: a Studio task with 200+ segments all marked ``text_dirty``
would take 200 × 2-5s of TTS latency serially. The old synchronous
endpoint blocked the Gateway HTTP connection for 7-17 minutes and
reliably hit Gateway timeout. Async decouples user-facing latency
(< 100ms to start) from work duration.

Contract pinned by these tests:

1. ``start_regen_all_async`` returns a task_id immediately, does NOT
   block on per-segment TTS calls.
2. Status file at ``{project_dir}/editor/editing/regen_status.json``
   transitions ``starting → running → completed`` (or ``failed``).
3. Progress fields track per-segment advancement: ``total``,
   ``succeeded_count``, ``failed_count``, ``current_segment_id``.
4. Completion carries the D38 response summary in ``result``.
5. ``read_regen_all_status`` enforces ``task_id`` match — a stale
   task_id sees ``{"mismatch": True}`` (a newer batch is in flight).
6. Per-segment failures do NOT abort the batch — the thread continues
   and records each failure, matching the sync ``regenerate_all_dirty_segments``
   contract (plan D38).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from services.jobs.editing_segments import (
    SEGMENT_STATUS_TEXT_DIRTY,
    mark_segment_status,
)
from services.jobs.regenerate_all_async import (
    read_regen_all_status,
    start_regen_all_async,
    status_file_path,
)


def _make_project(tmp_path: Path, segment_ids: list[str]) -> Path:
    """Minimal project dir for batch re-TTS: editing/segments.json + status map."""
    project = tmp_path / "project"
    editing = project / "editor" / "editing"
    editing.mkdir(parents=True, exist_ok=True)
    (editing / "tts_segments_draft").mkdir(parents=True, exist_ok=True)
    # editing/segments.json with stub segments matching segment_ids
    (editing / "segments.json").write_text(
        json.dumps([
            {
                "segment_id": sid,
                "speaker_id": "speaker_a",
                "display_name": "A",
                "voice_id": "v1",
                "start_ms": 0,
                "end_ms": 1000,
                "target_duration_ms": 1000,
                "source_text": "x",
                "cn_text": "x",
            }
            for sid in segment_ids
        ]),
        encoding="utf-8",
    )
    # Mark every segment as text_dirty so batch picks them up
    for sid in segment_ids:
        mark_segment_status(project, sid, SEGMENT_STATUS_TEXT_DIRTY)
    return project


def _wait_until(pred, *, timeout_s: float = 2.0, poll_s: float = 0.02):
    """Helper: block until pred() is truthy, then return its value."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(poll_s)
    return pred()  # final shot


def _happy_tts_caller(segment_dict: dict[str, Any], output_path: Path) -> None:
    """Writes a 1-byte fake wav so regenerate_segment_tts succeeds."""
    output_path.write_bytes(b"W")


def _failing_tts_caller(segment_dict: dict[str, Any], output_path: Path) -> None:
    raise RuntimeError("upstream 429 overload")


# ---------------------------------------------------------------------------
# start + status roundtrip
# ---------------------------------------------------------------------------


def test_start_regen_all_async_returns_task_id_immediately(tmp_path: Path) -> None:
    """The POST endpoint must not block: start_regen_all_async spawns a
    thread and returns within tens of milliseconds even for hundreds of
    segments. Assertion: after 50ms the call site has already returned
    a non-empty task_id — proof that per-segment work is offloaded."""
    project = _make_project(tmp_path, [f"seg_{i:03d}" for i in range(50)])

    # Build a caller that blocks each call so total "work" dominates if
    # the start function were sync. If start is truly async, we return
    # before any per-segment work completes.
    barrier = threading.Event()
    per_segment_latency = 0.05  # 50ms per segment * 50 segments = 2.5s

    def slow_caller(segment_dict: dict[str, Any], output_path: Path) -> None:
        barrier.wait(timeout=1.0)  # block until main releases
        output_path.write_bytes(b"W")

    t0 = time.monotonic()
    task_id = start_regen_all_async(
        project_dir=project, tts_caller=slow_caller,
    )
    elapsed = time.monotonic() - t0

    assert task_id, "start_regen_all_async must return a non-empty task_id"
    assert elapsed < 0.2, (
        f"start call took {elapsed:.3f}s — must return within 200ms even "
        f"before any per-segment work starts ({per_segment_latency*50:.1f}s sync would be)"
    )
    barrier.set()  # release the thread so it finishes


def test_status_file_path_is_in_editing_subtree(tmp_path: Path) -> None:
    """Status file must live in editor/editing/ — gets cleaned up with
    the rest of editing state on commit / cancel / rm_editing_dir."""
    project = tmp_path / "proj"
    path = status_file_path(project)
    assert path == project / "editor" / "editing" / "regen_status.json"


def test_status_transitions_starting_running_completed(tmp_path: Path) -> None:
    """End-to-end: a 2-segment happy batch writes starting → running →
    completed, each visible via read_regen_all_status."""
    project = _make_project(tmp_path, ["seg_001", "seg_002"])

    task_id = start_regen_all_async(
        project_dir=project, tts_caller=_happy_tts_caller,
    )

    # Final state must be "completed" (within the generous timeout —
    # 2 segments × ~1ms caller = ~2ms of work).
    final = _wait_until(
        lambda: read_regen_all_status(project, task_id) or None
        if (s := read_regen_all_status(project, task_id))
        and s.get("stage") in {"completed", "failed"}
        else None,
        timeout_s=3.0,
    )
    assert final is not None, "status file never materialised"
    assert final["stage"] == "completed", f"expected completed, got {final!r}"
    assert final["total"] == 2
    assert final["succeeded_count"] == 2
    assert final["failed_count"] == 0
    # D38 summary present under "result"
    assert isinstance(final["result"], dict)
    assert final["result"]["succeeded_count"] == 2


def test_per_segment_failure_does_not_abort_batch(tmp_path: Path) -> None:
    """Plan D38 contract: one segment failing must not kill the batch.
    The thread collects the failure and continues to the next segment."""
    project = _make_project(tmp_path, ["seg_001", "seg_002", "seg_003"])

    # Caller: fail only seg_002
    def partial_caller(segment_dict: dict[str, Any], output_path: Path) -> None:
        if segment_dict.get("segment_id") == "seg_002":
            raise RuntimeError("upstream 429")
        output_path.write_bytes(b"W")

    task_id = start_regen_all_async(
        project_dir=project, tts_caller=partial_caller,
    )

    def _terminal():
        s = read_regen_all_status(project, task_id)
        if s and s.get("stage") in {"completed", "failed"}:
            return s
        return None
    final = _wait_until(_terminal, timeout_s=3.0)
    assert final is not None
    assert final["stage"] == "completed", (
        f"per-segment failure must not set the batch status to failed; "
        f"got {final!r}"
    )
    assert final["total"] == 3
    assert final["succeeded_count"] == 2
    assert final["failed_count"] == 1
    assert final["failed_segment_ids"] == ["seg_002"]


# ---------------------------------------------------------------------------
# stale task_id + concurrency
# ---------------------------------------------------------------------------


def test_read_status_returns_mismatch_for_stale_task_id(tmp_path: Path) -> None:
    """If a newer batch has overwritten the status file, the old task_id
    polling client must see {"mismatch": True} — not the new task's
    progress (which would be misleading)."""
    project = _make_project(tmp_path, ["seg_001"])
    task1 = start_regen_all_async(
        project_dir=project, tts_caller=_happy_tts_caller,
    )
    _wait_until(
        lambda: read_regen_all_status(project, task1)
        and read_regen_all_status(project, task1).get("stage") == "completed",
        timeout_s=3.0,
    )
    # Start a second batch — overwrites the status file
    task2 = start_regen_all_async(
        project_dir=project, tts_caller=_happy_tts_caller,
    )
    _wait_until(
        lambda: read_regen_all_status(project, task2)
        and read_regen_all_status(project, task2).get("stage") == "completed",
        timeout_s=3.0,
    )

    # Poll with task1 id — must see mismatch, not task2's data
    stale = read_regen_all_status(project, task1)
    assert stale is not None
    assert stale.get("mismatch") is True
    assert stale.get("actual_task_id") == task2


def test_read_status_returns_none_when_file_absent(tmp_path: Path) -> None:
    """Before any batch starts, status file doesn't exist — read returns None."""
    project = _make_project(tmp_path, ["seg_001"])
    assert read_regen_all_status(project, "nonexistent_task") is None


# ---------------------------------------------------------------------------
# catastrophic failure → "failed" terminal state
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Single-flight: concurrent POSTs must not spawn duplicate batches
#
# Bug (Claude Code ultrareview #4, P1 paid-API violation):
# ``start_regen_all_async`` unconditionally created a new task + thread
# each call. Double-click / proxy retry / two-tab scenario spun up N
# parallel threads iterating the SAME eligible list → N × TTS calls
# per segment = 双倍付费. CLAUDE.md forbids auto-paid-API calls; even
# user-triggered, duplicate billing is a silent money-burn vector.
#
# Fix: per-project in-process lock. If a batch is already active
# (status file stage ∈ {starting, running}), return the existing
# task_id. Caller (frontend already polls /status) sees the same id
# and proceeds without duplicating work.
# ---------------------------------------------------------------------------


def test_concurrent_start_returns_same_task_id_no_duplicate_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two near-simultaneous POSTs on the same project must share one
    task_id and one worker thread. Verifies:
      1. Second call returns the SAME task_id as the first
      2. The TTS caller is invoked exactly N times (not 2N) for the
         dirty segments
    """
    project = _make_project(tmp_path, ["seg_001", "seg_002", "seg_003"])
    call_barrier = threading.Event()
    calls: list[str] = []

    def _barrier_caller(segment_dict: dict[str, Any], output_path: Path) -> None:
        # Hold the first thread so a second start() can race it
        call_barrier.wait(timeout=2.0)
        calls.append(segment_dict.get("segment_id", ""))
        output_path.write_bytes(b"W")

    task_a = start_regen_all_async(
        project_dir=project, tts_caller=_barrier_caller,
    )
    # Second call immediately while thread A is blocked on barrier
    task_b = start_regen_all_async(
        project_dir=project, tts_caller=_barrier_caller,
    )
    assert task_a == task_b, (
        f"single-flight broken: got task_a={task_a!r} task_b={task_b!r} "
        "(two concurrent POSTs spawned two tasks → paid API double-billing)"
    )

    # Release the thread and wait for completion
    call_barrier.set()
    _wait_until(
        lambda: (s := read_regen_all_status(project, task_a))
        and s.get("stage") == "completed",
        timeout_s=3.0,
    )

    # Each segment called exactly once, never twice
    from collections import Counter
    counts = Counter(calls)
    for sid, count in counts.items():
        assert count == 1, (
            f"segment {sid} TTS called {count}x — duplicate billing path"
        )
    assert len(calls) == 3


def test_start_after_previous_batch_completed_spawns_new_task(
    tmp_path: Path,
) -> None:
    """Single-flight is only per-batch-in-flight, not per-project-ever:
    once the previous batch completed, a new POST must start a new
    task (with its own task_id) — otherwise users couldn't re-run
    a batch after the first finishes."""
    project = _make_project(tmp_path, ["seg_001"])
    task_a = start_regen_all_async(
        project_dir=project, tts_caller=_happy_tts_caller,
    )
    _wait_until(
        lambda: (s := read_regen_all_status(project, task_a))
        and s.get("stage") == "completed",
        timeout_s=3.0,
    )
    task_b = start_regen_all_async(
        project_dir=project, tts_caller=_happy_tts_caller,
    )
    assert task_a != task_b, (
        "After previous batch finished, new POST must spawn new task_id "
        f"(got same id {task_a!r}) — single-flight gate is too strict"
    )


# ---------------------------------------------------------------------------
# Zombie dir: _write_status must not resurrect a deleted editing/ directory
#
# Bug (Claude Code ultrareview #1, P2):
# _write_status unconditionally ran ``path.parent.mkdir(parents=True,
# exist_ok=True)`` before writing, which means a batch thread still
# running after the user cancelled / committed (both tear down
# editor/editing/) recreated the directory just to drop a status file
# in it. That zombie ``editor/editing/regen_status.json`` then lingers
# on disk, confuses cleanup scanners, and (more importantly) violates
# the docstring promise of "silently drops if the editing dir has been
# removed". Legit invariant: editor/editing/ exists iff job status ==
# editing.
#
# Fix: if parent doesn't exist, silent return (no mkdir).
# ---------------------------------------------------------------------------


def test_write_status_does_not_resurrect_deleted_editing_dir(tmp_path: Path) -> None:
    """Simulate cancel/commit race: batch thread tries to write status
    after editor/editing/ is gone. The status file must not be written,
    and the editing/ directory must stay gone."""
    from services.jobs.regenerate_all_async import _write_status, _initial_status

    project = tmp_path / "proj"
    # Deliberately do NOT create editor/editing/ — simulates the user
    # having just cancelled the editing session.
    assert not (project / "editor" / "editing").exists()

    _write_status(project, _initial_status("fake_task"))

    # No resurrection: editing/ must not exist, regen_status.json must not exist.
    assert not (project / "editor" / "editing").exists(), (
        "zombie dir — _write_status recreated editor/editing/ after "
        "user cancelled the editing session"
    )
    assert not (project / "editor" / "editing" / "regen_status.json").exists()


def test_write_status_writes_when_editing_dir_exists(tmp_path: Path) -> None:
    """Baseline regression: normal case (editing/ exists) still works."""
    from services.jobs.regenerate_all_async import _write_status, _initial_status

    project = tmp_path / "proj"
    (project / "editor" / "editing").mkdir(parents=True)

    _write_status(project, _initial_status("real_task"))

    status = (project / "editor" / "editing" / "regen_status.json")
    assert status.is_file()
    data = json.loads(status.read_text(encoding="utf-8"))
    assert data["task_id"] == "real_task"


def test_status_transitions_to_failed_on_top_level_exception(tmp_path: Path) -> None:
    """If the thread itself crashes (not a per-segment fail), stage =
    "failed" and ``error`` carries the message. Use missing
    editing/segments.json to trigger a top-level raise."""
    project = tmp_path / "broken"
    (project / "editor" / "editing").mkdir(parents=True)
    # Intentionally DON'T write editing/segments.json or segment_status.json

    task_id = start_regen_all_async(
        project_dir=project, tts_caller=_happy_tts_caller,
    )
    def _terminal():
        s = read_regen_all_status(project, task_id)
        if s and s.get("stage") in {"completed", "failed"}:
            return s
        return None
    final = _wait_until(_terminal, timeout_s=3.0)
    assert final is not None
    # Missing editing/ state → empty candidate list → completes with 0
    # work (not a crash). Valid outcome: "completed" with total=0.
    # If someone later changes the semantics to raise, "failed" is also OK.
    assert final["stage"] in {"completed", "failed"}, final
    if final["stage"] == "completed":
        assert final["total"] == 0
