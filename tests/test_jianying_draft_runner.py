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
    JianyingInvalidDraftRoot,
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


# ---------------------------------------------------------------------------
# K11: user_draft_root plumbing through trigger / background
# ---------------------------------------------------------------------------


class TestUserDraftRootPlumbing:
    """K11: user_draft_root kwarg passes through trigger → background → request."""

    def test_trigger_with_user_draft_root_passes_through_to_request(self, tmp_path):
        """trigger(user_draft_root=...) is forwarded to JianyingDraftRequest."""
        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        record = _make_record(project_dir=str(project_dir))
        store.save_job(record)

        captured_request = {}

        def capture_write(request):
            captured_request["req"] = request
            return _make_ok_result()

        backend = mock.MagicMock()
        backend.write.side_effect = capture_write

        runner = _make_runner(store, backend)
        win_root = r"F:\剪映缓存\草稿\JianyingPro Drafts"
        runner.trigger("job-test-001", user_draft_root=win_root)

        _wait_for_jianying_status(store, "job-test-001", "succeeded")

        req = captured_request["req"]
        assert req.user_draft_root == win_root

    def test_trigger_with_no_user_draft_root_passes_none_to_request(self, tmp_path):
        """trigger() without user_draft_root passes None to JianyingDraftRequest."""
        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        record = _make_record(project_dir=str(project_dir))
        store.save_job(record)

        captured_request = {}

        def capture_write(request):
            captured_request["req"] = request
            return _make_ok_result()

        backend = mock.MagicMock()
        backend.write.side_effect = capture_write

        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")  # no user_draft_root

        _wait_for_jianying_status(store, "job-test-001", "succeeded")

        req = captured_request["req"]
        assert req.user_draft_root is None

    def test_user_draft_root_persisted_on_success(self, tmp_path):
        """On success, jianying_draft_user_root is saved to JobRecord (K11)."""
        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        record = _make_record(project_dir=str(project_dir))
        store.save_job(record)

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result(zip_path="/tmp/draft.zip")

        runner = _make_runner(store, backend)
        win_root = r"F:\JianyingPro Drafts"
        runner.trigger("job-test-001", user_draft_root=win_root)

        finished = _wait_for_jianying_status(store, "job-test-001", "succeeded")

        assert finished.jianying_draft_user_root == win_root


class TestUserDraftRootValidation:
    """K11: user_draft_root validation raises JianyingInvalidDraftRoot."""

    def test_empty_string_raises_invalid_draft_root(self, tmp_path):
        """trigger(user_draft_root='') raises JianyingInvalidDraftRoot (K11)."""
        store = _make_store(tmp_path)
        record = _make_record()
        store.save_job(record)

        runner = _make_runner(store)

        with pytest.raises(JianyingInvalidDraftRoot):
            runner.trigger("job-test-001", user_draft_root="")

    def test_whitespace_only_raises_invalid_draft_root(self, tmp_path):
        """trigger(user_draft_root='   ') raises JianyingInvalidDraftRoot (K11)."""
        store = _make_store(tmp_path)
        record = _make_record()
        store.save_job(record)

        runner = _make_runner(store)

        with pytest.raises(JianyingInvalidDraftRoot):
            runner.trigger("job-test-001", user_draft_root="   ")

    def test_url_scheme_raises_invalid_draft_root(self, tmp_path):
        """trigger(user_draft_root='https://...') raises JianyingInvalidDraftRoot (K11)."""
        store = _make_store(tmp_path)
        record = _make_record()
        store.save_job(record)

        runner = _make_runner(store)

        with pytest.raises(JianyingInvalidDraftRoot):
            runner.trigger("job-test-001", user_draft_root="https://example.com/drafts")

    def test_null_byte_raises_invalid_draft_root(self, tmp_path):
        """trigger(user_draft_root containing \\0) raises JianyingInvalidDraftRoot (K11)."""
        store = _make_store(tmp_path)
        record = _make_record()
        store.save_job(record)

        runner = _make_runner(store)

        with pytest.raises(JianyingInvalidDraftRoot):
            runner.trigger("job-test-001", user_draft_root="F:\\Drafts\x00bad")

    def test_valid_windows_path_accepted(self, tmp_path):
        """trigger(user_draft_root='F:\\...') does not raise JianyingInvalidDraftRoot (K11)."""
        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        record = _make_record(project_dir=str(project_dir))
        store.save_job(record)

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result()
        runner = _make_runner(store, backend)

        # Must not raise
        response = runner.trigger("job-test-001", user_draft_root=r"F:\JianyingPro Drafts")
        assert response["status"] == "running"
        _wait_for_jianying_status(store, "job-test-001", "succeeded")

    def test_none_user_draft_root_skips_validation(self, tmp_path):
        """trigger(user_draft_root=None) skips validation entirely (K11)."""
        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        record = _make_record(project_dir=str(project_dir))
        store.save_job(record)

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result()
        runner = _make_runner(store, backend)

        # Must not raise
        response = runner.trigger("job-test-001", user_draft_root=None)
        assert response["status"] == "running"
        _wait_for_jianying_status(store, "job-test-001", "succeeded")


