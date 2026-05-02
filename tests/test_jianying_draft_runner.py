"""Tests for JianyingDraftRunner background worker (Task K3).

Covers 12 scenarios:
1.  trigger(idle) -> spawn thread, status=running, response has "running"
2.  trigger(running) -> no new thread, response says still in progress
3.  trigger(succeeded) -> no new thread, response includes zip path + artifact_key
4.  trigger(failed) -> clear error, spawn fresh thread, status=running
5.  trigger with service_mode != "studio" -> JianyingNotAllowedError(service_mode_not_studio)
6.  trigger with job.status != "succeeded" -> JianyingNotAllowedError(job_not_succeeded)
7.  trigger with unknown job_id -> KeyError
8.  background success path (validation_status=ok) -> status=succeeded, zip_path set
9.  background failure path (validation_status=failed) -> status=failed, error set
10. background exception path (backend raises RuntimeError) -> status=failed, error set
11. reap_stale marks old running as failed; fresh running untouched; succeeded untouched
12. get_status returns current state fields

Plan: docs/plans/2026-05-02-jianying-draft-delivery-integration-plan.md §11.6 (K3)
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from services.jobs.jianying_draft_runner import (
    JianyingDraftRunner,
    JianyingNotAllowedError,
)
from services.jobs.models import JobRecord
from services.jobs.store import JobStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job_dict(**overrides) -> dict:
    """Return a minimal valid JobRecord dict for tests."""
    base = {
        "job_id": "job-test-001",
        "job_type": "localize_video",
        "source_type": "youtube_url",
        "source_ref": "https://youtube.com/watch?v=test",
        "output_target": "editor",
        "speakers": "auto",
        "status": "succeeded",
        "service_mode": "studio",
        "created_at": "2026-05-02T00:00:00Z",
        "updated_at": "2026-05-02T00:00:00Z",
    }
    base.update(overrides)
    return base


def _make_record(**overrides) -> JobRecord:
    return JobRecord.from_dict(_make_job_dict(**overrides))


def _make_store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs")


def _make_runner(store: JobStore, backend=None) -> JianyingDraftRunner:
    return JianyingDraftRunner(store=store, backend=backend)


def _make_ok_result(zip_path: str = "/tmp/draft.zip") -> mock.MagicMock:
    result = mock.MagicMock()
    result.validation_status = "ok"
    result.draft_zip_path = zip_path
    result.compatibility_report_path = "/tmp/report.json"
    return result


def _make_fail_result(status: str = "failed") -> mock.MagicMock:
    result = mock.MagicMock()
    result.validation_status = status
    result.draft_zip_path = ""
    result.compatibility_report_path = "/tmp/report.json"
    return result


def _make_project_dir(tmp_path: Path, job_id: str = "job-test-001") -> Path:
    """Create a minimal project dir with manifest.json so _build_jianying_request succeeds."""
    project_dir = tmp_path / "project" / job_id
    project_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "artifact_index": {
            "source.original_video": str(project_dir / "source.mp4"),
            "editor.dubbed_audio_complete": str(project_dir / "dubbed.wav"),
            "editor.subtitles": str(project_dir / "subtitles.srt"),
        }
    }
    (project_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return project_dir


def _wait_for_jianying_status(store: JobStore, job_id: str, expected: str, timeout: float = 5.0) -> JobRecord:
    """Poll store until jianying_draft_status matches expected or timeout."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.require_job(job_id)
        if job.jianying_draft_status == expected:
            return job
        time.sleep(0.05)
    raise TimeoutError(
        f"jianying_draft_status did not become {expected!r} within {timeout}s; "
        f"last value: {store.require_job(job_id).jianying_draft_status!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 1: trigger(idle) starts thread, returns running
# ---------------------------------------------------------------------------


class TestTriggerFromIdle:
    def test_trigger_idle_returns_running_and_spawns_thread(self, tmp_path):
        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        record = _make_record(project_dir=str(project_dir))
        store.save_job(record)

        # Backend that blocks until we release it, so we can inspect the thread
        event_enter = threading.Event()
        event_release = threading.Event()

        def slow_write(request):
            event_enter.set()
            event_release.wait(timeout=5)
            return _make_ok_result()

        backend = mock.MagicMock()
        backend.write.side_effect = slow_write

        runner = _make_runner(store, backend)
        response = runner.trigger("job-test-001")

        # Response indicates running
        assert response["status"] == "running"
        assert "started_at" in response

        # JobRecord persisted as running
        persisted = store.require_job("job-test-001")
        assert persisted.jianying_draft_status == "running"
        assert persisted.jianying_draft_started_at is not None

        # Thread entered the backend
        entered = event_enter.wait(timeout=5)
        assert entered, "Background thread did not start within 5s"

        # Release thread to finish cleanly
        event_release.set()
        _wait_for_jianying_status(store, "job-test-001", "succeeded")


# ---------------------------------------------------------------------------
# Scenario 2: trigger(running) returns running without starting a new thread
# ---------------------------------------------------------------------------


class TestTriggerWhenRunning:
    def test_trigger_running_returns_still_in_progress(self, tmp_path):
        store = _make_store(tmp_path)
        record = _make_record(
            jianying_draft_status="running",
            jianying_draft_started_at="2026-05-02T10:00:00Z",
        )
        store.save_job(record)

        backend = mock.MagicMock()
        runner = _make_runner(store, backend)

        response = runner.trigger("job-test-001")

        assert response["status"] == "running"
        assert response.get("message") == "still in progress"
        assert response.get("started_at") == "2026-05-02T10:00:00Z"

        # Backend must NOT be called (no new thread)
        backend.write.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 3: trigger(succeeded) returns succeeded with existing path
# ---------------------------------------------------------------------------


class TestTriggerWhenSucceeded:
    def test_trigger_succeeded_returns_existing_artifact(self, tmp_path):
        store = _make_store(tmp_path)
        record = _make_record(
            jianying_draft_status="succeeded",
            jianying_draft_completed_at="2026-05-02T11:00:00Z",
            jianying_draft_zip_path="/some/path/draft.zip",
        )
        store.save_job(record)

        backend = mock.MagicMock()
        runner = _make_runner(store, backend)

        response = runner.trigger("job-test-001")

        assert response["status"] == "succeeded"
        assert response["draft_zip_path"] == "/some/path/draft.zip"
        assert response["artifact_key"] == "editor.jianying_draft_zip"
        assert response["completed_at"] == "2026-05-02T11:00:00Z"

        # No re-run
        backend.write.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 4: trigger(failed) clears error and starts new thread
# ---------------------------------------------------------------------------


class TestTriggerWhenFailed:
    def test_trigger_failed_clears_error_and_starts_thread(self, tmp_path):
        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        record = _make_record(
            project_dir=str(project_dir),
            jianying_draft_status="failed",
            jianying_draft_error="previous error message",
            jianying_draft_started_at="2026-05-02T09:00:00Z",
        )
        store.save_job(record)

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result()

        runner = _make_runner(store, backend)
        response = runner.trigger("job-test-001")

        assert response["status"] == "running"

        # Persisted state immediately after trigger
        persisted = store.require_job("job-test-001")
        assert persisted.jianying_draft_status == "running"
        assert persisted.jianying_draft_error is None

        # Wait for thread to finish
        _wait_for_jianying_status(store, "job-test-001", "succeeded")
        backend.write.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 5: trigger with service_mode != "studio" raises JianyingNotAllowedError
# ---------------------------------------------------------------------------


class TestTriggerServiceModeNotStudio:
    def test_trigger_express_job_raises(self, tmp_path):
        store = _make_store(tmp_path)
        record = _make_record(service_mode="express")
        store.save_job(record)

        runner = _make_runner(store)

        with pytest.raises(JianyingNotAllowedError) as exc_info:
            runner.trigger("job-test-001")

        assert exc_info.value.reason == "service_mode_not_studio"

    def test_trigger_none_service_mode_raises(self, tmp_path):
        store = _make_store(tmp_path)
        record = _make_record(service_mode=None)
        store.save_job(record)

        runner = _make_runner(store)

        with pytest.raises(JianyingNotAllowedError) as exc_info:
            runner.trigger("job-test-001")

        assert exc_info.value.reason == "service_mode_not_studio"


# ---------------------------------------------------------------------------
# Scenario 6: trigger with job.status != "succeeded" raises JianyingNotAllowedError
# ---------------------------------------------------------------------------


class TestTriggerJobNotSucceeded:
    @pytest.mark.parametrize("job_status", ["running", "failed", "queued", "editing"])
    def test_trigger_non_succeeded_job_raises(self, tmp_path, job_status):
        store = _make_store(tmp_path)
        record = _make_record(status=job_status)
        store.save_job(record)

        runner = _make_runner(store)

        with pytest.raises(JianyingNotAllowedError) as exc_info:
            runner.trigger("job-test-001")

        assert exc_info.value.reason == "job_not_succeeded"


# ---------------------------------------------------------------------------
# Scenario 7: trigger with unknown job_id raises KeyError
# ---------------------------------------------------------------------------


class TestTriggerJobNotFound:
    def test_trigger_unknown_job_id_raises_key_error(self, tmp_path):
        store = _make_store(tmp_path)
        runner = _make_runner(store)

        with pytest.raises(KeyError):
            runner.trigger("nonexistent-job-id")


# ---------------------------------------------------------------------------
# Scenario 8: background success path -> status=succeeded + zip_path set
# ---------------------------------------------------------------------------


class TestBackgroundSuccessPath:
    def test_background_ok_result_sets_succeeded(self, tmp_path):
        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        record = _make_record(project_dir=str(project_dir))
        store.save_job(record)

        ok_result = _make_ok_result(zip_path="/project/jianying/draft.zip")
        backend = mock.MagicMock()
        backend.write.return_value = ok_result

        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")

        finished = _wait_for_jianying_status(store, "job-test-001", "succeeded")

        assert finished.jianying_draft_status == "succeeded"
        assert finished.jianying_draft_zip_path == "/project/jianying/draft.zip"
        assert finished.jianying_draft_completed_at is not None
        assert finished.jianying_draft_error is None


# ---------------------------------------------------------------------------
# Scenario 9: background failure path (non-ok validation_status) -> status=failed
# ---------------------------------------------------------------------------


class TestBackgroundFailurePath:
    @pytest.mark.parametrize(
        "validation_status",
        ["failed", "skipped_no_engine", "skipped_missing_input"],
    )
    def test_background_non_ok_result_sets_failed(self, tmp_path, validation_status):
        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        record = _make_record(project_dir=str(project_dir))
        store.save_job(record)

        fail_result = _make_fail_result(status=validation_status)
        backend = mock.MagicMock()
        backend.write.return_value = fail_result

        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")

        finished = _wait_for_jianying_status(store, "job-test-001", "failed")

        assert finished.jianying_draft_status == "failed"
        assert finished.jianying_draft_error is not None
        assert validation_status in finished.jianying_draft_error
        assert finished.jianying_draft_completed_at is not None


# ---------------------------------------------------------------------------
# Scenario 10: background exception path -> status=failed + error set
# ---------------------------------------------------------------------------


class TestBackgroundExceptionPath:
    def test_background_exception_sets_failed_with_error_detail(self, tmp_path):
        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        record = _make_record(project_dir=str(project_dir))
        store.save_job(record)

        backend = mock.MagicMock()
        backend.write.side_effect = RuntimeError("something exploded")

        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")

        finished = _wait_for_jianying_status(store, "job-test-001", "failed")

        assert finished.jianying_draft_status == "failed"
        assert "RuntimeError" in finished.jianying_draft_error
        assert "something exploded" in finished.jianying_draft_error
        assert finished.jianying_draft_completed_at is not None


# ---------------------------------------------------------------------------
# Scenario 11: reap_stale marks old running as failed; fresh and succeeded untouched
# ---------------------------------------------------------------------------


class TestReapStale:
    def test_reap_stale_marks_old_running_only(self, tmp_path):
        store = _make_store(tmp_path)
        now = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)

        # Job 1: running, started 60 min ago (stale — should be reaped)
        stale_time = (now - timedelta(minutes=60)).isoformat()
        job_stale = _make_record(job_id="job-stale", jianying_draft_status="running",
                                  jianying_draft_started_at=stale_time)
        store.save_job(job_stale)

        # Job 2: running, started 10 min ago (fresh — should NOT be reaped)
        fresh_time = (now - timedelta(minutes=10)).isoformat()
        job_fresh = _make_record(job_id="job-fresh", jianying_draft_status="running",
                                  jianying_draft_started_at=fresh_time)
        store.save_job(job_fresh)

        # Job 3: succeeded — should NOT be touched
        job_ok = _make_record(job_id="job-ok", jianying_draft_status="succeeded",
                               jianying_draft_completed_at="2026-05-02T10:00:00Z")
        store.save_job(job_ok)

        runner = _make_runner(store)
        count = runner.reap_stale(now=now)

        assert count == 1, f"Expected 1 reaped, got {count}"

        # Stale job marked failed
        reaped = store.require_job("job-stale")
        assert reaped.jianying_draft_status == "failed"
        assert "stale" in (reaped.jianying_draft_error or "").lower()
        assert reaped.jianying_draft_completed_at is not None

        # Fresh job untouched
        fresh_after = store.require_job("job-fresh")
        assert fresh_after.jianying_draft_status == "running"

        # Succeeded job untouched
        ok_after = store.require_job("job-ok")
        assert ok_after.jianying_draft_status == "succeeded"

    def test_reap_stale_no_running_returns_zero(self, tmp_path):
        store = _make_store(tmp_path)
        now = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)

        job = _make_record(jianying_draft_status="idle")
        store.save_job(job)

        runner = _make_runner(store)
        count = runner.reap_stale(now=now)

        assert count == 0

    def test_reap_stale_skips_corrupt_timestamp(self, tmp_path):
        store = _make_store(tmp_path)
        now = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)

        job = _make_record(jianying_draft_status="running",
                            jianying_draft_started_at="NOT_A_TIMESTAMP")
        store.save_job(job)

        runner = _make_runner(store)
        # Must not raise; corrupt timestamp is skipped
        count = runner.reap_stale(now=now)
        assert count == 0


