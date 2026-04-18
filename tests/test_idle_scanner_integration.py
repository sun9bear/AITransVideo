"""T1-10 — idle scanner with real cancel callback injection.

The Phase 0 scanner shipped with a ``_noop_cancel`` default so nothing
would fire until Phase 1 landed. T1-10 adds ``inject_editing_cancel_callback``,
an app-startup-time binding that routes scan hits to
``JobService.cancel_editing(job_id, reason='idle_24h_auto_cancel')``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.jobs.editing import EDITING_SUBDIR, enter_editing
from services.jobs.models import JOB_STATUS_EDITING, JOB_STATUS_SUCCEEDED, JobRecord
from services.jobs.service import JobService
from services.jobs.store import JobStore
from services.web_ui import editing_idle_scanner
from services.web_ui.editing_idle_scanner import (
    IDLE_THRESHOLD_HOURS,
    REASON_IDLE_AUTO,
    inject_editing_cancel_callback,
    reset_editing_cancel_callback,
    scan_editing_idle,
)


class _NullRunner:
    pass


@pytest.fixture(autouse=True)
def _restore_callback():
    """Every test resets the module-level callback. Otherwise the
    inject tests would leak into later tests in the session."""
    yield
    reset_editing_cancel_callback()


def _build_idle_editing_job(tmp_path: Path, *, hours_idle: int) -> tuple[JobService, str, Path]:
    project_dir = tmp_path / "projects" / "job_idle"
    (project_dir / "editor").mkdir(parents=True)
    (project_dir / "editor" / "segments.json").write_text("[]", encoding="utf-8")

    now = datetime.now(timezone.utc)
    created = now - timedelta(hours=hours_idle + 1)
    record = JobRecord(
        job_id="job_idle",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://example.com",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=JOB_STATUS_SUCCEEDED,
        current_stage="completed",
        progress_message=None,
        created_at=created.isoformat(),
        updated_at=created.isoformat(),
        project_dir=str(project_dir),
        service_mode="studio",
    )
    store = JobStore(tmp_path / "jobs")
    store.save_job(record)
    # Enter editing, then backdate touched_at to simulate hours_idle
    enter_editing(record, store)
    updated = store.require_job("job_idle")
    idle_touched = (now - timedelta(hours=hours_idle)).isoformat()
    from dataclasses import replace
    store.save_job(replace(updated, editing_touched_at=idle_touched))

    service = JobService(store=store, runner=_NullRunner())
    return service, "job_idle", project_dir


# ---------------------------------------------------------------------------
# inject_editing_cancel_callback
# ---------------------------------------------------------------------------


def test_inject_replaces_noop(tmp_path: Path) -> None:
    service, _, _ = _build_idle_editing_job(tmp_path, hours_idle=25)
    # Default is _noop_cancel
    from services.web_ui.editing_idle_scanner import _noop_cancel
    assert editing_idle_scanner.registered_cancel_callback is _noop_cancel

    inject_editing_cancel_callback(service)

    assert editing_idle_scanner.registered_cancel_callback is not _noop_cancel


def test_injected_callback_actually_cancels_editing(tmp_path: Path) -> None:
    service, job_id, project_dir = _build_idle_editing_job(tmp_path, hours_idle=25)
    inject_editing_cancel_callback(service)

    result = scan_editing_idle(
        datetime.now(timezone.utc),
        editing_idle_scanner.registered_cancel_callback,
        jobs_dir=tmp_path / "jobs",
    )

    assert result["cancelled"] == [job_id]
    # Job is now succeeded, editing/ dir is gone
    final = service.require_job(job_id)
    assert final.status == JOB_STATUS_SUCCEEDED
    assert final.editing_touched_at is None
    assert not (project_dir / EDITING_SUBDIR).exists()


def test_injected_callback_reports_failure_on_exception(tmp_path: Path, monkeypatch) -> None:
    service, _, _ = _build_idle_editing_job(tmp_path, hours_idle=25)
    inject_editing_cancel_callback(service)

    # Swap the service's cancel_editing to raise
    def boom(job_id, *, reason):
        raise RuntimeError("DB down")
    monkeypatch.setattr(service, "cancel_editing", boom)

    result = scan_editing_idle(
        datetime.now(timezone.utc),
        editing_idle_scanner.registered_cancel_callback,
        jobs_dir=tmp_path / "jobs",
    )
    assert result["failed"] == ["job_idle"]
    assert result["cancelled"] == []


def test_injected_callback_ignores_fresh_editing_jobs(tmp_path: Path) -> None:
    service, _, _ = _build_idle_editing_job(tmp_path, hours_idle=1)  # only 1h idle
    inject_editing_cancel_callback(service)

    result = scan_editing_idle(
        datetime.now(timezone.utc),
        editing_idle_scanner.registered_cancel_callback,
        jobs_dir=tmp_path / "jobs",
    )

    assert result["candidates"] == []
    # Job stays in editing
    assert service.require_job("job_idle").status == JOB_STATUS_EDITING


def test_injected_callback_uses_reason_idle_auto(tmp_path: Path) -> None:
    """The reason string is surfaced in job events — verifies the callback
    passes through the REASON_IDLE_AUTO constant, not a random string."""
    service, job_id, _ = _build_idle_editing_job(tmp_path, hours_idle=25)
    inject_editing_cancel_callback(service)

    scan_editing_idle(
        datetime.now(timezone.utc),
        editing_idle_scanner.registered_cancel_callback,
        jobs_dir=tmp_path / "jobs",
    )

    events = service.read_logs(job_id)
    cancel_events = [e for e in events if e.message and "editing.cancelled" in e.message]
    assert len(cancel_events) == 1
    assert REASON_IDLE_AUTO in cancel_events[0].message


def test_reset_restores_noop(tmp_path: Path) -> None:
    service, _, _ = _build_idle_editing_job(tmp_path, hours_idle=25)
    inject_editing_cancel_callback(service)
    reset_editing_cancel_callback()
    from services.web_ui.editing_idle_scanner import _noop_cancel
    assert editing_idle_scanner.registered_cancel_callback is _noop_cancel


def test_idle_threshold_constant_is_24_hours() -> None:
    """Contract — plan §5.4: 24 h cutoff."""
    assert IDLE_THRESHOLD_HOURS == 24