class TestIdempotencyWithUserDraftRoot:
    """K11: idempotency behavior when user_draft_root changes."""

    def test_succeeded_same_root_returns_cached(self, tmp_path):
        """trigger(succeeded, same user_draft_root) returns cached artifact, no re-run."""
        store = _make_store(tmp_path)
        win_root = r"F:\JianyingPro Drafts"
        record = _make_record(
            jianying_draft_status="succeeded",
            jianying_draft_completed_at="2026-05-02T11:00:00Z",
            jianying_draft_zip_path="/some/path/draft.zip",
            jianying_draft_user_root=win_root,
        )
        store.save_job(record)

        backend = mock.MagicMock()
        runner = _make_runner(store, backend)

        response = runner.trigger("job-test-001", user_draft_root=win_root)

        assert response["status"] == "succeeded"
        assert response.get("_idempotent") is True
        assert response["draft_zip_path"] == "/some/path/draft.zip"
        backend.write.assert_not_called()

    def test_succeeded_different_root_regenerates(self, tmp_path):
        """trigger(succeeded, different user_draft_root) falls through and regenerates."""
        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        old_root = r"F:\OldDrafts"
        new_root = r"G:\NewDrafts"

        record = _make_record(
            project_dir=str(project_dir),
            jianying_draft_status="succeeded",
            jianying_draft_completed_at="2026-05-02T11:00:00Z",
            jianying_draft_zip_path="/old/draft.zip",
            jianying_draft_user_root=old_root,
        )
        store.save_job(record)

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result(zip_path="/new/draft.zip")

        runner = _make_runner(store, backend)
        response = runner.trigger("job-test-001", user_draft_root=new_root)

        # Should have triggered a new run
        assert response["status"] == "running"
        assert response.get("_idempotent") is None

        # Wait for the background thread to complete
        finished = _wait_for_jianying_status(store, "job-test-001", "succeeded")
        assert finished.jianying_draft_user_root == new_root
        backend.write.assert_called_once()

    def test_succeeded_no_user_draft_root_returns_cached(self, tmp_path):
        """trigger(succeeded, no user_draft_root) returns cached even if cached_root is set."""
        store = _make_store(tmp_path)
        win_root = r"F:\JianyingPro Drafts"
        record = _make_record(
            jianying_draft_status="succeeded",
            jianying_draft_completed_at="2026-05-02T11:00:00Z",
            jianying_draft_zip_path="/some/path/draft.zip",
            jianying_draft_user_root=win_root,
        )
        store.save_job(record)

        backend = mock.MagicMock()
        runner = _make_runner(store, backend)

        # No user_draft_root in call — falsy → treated as "don't compare, use cached"
        response = runner.trigger("job-test-001")

        assert response["status"] == "succeeded"
        assert response.get("_idempotent") is True
        backend.write.assert_not_called()

    def test_succeeded_no_cached_root_and_no_new_root_returns_cached(self, tmp_path):
        """trigger(succeeded, both None) returns cached artifact (K11)."""
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
        assert response.get("_idempotent") is True
        backend.write.assert_not_called()


# ===========================================================================
# Phase A hardening (plan 2026-05-03 §A): fingerprint, file_lock, sub-step,
# orphan recovery. Each class below exercises one hardening invariant;
# scenarios mirror plan §A10's test plan.
# ===========================================================================


