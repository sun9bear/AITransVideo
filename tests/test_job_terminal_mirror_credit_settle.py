from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


REPO = Path(__file__).resolve().parent.parent
GATEWAY_DIR = REPO / "gateway"
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import job_terminal_mirror  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_terminal_mirror_settles_credit_ledger_on_r2_sweeper_path(monkeypatch):
    db = AsyncMock()
    job = SimpleNamespace(
        job_id="job_r2",
        status="running",
        current_stage="s5",
        project_dir="/tmp/project",
        completed_at=None,
        edit_generation=0,
    )
    upstream = SimpleNamespace(
        status="succeeded",
        current_stage="completed",
        project_dir="/tmp/project",
        completed_at=None,
        edit_generation=0,
    )
    quota_settle = AsyncMock()
    credit_settle = AsyncMock()
    monkeypatch.setattr(job_terminal_mirror, "settle_job_quota", quota_settle)
    monkeypatch.setattr(job_terminal_mirror, "settle_job_credit_ledger", credit_settle)

    changed = _run(job_terminal_mirror.mirror_job_terminal_state(db, job, upstream))

    assert changed is True
    quota_settle.assert_awaited_once_with(db, job, "succeeded")
    credit_settle.assert_awaited_once_with(db, job, "succeeded")


def test_terminal_mirror_does_not_resettle_already_terminal(monkeypatch):
    db = AsyncMock()
    job = SimpleNamespace(
        job_id="job_done",
        status="succeeded",
        current_stage="completed",
        project_dir="/tmp/project",
        completed_at=None,
        edit_generation=0,
        quota_state="committed",
    )
    upstream = SimpleNamespace(
        status="succeeded",
        current_stage="completed",
        project_dir="/tmp/project",
        completed_at=None,
        edit_generation=0,
    )
    quota_settle = AsyncMock()
    credit_settle = AsyncMock()
    credit_settle.return_value = []
    monkeypatch.setattr(job_terminal_mirror, "settle_job_quota", quota_settle)
    monkeypatch.setattr(job_terminal_mirror, "settle_job_credit_ledger", credit_settle)

    changed = _run(job_terminal_mirror.mirror_job_terminal_state(db, job, upstream))

    assert changed is False
    quota_settle.assert_awaited_once_with(db, job, "succeeded")
    credit_settle.assert_awaited_once_with(db, job, "succeeded")


def test_terminal_mirror_compensates_already_terminal_reserved_job(monkeypatch):
    db = AsyncMock()
    job = SimpleNamespace(
        job_id="job_reserved_done",
        status="succeeded",
        current_stage="completed",
        project_dir="/tmp/project",
        completed_at=None,
        edit_generation=0,
        quota_state="reserved",
    )
    upstream = SimpleNamespace(
        status="succeeded",
        current_stage="completed",
        project_dir="/tmp/project",
        completed_at=None,
        edit_generation=0,
    )

    async def quota_settle(_db, _job, _status):
        _job.quota_state = "committed"

    credit_settle = AsyncMock()
    credit_settle.return_value = [SimpleNamespace(direction="capture")]
    monkeypatch.setattr(job_terminal_mirror, "settle_job_quota", quota_settle)
    monkeypatch.setattr(job_terminal_mirror, "settle_job_credit_ledger", credit_settle)

    changed = _run(job_terminal_mirror.mirror_job_terminal_state(db, job, upstream))

    assert changed is True
    assert job.quota_state == "committed"
    credit_settle.assert_awaited_once_with(db, job, "succeeded")


def test_notification_transition_does_not_write_job_status(monkeypatch):
    import notifications_helpers

    db = AsyncMock()
    job = SimpleNamespace(
        id="row-1",
        job_id="job_notify",
        user_id="user-1",
        status="running",
        display_name="Demo",
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(notifications_helpers, "dispatch_event", dispatch)

    _run(
        notifications_helpers.maybe_dispatch_job_transition(
            db,
            db_job=job,
            upstream_status="succeeded",
        )
    )

    assert job.status == "running"
    dispatch.assert_awaited_once()