# ---------------------------------------------------------------------------
# Scenario 12: get_status returns current state
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_get_status_idle(self, tmp_path):
        store = _make_store(tmp_path)
        record = _make_record()
        store.save_job(record)

        runner = _make_runner(store)
        status = runner.get_status("job-test-001")

        assert status["status"] == "idle"
        assert status["started_at"] is None
        assert status["completed_at"] is None
        assert status["error"] is None
        assert status["artifact_key"] is None

    def test_get_status_running(self, tmp_path):
        store = _make_store(tmp_path)
        record = _make_record(
            jianying_draft_status="running",
            jianying_draft_started_at="2026-05-02T10:00:00Z",
        )
        store.save_job(record)

        runner = _make_runner(store)
        status = runner.get_status("job-test-001")

        assert status["status"] == "running"
        assert status["started_at"] == "2026-05-02T10:00:00Z"
        assert status["artifact_key"] is None

    def test_get_status_succeeded(self, tmp_path):
        store = _make_store(tmp_path)
        record = _make_record(
            jianying_draft_status="succeeded",
            jianying_draft_zip_path="/path/to/draft.zip",
            jianying_draft_completed_at="2026-05-02T11:00:00Z",
        )
        store.save_job(record)

        runner = _make_runner(store)
        status = runner.get_status("job-test-001")

        assert status["status"] == "succeeded"
        assert status["artifact_key"] == "editor.jianying_draft_zip"
        assert status["draft_zip_path"] == "/path/to/draft.zip"
        assert status["completed_at"] == "2026-05-02T11:00:00Z"

    def test_get_status_failed(self, tmp_path):
        store = _make_store(tmp_path)
        record = _make_record(
            jianying_draft_status="failed",
            jianying_draft_error="something went wrong",
        )
        store.save_job(record)

        runner = _make_runner(store)
        status = runner.get_status("job-test-001")

        assert status["status"] == "failed"
        assert status["error"] == "something went wrong"
        assert status["artifact_key"] is None

    def test_get_status_unknown_job_raises(self, tmp_path):
        store = _make_store(tmp_path)
        runner = _make_runner(store)

        with pytest.raises(KeyError):
            runner.get_status("nonexistent-job")