def _make_real_inputs(project_dir: Path) -> dict:
    """Create real artifact files referenced by manifest.json so
    fingerprint computation hashes actual content."""
    source = project_dir / "source.mp4"
    dubbed = project_dir / "dubbed.wav"
    subs = project_dir / "subtitles.srt"
    source.write_bytes(b"fake-video-bytes-001")
    dubbed.write_bytes(b"fake-audio-bytes-001")
    subs.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
    manifest = {
        "artifact_index": {
            "source.original_video": str(source),
            "editor.dubbed_audio_complete": str(dubbed),
            "editor.subtitles": str(subs),
        }
    }
    (project_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return {"source": source, "dubbed": dubbed, "subs": subs}


class TestFingerprintIdempotency:
    """Plan §A10.1–A10.3: cache hit when fingerprint + zip + root all match;
    miss when zip gone, root differs, or content changed."""

    def test_succeeded_same_fingerprint_and_zip_returns_cached(self, tmp_path):
        from services.jobs.jianying_draft_runner import (
            _compute_jianying_fingerprint,
        )

        store = _make_store(tmp_path)
        project_dir = tmp_path / "project" / "job-test-001"
        project_dir.mkdir(parents=True)
        _make_real_inputs(project_dir)

        zip_path = tmp_path / "draft.zip"
        zip_path.write_bytes(b"existing-zip")

        # Persist record with fingerprint matching current inputs
        record_seed = _make_record(project_dir=str(project_dir))
        fingerprint = _compute_jianying_fingerprint(record_seed, None)
        assert fingerprint, "fingerprint helper must succeed for real inputs"

        record = _make_record(
            project_dir=str(project_dir),
            jianying_draft_status="succeeded",
            jianying_draft_completed_at="2026-05-03T11:00:00Z",
            jianying_draft_zip_path=str(zip_path),
            jianying_draft_fingerprint=fingerprint,
        )
        store.save_job(record)

        backend = mock.MagicMock()
        runner = _make_runner(store, backend)
        response = runner.trigger("job-test-001")

        assert response["status"] == "succeeded"
        assert response.get("_idempotent") is True
        assert response["fingerprint"] == fingerprint
        backend.write.assert_not_called()

    def test_succeeded_fingerprint_match_but_zip_missing_regenerates(self, tmp_path):
        from services.jobs.jianying_draft_runner import (
            _compute_jianying_fingerprint,
        )

        store = _make_store(tmp_path)
        project_dir = tmp_path / "project" / "job-test-001"
        project_dir.mkdir(parents=True)
        _make_real_inputs(project_dir)

        seed = _make_record(project_dir=str(project_dir))
        fingerprint = _compute_jianying_fingerprint(seed, None)

        record = _make_record(
            project_dir=str(project_dir),
            jianying_draft_status="succeeded",
            jianying_draft_completed_at="2026-05-03T11:00:00Z",
            jianying_draft_zip_path="/does/not/exist.zip",  # gone
            jianying_draft_fingerprint=fingerprint,
        )
        store.save_job(record)

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result()
        runner = _make_runner(store, backend)
        response = runner.trigger("job-test-001")

        assert response["status"] == "running"
        _wait_for_jianying_status(store, "job-test-001", "succeeded")
        backend.write.assert_called_once()

    def test_succeeded_different_fingerprint_regenerates(self, tmp_path):
        store = _make_store(tmp_path)
        project_dir = tmp_path / "project" / "job-test-001"
        project_dir.mkdir(parents=True)
        _make_real_inputs(project_dir)

        zip_path = tmp_path / "draft.zip"
        zip_path.write_bytes(b"existing-zip")

        record = _make_record(
            project_dir=str(project_dir),
            jianying_draft_status="succeeded",
            jianying_draft_completed_at="2026-05-03T11:00:00Z",
            jianying_draft_zip_path=str(zip_path),
            jianying_draft_fingerprint="fingerprint_from_old_input",  # stale
        )
        store.save_job(record)

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result(zip_path=str(zip_path))
        runner = _make_runner(store, backend)
        response = runner.trigger("job-test-001")

        assert response["status"] == "running"
        _wait_for_jianying_status(store, "job-test-001", "succeeded")
        backend.write.assert_called_once()

    def test_legacy_succeeded_no_fingerprint_returns_cached(self, tmp_path):
        """Backward compat: pre-Phase-A succeeded jobs (fingerprint=None) keep
        the historical 'trust state' behavior — no re-hash, no re-run."""
        store = _make_store(tmp_path)
        record = _make_record(
            jianying_draft_status="succeeded",
            jianying_draft_completed_at="2026-05-03T11:00:00Z",
            jianying_draft_zip_path="/legacy/draft.zip",
            jianying_draft_fingerprint=None,
        )
        store.save_job(record)

        backend = mock.MagicMock()
        runner = _make_runner(store, backend)
        response = runner.trigger("job-test-001")

        assert response["status"] == "succeeded"
        assert response.get("_idempotent") is True
        backend.write.assert_not_called()


class TestConcurrentTriggerLock:
    """Plan §A10.7 (post-CodeX-review revision): concurrent trigger() calls
    must NOT block on the worker's long-running backend.write.

    Lock contract after the 2026-05-04 fix: trigger() takes a *short* lock
    only for state-machine transitions; the worker doesn't hold the lock for
    backend.write itself. So the second HTTP request returns immediately
    with status=="running", not after waiting minutes for the draft to
    finish. Cross-process double-spawn protection comes from the JobRecord
    state machine (status=="running" gate), not from a long-held lock.
    """

    def test_concurrent_triggers_return_running_without_blocking(self, tmp_path):
        import time

        store = _make_store(tmp_path)
        project_dir = tmp_path / "project" / "job-test-001"
        project_dir.mkdir(parents=True)
        _make_real_inputs(project_dir)
        zip_path = project_dir / "draft.zip"
        store.save_job(_make_record(project_dir=str(project_dir)))

        call_count = [0]
        in_backend = threading.Event()
        release = threading.Event()

        def slow_write(request):
            call_count[0] += 1
            in_backend.set()
            release.wait(timeout=15)
            zip_path.write_bytes(b"draft contents")
            return _make_ok_result(zip_path=str(zip_path))

        backend = mock.MagicMock()
        backend.write.side_effect = slow_write
        runner = _make_runner(store, backend)

        # First trigger: transitions to running, spawns worker
        first = runner.trigger("job-test-001")
        assert first["status"] == "running"

        # Wait for worker to enter backend.write so we know it's mid-flight
        assert in_backend.wait(timeout=5), "worker did not enter backend"

        # Second trigger MUST return quickly with "still in progress",
        # NOT block until the worker finishes. We measure wall time to
        # prove non-blocking semantics — anything over a few hundred ms
        # would mean we accidentally re-introduced the long-lock bug.
        before = time.monotonic()
        second = runner.trigger("job-test-001")
        elapsed_ms = (time.monotonic() - before) * 1000

        assert elapsed_ms < 500, (
            f"second trigger took {elapsed_ms:.0f}ms — blocking on worker "
            "lock has crept back in"
        )
        assert second["status"] == "running"
        assert second.get("message") == "still in progress"
        assert second.get("attempt_id") == first["attempt_id"], (
            "second trigger must observe the SAME attempt_id, not a fresh one"
        )

        # Release worker so the test can clean up
        release.set()
        _wait_for_jianying_status(store, "job-test-001", "succeeded")

        # Only ONE backend invocation despite the concurrent trigger
        assert call_count[0] == 1, f"backend called {call_count[0]} times"

    def test_substep_updates_dont_block_concurrent_trigger(self, tmp_path):
        """Worker's per-substep state writes take a short lock; a concurrent
        trigger that arrives between substep transitions still returns fast."""
        import time

        store = _make_store(tmp_path)
        project_dir = tmp_path / "project" / "job-test-001"
        project_dir.mkdir(parents=True)
        _make_real_inputs(project_dir)
        zip_path = project_dir / "draft.zip"
        store.save_job(_make_record(project_dir=str(project_dir)))

        in_backend = threading.Event()
        release = threading.Event()

        def slow_write(_request):
            in_backend.set()
            release.wait(timeout=15)
            zip_path.write_bytes(b"draft")
            return _make_ok_result(zip_path=str(zip_path))

        backend = mock.MagicMock()
        backend.write.side_effect = slow_write
        runner = _make_runner(store, backend)

        runner.trigger("job-test-001")
        assert in_backend.wait(timeout=5)

        # Hammer trigger multiple times concurrently from worker threads.
        # All must return quickly without spawning new workers.
        results: list[tuple[float, dict]] = []
        lock = threading.Lock()

        def race():
            t0 = time.monotonic()
            r = runner.trigger("job-test-001")
            with lock:
                results.append((time.monotonic() - t0, r))

        threads = [threading.Thread(target=race) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # Every concurrent trigger returned within 500ms
        for elapsed, resp in results:
            assert elapsed < 0.5, (
                f"trigger took {elapsed*1000:.0f}ms during worker substep updates"
            )
            assert resp["status"] == "running"

        release.set()
        _wait_for_jianying_status(store, "job-test-001", "succeeded")
        assert backend.write.call_count == 1


class TestSubstepEvents:
    """Plan §A10.8: each substep persists on JobRecord and emits a JobEvent."""

    def test_substeps_recorded_during_generation(self, tmp_path):
        from services.jobs.jianying_draft_runner import (
            SUBSTEP_BUILDING_DRAFT,
            SUBSTEP_COMPLETED,
            SUBSTEP_RESOLVING_ARTIFACTS,
            SUBSTEP_VALIDATING_INPUTS,
        )

        store = _make_store(tmp_path)
        project_dir = tmp_path / "project" / "job-test-001"
        project_dir.mkdir(parents=True)
        _make_real_inputs(project_dir)
        store.save_job(_make_record(project_dir=str(project_dir)))

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result()
        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")

        finished = _wait_for_jianying_status(store, "job-test-001", "succeeded")

        # Final substep persisted on record
        assert finished.jianying_draft_substep == SUBSTEP_COMPLETED
        assert finished.jianying_draft_attempt_id is not None
        assert finished.jianying_draft_fingerprint is not None

        # Events sequence on disk
        events = store.load_events("job-test-001")
        substeps_seen = [e.payload.get("substep") for e in events]
        # Order: validating_inputs -> resolving_artifacts -> building_draft -> ...
        assert SUBSTEP_VALIDATING_INPUTS in substeps_seen
        assert SUBSTEP_RESOLVING_ARTIFACTS in substeps_seen
        assert SUBSTEP_BUILDING_DRAFT in substeps_seen
        assert SUBSTEP_COMPLETED in substeps_seen

        # All events use stage=jianying_draft + carry attempt_id
        for ev in events:
            assert ev.stage == "jianying_draft"
            assert ev.payload.get("attempt_id") == finished.jianying_draft_attempt_id

    def test_failed_event_carries_error_classification(self, tmp_path):
        store = _make_store(tmp_path)
        project_dir = tmp_path / "project" / "job-test-001"
        project_dir.mkdir(parents=True)
        _make_real_inputs(project_dir)
        store.save_job(_make_record(project_dir=str(project_dir)))

        backend = mock.MagicMock()
        backend.write.side_effect = RuntimeError("kaboom")
        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")

        _wait_for_jianying_status(store, "job-test-001", "failed")

        events = store.load_events("job-test-001")
        failed_events = [e for e in events if e.status == "failed"]
        assert failed_events, "no failed JobEvent emitted"
        last = failed_events[-1]
        assert last.payload.get("error_code") == "unexpected_exception"
        assert last.payload.get("error_class") == "unknown"
        assert last.level == "critical"


class TestOrphanRecovery:
    """Plan §A10.5–A10.6: stale running with matching zip → succeeded;
    stale running with no zip → failed with orphan error code."""

    def test_stale_running_with_matching_zip_recovers_to_succeeded(self, tmp_path):
        from services.jobs.jianying_draft_runner import (
            _compute_jianying_fingerprint,
        )

        store = _make_store(tmp_path)
        project_dir = tmp_path / "project" / "job-stale"
        project_dir.mkdir(parents=True)
        _make_real_inputs(project_dir)

        zip_path = tmp_path / "draft.zip"
        zip_path.write_bytes(b"existing")

        seed = _make_record(job_id="job-stale", project_dir=str(project_dir))
        fingerprint = _compute_jianying_fingerprint(seed, None)

        now = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)
        stale_iso = (now - timedelta(minutes=60)).isoformat()
        store.save_job(_make_record(
            job_id="job-stale",
            project_dir=str(project_dir),
            jianying_draft_status="running",
            jianying_draft_started_at=stale_iso,
            jianying_draft_zip_path=str(zip_path),
            jianying_draft_fingerprint=fingerprint,
        ))

        runner = _make_runner(store)
        count = runner.reap_stale(now=now)
        assert count == 1

        recovered = store.require_job("job-stale")
        assert recovered.jianying_draft_status == "succeeded"
        assert recovered.jianying_draft_zip_path == str(zip_path)

        # WARN-level event with stale_running_recovered code
        events = store.load_events("job-stale")
        recovery = [e for e in events if e.payload.get("error_code") == "stale_running_recovered"]
        assert recovery, "no orphan-recovered JobEvent"
        assert recovery[-1].level == "warn"

    def test_stale_running_no_zip_reaped_as_failed_with_orphan_code(self, tmp_path):
        store = _make_store(tmp_path)
        now = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)
        stale_iso = (now - timedelta(minutes=60)).isoformat()

        # Legacy stale (fingerprint=None) → orphaned_after_process_restart
        store.save_job(_make_record(
            job_id="job-orphan",
            jianying_draft_status="running",
            jianying_draft_started_at=stale_iso,
            jianying_draft_fingerprint=None,
        ))

        runner = _make_runner(store)
        count = runner.reap_stale(now=now)
        assert count == 1

        reaped = store.require_job("job-orphan")
        assert reaped.jianying_draft_status == "failed"
        assert "stale" in (reaped.jianying_draft_error or "").lower()

        events = store.load_events("job-orphan")
        critical = [e for e in events if e.level == "critical"]
        assert critical, "no critical JobEvent emitted"
        assert critical[-1].payload.get("error_code") == "orphaned_after_process_restart"
        assert critical[-1].payload.get("error_class") == "orphan_recovery"

    def test_stale_running_with_fingerprint_no_zip_reaped(self, tmp_path):
        """Has fingerprint (newer record) but zip missing → stale_running_reaped."""
        store = _make_store(tmp_path)
        now = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)
        stale_iso = (now - timedelta(minutes=60)).isoformat()

        store.save_job(_make_record(
            job_id="job-staleprint",
            jianying_draft_status="running",
            jianying_draft_started_at=stale_iso,
            jianying_draft_zip_path="/nope.zip",
            jianying_draft_fingerprint="abc123",
        ))

        runner = _make_runner(store)
        count = runner.reap_stale(now=now)
        assert count == 1

        events = store.load_events("job-staleprint")
        critical = [e for e in events if e.level == "critical"]
        assert critical[-1].payload.get("error_code") == "stale_running_reaped"


