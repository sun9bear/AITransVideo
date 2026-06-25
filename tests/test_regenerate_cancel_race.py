"""TU-01 / H3 (ASYNC-06): a cancel must not be clobbered by a running write.

The batch worker writes a "current segment" progress snapshot each iteration.
That snapshot used to hardcode ``cancel_requested=False``; a cancel landing in
the status file between the worker's last check and this write would be reset to
False and silently lost. ``_write_running_status`` now preserves an in-flight
cancel for every running-phase write.

Coverage:
- unit: ``_write_running_status`` preserves a concurrent cancel (the core fix).
- unit: it still records ``cancel_requested=False`` when none is set (schema).
- integration: a cancel raised *while a segment is being processed* stops the
  batch before the next segment — this drives the real ``_run_batch`` loop and
  its per-segment write call-site, so a future write that bypasses the helper
  (or hardcodes ``cancel_requested=False``) would be caught here.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from services.jobs.editing_segments import (
    SEGMENT_STATUS_TEXT_DIRTY,
    mark_segment_status,
)
from services.jobs.regenerate_all_async import (
    _initial_status,
    _read_status_raw,
    _write_running_status,
    read_regen_all_status,
    request_regen_all_cancel,
    start_regen_all_async,
    status_file_path,
)


def _seed_status(tmp_path, task_id, **overrides):
    path = status_file_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    status = {**_initial_status(task_id), "task_id": task_id}
    status.update(overrides)
    path.write_text(json.dumps(status), encoding="utf-8")
    return path


def test_running_write_preserves_concurrent_cancel(tmp_path):
    """Core H3 fix: a cancel already in the status file survives a running write."""
    task_id = "t1"
    _seed_status(tmp_path, task_id, cancel_requested=True)

    _write_running_status(
        tmp_path,
        task_id,
        {"task_id": task_id, "stage": "running", "current_segment_id": "seg_2"},
    )

    saved = _read_status_raw(tmp_path, task_id)
    assert saved is not None
    assert saved["cancel_requested"] is True  # the cancel survived the write
    assert saved["current_segment_id"] == "seg_2"  # progress still recorded


def test_running_write_keeps_cancel_false_in_schema_when_not_set(tmp_path):
    """Schema only: with no prior cancel the field is still written as bool False
    (this does NOT lock the regression — see the integration test for that)."""
    task_id = "t2"
    _seed_status(tmp_path, task_id, cancel_requested=False)

    _write_running_status(
        tmp_path,
        task_id,
        {"task_id": task_id, "stage": "running", "current_segment_id": "seg_1"},
    )

    saved = _read_status_raw(tmp_path, task_id)
    assert saved is not None
    assert saved["cancel_requested"] is False


def _make_project(tmp_path: Path, segment_ids: list[str]) -> Path:
    project = tmp_path / "project"
    editing = project / "editor" / "editing"
    editing.mkdir(parents=True, exist_ok=True)
    (editing / "tts_segments_draft").mkdir(parents=True, exist_ok=True)
    (editing / "segments.json").write_text(
        json.dumps([
            {
                "segment_id": sid, "speaker_id": "a", "display_name": "A",
                "voice_id": "v1", "start_ms": 0, "end_ms": 1000,
                "target_duration_ms": 1000, "source_text": "x", "cn_text": "x",
            }
            for sid in segment_ids
        ]),
        encoding="utf-8",
    )
    for sid in segment_ids:
        mark_segment_status(project, sid, SEGMENT_STATUS_TEXT_DIRTY)
    return project


def _wait_terminal(project, task_id, timeout_s=4.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        st = read_regen_all_status(project, task_id)
        if st and st.get("stage") in {"completed", "cancelled", "failed"}:
            return st
        time.sleep(0.02)
    return read_regen_all_status(project, task_id)


def test_run_batch_honors_cancel_raised_during_a_segment(tmp_path):
    """Integration: a cancel that lands while s1 is processed must stop the
    batch before s2. Drives the real _run_batch loop + its per-segment write
    call-site — a write that clobbered cancel_requested back to False would let
    s2 run and fail this test."""
    project = _make_project(tmp_path, ["s1", "s2"])
    calls: list[str] = []

    def tts_caller(segment_dict, output_path):
        sid = segment_dict["segment_id"]
        calls.append(sid)
        output_path.write_bytes(b"W")
        if sid == "s1":
            # Simulate a cancel arriving mid-batch (read task_id from the
            # status file the worker already wrote, to avoid a closure race).
            raw = json.loads(status_file_path(project).read_text(encoding="utf-8"))
            assert request_regen_all_cancel(project, raw["task_id"]) is True

    task_id = start_regen_all_async(
        project_dir=project, tts_caller=tts_caller, default_tts_model="m",
    )
    final = _wait_terminal(project, task_id)

    assert final is not None
    assert final["stage"] == "cancelled"
    assert final.get("cancel_requested") is True
    assert calls == ["s1"]  # cancel during s1 prevented s2 from being processed