# ---------------------------------------------------------------------------
# Additional: JianyingNotAllowedError carries reason field
# ---------------------------------------------------------------------------


class TestJianyingNotAllowedError:
    def test_reason_field_accessible(self):
        err = JianyingNotAllowedError("service_mode_not_studio", "custom message")
        assert err.reason == "service_mode_not_studio"
        assert "custom message" in str(err)

    def test_reason_as_message_when_no_message(self):
        err = JianyingNotAllowedError("job_not_found")
        assert err.reason == "job_not_found"
        assert str(err) == "job_not_found"


# ---------------------------------------------------------------------------
# Additional: Build jianying request from manifest.json
# ---------------------------------------------------------------------------


class TestBuildJianyingRequest:
    def test_build_request_reads_manifest(self, tmp_path):
        """Runner builds JianyingDraftRequest from manifest.json artifact_index."""
        store = _make_store(tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Write a minimal manifest.json
        manifest = {
            "artifact_index": {
                "source.original_video": str(project_dir / "source.mp4"),
                "editor.dubbed_audio_complete": str(project_dir / "dubbed.wav"),
                "editor.subtitles": str(project_dir / "subtitles.srt"),
                "editor.ambient_audio": str(project_dir / "ambient.wav"),
            }
        }
        (project_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        record = _make_record(
            project_dir=str(project_dir),
            display_name="My Test Video",
        )
        store.save_job(record)

        # Use a real runner but intercept the backend
        captured_request = {}

        def capture_write(request):
            captured_request["req"] = request
            return _make_ok_result()

        backend = mock.MagicMock()
        backend.write.side_effect = capture_write

        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")

        _wait_for_jianying_status(store, "job-test-001", "succeeded")

        req = captured_request["req"]
        assert req.project_id == "job-test-001"
        assert req.project_title == "My Test Video"
        assert str(project_dir / "source.mp4") in req.source_video_path
        assert req.ambient_audio_path is not None

    def test_build_request_uses_job_id_as_title_fallback(self, tmp_path):
        """When display_name is None, project_title defaults to job_id."""
        store = _make_store(tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        manifest = {"artifact_index": {}}
        (project_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        record = _make_record(project_dir=str(project_dir), display_name=None)
        store.save_job(record)

        captured_request = {}

        def capture_write(request):
            captured_request["req"] = request
            return _make_ok_result()

        backend = mock.MagicMock()
        backend.write.side_effect = capture_write

        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")

        _wait_for_jianying_status(store, "job-test-001", "succeeded")

        req = captured_request["req"]
        assert req.project_title == "job-test-001"

    def test_build_request_no_project_dir_causes_failure(self, tmp_path):
        """Missing project_dir causes background thread to mark job as failed."""
        store = _make_store(tmp_path)
        record = _make_record(project_dir=None)
        store.save_job(record)

        backend = mock.MagicMock()
        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")

        finished = _wait_for_jianying_status(store, "job-test-001", "failed")
        assert finished.jianying_draft_error is not None
        assert backend.write.call_count == 0  # never reached the backend


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