class TestStatusApiSurfacesRunnerFields:
    """Plan §A9: get_status() returns substep / attempt_id / fingerprint."""

    def test_status_surfaces_runner_fields_after_trigger(self, tmp_path):
        store = _make_store(tmp_path)
        project_dir = tmp_path / "project" / "job-test-001"
        project_dir.mkdir(parents=True)
        _make_real_inputs(project_dir)
        store.save_job(_make_record(project_dir=str(project_dir)))

        # Block the backend so we can read status mid-run
        release = threading.Event()
        backend = mock.MagicMock()
        def hold(_req):
            release.wait(timeout=5)
            return _make_ok_result()
        backend.write.side_effect = hold

        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")

        # Poll briefly for substep to advance past initial validating_inputs
        import time
        deadline = time.monotonic() + 3.0
        status = runner.get_status("job-test-001")
        while time.monotonic() < deadline:
            status = runner.get_status("job-test-001")
            if status.get("substep") and status["substep"] != "validating_inputs":
                break
            time.sleep(0.05)

        assert status["status"] == "running"
        assert status["attempt_id"]
        assert status["fingerprint"]
        assert status["substep"] in {
            "validating_inputs",
            "resolving_artifacts",
            "building_draft",
            "validating_compatibility",
        }

        release.set()
        _wait_for_jianying_status(store, "job-test-001", "succeeded")


