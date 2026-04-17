"""Unit tests for src.services.jobs.video_render_async.

Exercises the threading + status-file lifecycle without actually running
ffmpeg — uses VideoRenderer with a stub command_runner via monkeypatch.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

_repo_root = Path(__file__).resolve().parent.parent
_src_dir = _repo_root / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))


from services.jobs.video_render_async import (  # noqa: E402
    new_render_task_id,
    read_status,
    start_render_thread,
    status_file_path,
)


def _make_project(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    project_dir = tmp_path / "proj"
    (project_dir / "publish").mkdir(parents=True)
    source_video = tmp_path / "src.mp4"
    source_video.write_bytes(b"fake mp4" * 50)
    dubbed_audio = tmp_path / "dub.wav"
    dubbed_audio.write_bytes(b"fake wav" * 50)
    manifest_path = project_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"artifact_index": {}}), encoding="utf-8")
    return project_dir, source_video, dubbed_audio, manifest_path


def _wait_for_stage(project_dir: Path, task_id: str, want_stage: str, timeout: float = 5.0) -> dict:
    """Poll the status file until it reaches ``want_stage`` or times out."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        status = read_status(project_dir, task_id)
        if status is not None:
            last = status
            if status.get("stage") == want_stage:
                return status
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for stage={want_stage}; last={last!r}")


def test_initial_status_is_written_synchronously(tmp_path, monkeypatch):
    # Make ffmpeg a no-op that "creates" the output file so render succeeds
    from modules.output.publish import video_renderer as vr
    def _fake_run(cmd):
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"x" * 10)  # non-empty
    monkeypatch.setattr(vr.VideoRenderer, "_run_command", staticmethod(_fake_run))

    project_dir, source_video, dubbed_audio, manifest = _make_project(tmp_path)
    task_id = new_render_task_id()

    start_render_thread(
        render_task_id=task_id,
        project_dir=project_dir,
        job_id="job_X",
        source_video=source_video,
        dubbed_audio=dubbed_audio,
        ambient_audio=None,
        manifest_path=manifest,
    )

    # Initial status file must exist before start_render_thread returns
    status = read_status(project_dir, task_id)
    assert status is not None
    assert status["render_task_id"] == task_id
    assert status["stage"] in ("starting", "muxing", "finalizing", "done")

    # Let the thread finish
    final = _wait_for_stage(project_dir, task_id, "done", timeout=3.0)
    assert final["percent"] == 100
    assert final["result"]["path"].endswith("dubbed_video.mp4")


def test_render_thread_updates_manifest(tmp_path, monkeypatch):
    from modules.output.publish import video_renderer as vr
    def _fake_run(cmd):
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"x" * 10)
    monkeypatch.setattr(vr.VideoRenderer, "_run_command", staticmethod(_fake_run))

    project_dir, source_video, dubbed_audio, manifest_path = _make_project(tmp_path)
    task_id = new_render_task_id()

    start_render_thread(
        render_task_id=task_id,
        project_dir=project_dir,
        job_id="job_X",
        source_video=source_video,
        dubbed_audio=dubbed_audio,
        ambient_audio=None,
        manifest_path=manifest_path,
    )
    _wait_for_stage(project_dir, task_id, "done", timeout=3.0)

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "publish.dubbed_video" in manifest_data["artifact_index"]
    assert manifest_data["artifact_index"]["publish.dubbed_video"].endswith("dubbed_video.mp4")


def test_render_thread_marks_failed_on_runner_error(tmp_path, monkeypatch):
    from modules.output.publish import video_renderer as vr
    import subprocess
    def _failing_run(cmd):
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd, stderr="ffmpeg oops")
    monkeypatch.setattr(vr.VideoRenderer, "_run_command", staticmethod(_failing_run))

    project_dir, source_video, dubbed_audio, manifest_path = _make_project(tmp_path)
    task_id = new_render_task_id()

    start_render_thread(
        render_task_id=task_id,
        project_dir=project_dir,
        job_id="job_X",
        source_video=source_video,
        dubbed_audio=dubbed_audio,
        ambient_audio=None,
        manifest_path=manifest_path,
    )

    failed = _wait_for_stage(project_dir, task_id, "failed", timeout=3.0)
    assert failed["error"]
    assert "失败" in failed["error"] or "Publish" in failed["error"]


def test_read_status_detects_task_id_mismatch(tmp_path, monkeypatch):
    from modules.output.publish import video_renderer as vr
    def _fake_run(cmd):
        Path(cmd[-1]).write_bytes(b"x" * 10)
    monkeypatch.setattr(vr.VideoRenderer, "_run_command", staticmethod(_fake_run))

    project_dir, source_video, dubbed_audio, manifest_path = _make_project(tmp_path)
    task_a = new_render_task_id()
    start_render_thread(
        render_task_id=task_a,
        project_dir=project_dir,
        job_id="job_X",
        source_video=source_video,
        dubbed_audio=dubbed_audio,
        ambient_audio=None,
        manifest_path=manifest_path,
    )
    _wait_for_stage(project_dir, task_a, "done", timeout=3.0)

    # Read with a different (stale) task_id
    stale = read_status(project_dir, "different-task-id")
    assert stale is not None
    assert stale.get("mismatch") is True