# ---------------------------------------------------------------------------
# D-3: ensure-whisper-aligned subtitles wired in before draft build
# ---------------------------------------------------------------------------


class TestWhisperAlignmentHookD3:
    """The runner calls ``ensure_whisper_aligned_subtitles(project_dir)``
    BEFORE building the draft so the SRT in the zip carries whisper
    timing whenever both gates are open. Both gates closed → helper
    is a no-op (`skipped_admin_disabled`); draft uses existing
    proportional cues. Helper exception → swallowed; draft generation
    continues with on-disk SRTs."""

    def test_helper_invoked_when_both_gates_open(self, tmp_path, monkeypatch):
        """Env capability ON + admin policy ON → ensure-helper called
        before backend.write."""
        monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text(
            json.dumps({"whisper_alignment_enabled": True}), encoding="utf-8",
        )

        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        record = _make_record(project_dir=str(project_dir))
        store.save_job(record)

        ensure_calls: list[str] = []

        def _fake_ensure(project_dir_arg):
            ensure_calls.append(str(project_dir_arg))
            return {"action": "regenerated", "whisper_invoked": True,
                    "blocks_processed": 5, "elapsed_ms": 12345}

        monkeypatch.setattr(
            "services.subtitles.ensure_whisper_alignment.ensure_whisper_aligned_subtitles",
            _fake_ensure,
        )

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result()
        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")
        _wait_for_jianying_status(store, "job-test-001", "succeeded")

        assert len(ensure_calls) == 1, (
            "ensure_whisper_aligned_subtitles should be called exactly once "
            f"when both gates open; got {len(ensure_calls)}"
        )
        assert str(project_dir) in ensure_calls[0]

    def test_helper_skipped_when_env_capability_off(self, tmp_path, monkeypatch):
        """Env off → ensure-helper not even imported. Saves the
        ImportError surface area for tenants without whisper deployed."""
        monkeypatch.delenv("AVT_WHISPER_ALIGN_ENABLED", raising=False)
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text(
            json.dumps({"whisper_alignment_enabled": True}), encoding="utf-8",
        )

        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        store.save_job(_make_record(project_dir=str(project_dir)))

        ensure_calls: list = []

        def _fake_ensure(project_dir_arg):
            ensure_calls.append(project_dir_arg)
            return {"action": "regenerated", "whisper_invoked": True}

        monkeypatch.setattr(
            "services.subtitles.ensure_whisper_alignment.ensure_whisper_aligned_subtitles",
            _fake_ensure,
        )

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result()
        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")
        _wait_for_jianying_status(store, "job-test-001", "succeeded")

        assert ensure_calls == [], (
            "ensure_whisper_aligned_subtitles should NOT be called when "
            "env capability is off"
        )

    def test_helper_skipped_when_admin_policy_off(self, tmp_path, monkeypatch):
        """Env on, admin policy off → ensure-helper not called. Today's
        production default — admins must opt in via the backend toggle."""
        monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text(
            json.dumps({"whisper_alignment_enabled": False}), encoding="utf-8",
        )

        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        store.save_job(_make_record(project_dir=str(project_dir)))

        ensure_calls: list = []

        def _fake_ensure(project_dir_arg):
            ensure_calls.append(project_dir_arg)
            return {"action": "regenerated", "whisper_invoked": True}

        monkeypatch.setattr(
            "services.subtitles.ensure_whisper_alignment.ensure_whisper_aligned_subtitles",
            _fake_ensure,
        )

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result()
        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")
        _wait_for_jianying_status(store, "job-test-001", "succeeded")

        assert ensure_calls == []

    def test_helper_exception_does_not_block_draft(self, tmp_path, monkeypatch):
        """Helper raises (corrupt segments file, OOM, whatever) → draft
        generation continues with on-disk SRTs. Defense-in-depth on top
        of the cue_pipeline's own fallback."""
        monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text(
            json.dumps({"whisper_alignment_enabled": True}), encoding="utf-8",
        )

        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        store.save_job(_make_record(project_dir=str(project_dir)))

        def _explode(project_dir_arg):
            raise RuntimeError("simulated whisper helper failure")

        monkeypatch.setattr(
            "services.subtitles.ensure_whisper_alignment.ensure_whisper_aligned_subtitles",
            _explode,
        )

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result()
        runner = _make_runner(store, backend)
        runner.trigger("job-test-001")
        # Despite helper raising, draft must succeed.
        final = _wait_for_jianying_status(store, "job-test-001", "succeeded")
        assert final.jianying_draft_status == "succeeded"
        assert backend.write.call_count == 1


# ---------------------------------------------------------------------------
# CodeX P1 (2026-05-05): cache-hit must NOT bypass whisper alignment.
#
# Regression: trigger() returns the cached zip when a previous succeeded
# attempt exists with a matching fingerprint and the zip is still on disk.
# Before this fix, that path completely bypassed _maybe_align_subtitles
# in the background thread — so an admin who flipped the Whisper toggle
# AFTER a proportional draft was generated would see the same old zip
# returned forever (no rebuild, no whisper).
#
# Fix: whisper-alignment policy snapshot is part of the input fingerprint.
# Any change to (enabled / trigger / model / skip_cache) flips the
# fingerprint → cache miss → fresh background run → ensure_helper runs
# → final fingerprint stamped on completion (post-alignment SRT bytes).
# Subsequent identical triggers then cache-hit cleanly.
# ---------------------------------------------------------------------------


class TestWhisperAlignmentInvalidatesCachedDraft:
    """When the admin Whisper policy changes, succeeded jobs must rebuild
    on next trigger so the new policy actually takes effect — they must
    NOT be served from cache. After rebuild, second trigger with the
    same policy must cache-hit (no infinite rebuild loop)."""

    def _setup_succeeded_proportional_draft(self, tmp_path, monkeypatch):
        """Initial state: a draft was generated under proportional cues
        (whisper disabled). Returns (store, project_dir, runner_factory)."""
        # Phase 1 setup: gates closed (no whisper involved).
        monkeypatch.delenv("AVT_WHISPER_ALIGN_ENABLED", raising=False)
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))

        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        store.save_job(_make_record(project_dir=str(project_dir)))

        # Pre-create the artifact files the fingerprint hashes; the
        # specific bytes don't matter, only that they exist + are stable
        # across triggers in this test.
        (project_dir / "source.mp4").write_bytes(b"src")
        (project_dir / "dubbed.wav").write_bytes(b"dub")
        (project_dir / "subtitles.srt").write_text("proportional", encoding="utf-8")

        return store, project_dir

    def test_admin_enables_whisper_invalidates_cached_proportional_zip(
        self, tmp_path, monkeypatch,
    ):
        """Phase 1: proportional draft exists, status=succeeded.
        Phase 2: admin opens both gates. Next trigger MUST rebuild,
        not return the old zip — otherwise admin's toggle has zero
        effect on existing tasks."""
        store, project_dir = self._setup_succeeded_proportional_draft(
            tmp_path, monkeypatch,
        )

        ensure_calls: list[str] = []

        def _fake_ensure(project_dir_arg):
            ensure_calls.append(str(project_dir_arg))
            return {"action": "regenerated", "whisper_invoked": True,
                    "blocks_processed": 5, "elapsed_ms": 12345}

        monkeypatch.setattr(
            "services.subtitles.ensure_whisper_alignment.ensure_whisper_aligned_subtitles",
            _fake_ensure,
        )

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result(zip_path=str(tmp_path / "a.zip"))
        runner = _make_runner(store, backend)

        # Phase 1: gates closed → first trigger generates proportional zip
        runner.trigger("job-test-001")
        first = _wait_for_jianying_status(store, "job-test-001", "succeeded")
        # Make the zip path actually exist so the cache-hit branch on the
        # next trigger has a real file to point at.
        Path(first.jianying_draft_zip_path).write_bytes(b"first-zip")
        assert ensure_calls == [], (
            "Phase 1 control: gates closed → no whisper invocation"
        )
        first_fingerprint = first.jianying_draft_fingerprint

        # Phase 2: admin flips both gates ON, then user triggers again.
        monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
        (tmp_path / "admin_settings.json").write_text(
            json.dumps({
                "whisper_alignment_enabled": True,
                "whisper_alignment_trigger": "deliverable",
            }),
            encoding="utf-8",
        )

        # Reset backend call count so we can verify a NEW backend.write
        # happened on this second trigger (vs. cache hit).
        backend.write.reset_mock()
        backend.write.return_value = _make_ok_result(
            zip_path=str(tmp_path / "b.zip"),
        )

        runner.trigger("job-test-001")
        second = _wait_for_jianying_status(store, "job-test-001", "succeeded")
        Path(second.jianying_draft_zip_path).write_bytes(b"second-zip")

        # The whisper helper must have run during this rebuild.
        assert ensure_calls == [str(project_dir)], (
            "After admin opens gates, the next trigger MUST run "
            "ensure_whisper_aligned_subtitles. Got %r" % ensure_calls
        )
        # And backend.write must have run (proving cache was invalidated).
        assert backend.write.call_count == 1, (
            "Phase 2 must rebuild the draft (backend.write call count = 1); "
            "cache-hit on the proportional fingerprint would mean count = 0."
        )
        # Fingerprint should differ from phase 1 (whisper policy snapshot
        # is part of the fingerprint, OR the post-alignment SRT changed
        # the input set — either way the stored fingerprint must update).
        assert second.jianying_draft_fingerprint != first_fingerprint, (
            "After whisper rebuild, the stored fingerprint must reflect "
            "the new state (policy + post-alignment SRT). "
            "Otherwise next trigger would also rebuild needlessly."
        )

    def test_second_trigger_after_whisper_rebuild_hits_cache(
        self, tmp_path, monkeypatch,
    ):
        """After phase-2 rebuild produces a new whisper-aligned zip + new
        fingerprint, the THIRD trigger (same admin policy, same inputs)
        must cache-hit — no extra ensure_helper invocations, no extra
        backend.write calls.

        This is the "no infinite rebuild loop" guarantee. If the
        post-rebuild fingerprint isn't the one that gets stamped on the
        succeeded record, every subsequent trigger would compute a
        different fingerprint and rebuild again forever."""
        store, project_dir = self._setup_succeeded_proportional_draft(
            tmp_path, monkeypatch,
        )

        ensure_calls: list[str] = []

        def _fake_ensure(project_dir_arg):
            ensure_calls.append(str(project_dir_arg))
            # Simulate the helper rewriting the SRT (whisper-aligned content)
            (Path(project_dir_arg) / "subtitles.srt").write_text(
                "whisper-aligned-content", encoding="utf-8",
            )
            return {"action": "regenerated", "whisper_invoked": True,
                    "blocks_processed": 5, "elapsed_ms": 12345}

        monkeypatch.setattr(
            "services.subtitles.ensure_whisper_alignment.ensure_whisper_aligned_subtitles",
            _fake_ensure,
        )

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result(
            zip_path=str(tmp_path / "draft.zip"),
        )
        runner = _make_runner(store, backend)

        # Phase 1: gates closed, build proportional draft.
        runner.trigger("job-test-001")
        first = _wait_for_jianying_status(store, "job-test-001", "succeeded")
        Path(first.jianying_draft_zip_path).write_bytes(b"first")

        # Phase 2: open gates, trigger rebuild.
        monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")
        (tmp_path / "admin_settings.json").write_text(
            json.dumps({
                "whisper_alignment_enabled": True,
                "whisper_alignment_trigger": "deliverable",
            }),
            encoding="utf-8",
        )
        backend.write.reset_mock()
        backend.write.return_value = _make_ok_result(
            zip_path=str(tmp_path / "draft2.zip"),
        )
        runner.trigger("job-test-001")
        second = _wait_for_jianying_status(store, "job-test-001", "succeeded")
        Path(second.jianying_draft_zip_path).write_bytes(b"second")
        assert backend.write.call_count == 1
        assert len(ensure_calls) == 1

        # Phase 3: third trigger — same admin policy, same inputs (the
        # SRT is now whisper-aligned, but stable across calls). Should
        # cache-hit: no new backend.write, no new ensure_helper call.
        backend.write.reset_mock()
        runner.trigger("job-test-001")
        third = _wait_for_jianying_status(store, "job-test-001", "succeeded")

        assert backend.write.call_count == 0, (
            "Third trigger must cache-hit (no new backend.write). Got "
            f"{backend.write.call_count} call(s) — fingerprint after "
            "rebuild was not stamped correctly, causing perpetual rebuild."
        )
        assert len(ensure_calls) == 1, (
            "Third trigger must NOT re-run ensure_helper (cache hit). "
            f"Got {len(ensure_calls)} call(s)."
        )
        # Cached zip path stays the same.
        assert third.jianying_draft_zip_path == second.jianying_draft_zip_path


# ---------------------------------------------------------------------------
# CodeX P1 follow-up #2 (2026-05-05): env capability flip must invalidate
# cached jianying drafts even when admin policy is unchanged.
#
# Regression: _whisper_policy_snapshot() only read the four
# admin_settings.json fields, NOT the AVT_WHISPER_ALIGN_ENABLED env var.
# Rollout sequence:
#   1. admin saves whisper_alignment_enabled=true
#   2. ops env still off → effective gate is closed → trigger generates
#      proportional zip with fingerprint = F_admin_only
#   3. ops sets AVT_WHISPER_ALIGN_ENABLED=1 → effective gate opens
#   4. admin policy snapshot is unchanged → fingerprint still F_admin_only
#      → trigger() returns cached proportional zip → admin's whisper
#      toggle has zero effect even though both gates are now open
# This is the precise scenario double-gate rollout creates: admin
# typically opts in BEFORE ops capability is rolled. Fix: include the
# env capability bool in the fingerprint snapshot so env changes
# invalidate caches just like admin changes do.
# ---------------------------------------------------------------------------


class TestEnvCapabilityFlipsInvalidateCachedDraft:
    """Same shape as TestWhisperAlignmentInvalidatesCachedDraft but
    flipping the OPS env capability instead of the admin policy."""

    def test_env_capability_off_to_on_invalidates_cached_proportional_zip(
        self, tmp_path, monkeypatch,
    ):
        """Phase 1: admin enabled=true but env off → proportional zip.
        Phase 2: ops sets env=1 (admin unchanged) → next trigger MUST
        rebuild. Without env in fingerprint this would cache-hit and
        admin's earlier opt-in would never take effect."""
        # Phase 1 setup: admin opted in BEFORE env was opened.
        monkeypatch.delenv("AVT_WHISPER_ALIGN_ENABLED", raising=False)
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        (tmp_path / "admin_settings.json").write_text(
            json.dumps({
                "whisper_alignment_enabled": True,
                "whisper_alignment_trigger": "deliverable",
            }),
            encoding="utf-8",
        )

        store = _make_store(tmp_path)
        project_dir = _make_project_dir(tmp_path)
        store.save_job(_make_record(project_dir=str(project_dir)))
        (project_dir / "source.mp4").write_bytes(b"src")
        (project_dir / "dubbed.wav").write_bytes(b"dub")
        (project_dir / "subtitles.srt").write_text("proportional", encoding="utf-8")

        ensure_calls: list[str] = []

        def _fake_ensure(project_dir_arg):
            ensure_calls.append(str(project_dir_arg))
            return {"action": "regenerated", "whisper_invoked": True,
                    "blocks_processed": 5, "elapsed_ms": 12345}

        monkeypatch.setattr(
            "services.subtitles.ensure_whisper_alignment.ensure_whisper_aligned_subtitles",
            _fake_ensure,
        )

        backend = mock.MagicMock()
        backend.write.return_value = _make_ok_result(
            zip_path=str(tmp_path / "first.zip"),
        )
        runner = _make_runner(store, backend)

        # Phase 1: env off → effective gate closed → ensure_helper not
        # invoked (the runner short-circuits via _whisper_align_enabled).
        runner.trigger("job-test-001")
        first = _wait_for_jianying_status(store, "job-test-001", "succeeded")
        Path(first.jianying_draft_zip_path).write_bytes(b"first-zip")
        assert ensure_calls == [], (
            "Phase 1 control: env off → no whisper invocation regardless "
            "of admin policy"
        )
        first_fp = first.jianying_draft_fingerprint

        # Phase 2: ops opens env. Admin already has enabled=true; nothing
        # else changes (same project files, same admin policy).
        monkeypatch.setenv("AVT_WHISPER_ALIGN_ENABLED", "1")

        backend.write.reset_mock()
        backend.write.return_value = _make_ok_result(
            zip_path=str(tmp_path / "second.zip"),
        )

        runner.trigger("job-test-001")
        second = _wait_for_jianying_status(store, "job-test-001", "succeeded")
        Path(second.jianying_draft_zip_path).write_bytes(b"second-zip")

        # The whisper helper must have run on this rebuild.
        assert ensure_calls == [str(project_dir)], (
            "After ops opens env capability, the next trigger MUST "
            "rebuild and run ensure_whisper_aligned_subtitles. "
            f"Got ensure_calls={ensure_calls!r}."
        )
        assert backend.write.call_count == 1, (
            "Phase 2 trigger MUST rebuild (backend.write called once). "
            "Cache-hit on the env-unaware fingerprint would mean call=0."
        )
        # Fingerprint must change between phase 1 and phase 2 — env
        # capability is now part of the fingerprint inputs.
        assert second.jianying_draft_fingerprint != first_fp, (
            "Stored fingerprint must differ between env-off phase 1 and "
            "env-on phase 2 — otherwise next trigger would also rebuild "
            "perpetually."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
