"""Integration tests for T1-1 editing endpoints skeleton.

Scope:
- editing.py business logic (state transitions + filesystem + event emission)
- JobService delegate methods (enter_editing / cancel_editing / commit_editing)

Out of scope (deferred):
- Gateway FOR UPDATE lock behaviour — exercised end-to-end in manual smoke;
  unit-testing the SQLAlchemy lock path requires a live Postgres and is
  tracked as part of §17.4 smoke, not here.
- HTTP-layer dispatch in ``api.py`` ``do_POST`` — the branches are thin
  wrappers around ``service.*_editing``; testing the stdlib ThreadingHTTPServer
  adds significant fixture weight without catching bugs that the delegate
  tests below don't already cover. T1-2 onwards, when real HTTP semantics
  matter (multipart uploads, streaming, etc.), we'll add an HTTP-level suite.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.jobs.editing import (
    EDITING_SUBDIR,
    SUPPORTED_COMMIT_STRATEGIES,
    EditingConflictError,
    cancel_editing,
    commit_editing,
    enter_editing,
    touch_editing,
)
from services.jobs.models import (
    JOB_STATUS_EDITING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JobRecord,
)
from services.jobs.service import JobConflictError, JobService
from services.jobs.store import JobStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_studio_succeeded_record(tmp_path: Path) -> tuple[JobRecord, JobStore, Path]:
    """Build a studio job in succeeded state with a real project_dir
    containing an editor/segments.json baseline (enter_editing snapshots it)."""
    project_dir = tmp_path / "projects" / "job_123"
    (project_dir / "editor").mkdir(parents=True)
    (project_dir / "editor" / "segments.json").write_text(
        '[{"segment_id": "s_001", "cn_text": "hello"}]',
        encoding="utf-8",
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    record = JobRecord(
        job_id="job_123",
        job_type="localize_video",
        source_type="youtube_url",
        source_ref="https://example.com/video",
        output_target="editor",
        speakers="auto",
        voice_a=None,
        voice_b=None,
        status=JOB_STATUS_SUCCEEDED,
        current_stage="completed",
        progress_message=None,
        created_at=now_iso,
        updated_at=now_iso,
        project_dir=str(project_dir),
        service_mode="studio",
    )
    store = JobStore(tmp_path / "jobs")
    store.save_job(record)
    return record, store, project_dir


class _NullRunner:
    """Minimal runner stub. editing delegates never touch runner attributes;
    JobService.__init__ just assigns it as ``self.runner``."""


def _build_service_with_editing_fixture(tmp_path: Path) -> tuple[JobService, Path]:
    _, store, project_dir = _build_studio_succeeded_record(tmp_path)
    service = JobService(store=store, runner=_NullRunner())
    return service, project_dir


# ---------------------------------------------------------------------------
# editing.enter_editing
# ---------------------------------------------------------------------------


def test_enter_editing_succeeded_to_editing(tmp_path: Path) -> None:
    record, store, project_dir = _build_studio_succeeded_record(tmp_path)

    updated = enter_editing(record, store)

    assert updated.status == JOB_STATUS_EDITING
    assert updated.editing_touched_at is not None
    editing_dir = project_dir / EDITING_SUBDIR
    assert editing_dir.is_dir()
    assert (editing_dir / "tts_segments_draft").is_dir()
    # Baseline snapshot copied byte-for-byte
    baseline = (project_dir / "editor" / "segments.json").read_text(encoding="utf-8")
    assert (editing_dir / "segments.json").read_text(encoding="utf-8") == baseline
    # Persisted
    assert store.require_job("job_123").status == JOB_STATUS_EDITING


def test_enter_editing_rejects_running(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    record = replace(record, status=JOB_STATUS_RUNNING)
    store.save_job(record)

    with pytest.raises(EditingConflictError, match="can only enter editing from succeeded"):
        enter_editing(record, store)


def test_enter_editing_rejects_already_editing(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    updated = enter_editing(record, store)

    with pytest.raises(EditingConflictError, match="already in editing state"):
        enter_editing(updated, store)


def test_enter_editing_rejects_express(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    record = replace(record, service_mode="express")
    store.save_job(record)

    with pytest.raises(EditingConflictError, match="not a Studio job"):
        enter_editing(record, store)


def test_enter_editing_rejects_missing_project_dir(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    record = replace(record, project_dir=str(tmp_path / "does_not_exist"))
    store.save_job(record)

    with pytest.raises(EditingConflictError, match="project_dir does not exist"):
        enter_editing(record, store)


def test_enter_editing_rejects_empty_project_dir(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    record = replace(record, project_dir=None)
    store.save_job(record)

    with pytest.raises(EditingConflictError, match="has no project_dir"):
        enter_editing(record, store)


def test_editing_conflict_is_job_conflict_subclass() -> None:
    """Allows api.py ``except JobConflictError`` path to cover editing errors
    without adding a bespoke except branch (current api.py depends on this)."""
    assert issubclass(EditingConflictError, JobConflictError)


# ---------------------------------------------------------------------------
# editing.cancel_editing
# ---------------------------------------------------------------------------


def test_cancel_editing_drops_draft_and_reverts_status(tmp_path: Path) -> None:
    record, store, project_dir = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)
    editing_dir = project_dir / EDITING_SUBDIR
    assert editing_dir.is_dir()

    reverted = cancel_editing(editing_record, store, reason="user_cancel")

    assert reverted.status == JOB_STATUS_SUCCEEDED
    assert reverted.editing_touched_at is None
    assert not editing_dir.exists()
    assert store.require_job("job_123").status == JOB_STATUS_SUCCEEDED


def test_cancel_editing_records_reason_on_event(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)

    cancel_editing(editing_record, store, reason="admin_force")

    events = store.load_events("job_123")
    cancel_events = [e for e in events if e.message and "editing.cancelled" in e.message]
    assert len(cancel_events) == 1
    assert "reason=admin_force" in cancel_events[0].message


def test_cancel_editing_rejects_non_editing(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)

    with pytest.raises(EditingConflictError, match="not in editing state"):
        cancel_editing(record, store, reason="user_cancel")


def test_cancel_editing_survives_missing_editing_dir(tmp_path: Path) -> None:
    """Robust against partial states (e.g. manual rm -rf)."""
    record, store, project_dir = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)
    # Manually blow away the dir before cancel fires
    import shutil as _shutil

    _shutil.rmtree(project_dir / EDITING_SUBDIR)
    reverted = cancel_editing(editing_record, store, reason="user_cancel")

    assert reverted.status == JOB_STATUS_SUCCEEDED


# ---------------------------------------------------------------------------
# editing.commit_editing (T1-1 skeleton)
# ---------------------------------------------------------------------------


def test_commit_editing_valid_overwrite_hits_not_implemented(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)

    with pytest.raises(NotImplementedError, match="T1-9"):
        commit_editing(editing_record, store, strategy="overwrite")


def test_commit_editing_copy_as_new_with_name_hits_not_implemented(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)

    with pytest.raises(NotImplementedError):
        commit_editing(
            editing_record,
            store,
            strategy="copy_as_new",
            copy_display_name="A · 副本 1",
        )


def test_commit_editing_rejects_unknown_strategy(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)

    with pytest.raises(EditingConflictError, match="unsupported commit strategy"):
        commit_editing(editing_record, store, strategy="bogus")


def test_commit_editing_rejects_non_editing(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)

    with pytest.raises(EditingConflictError, match="not in editing state"):
        commit_editing(record, store, strategy="overwrite")


def test_commit_editing_copy_as_new_requires_non_empty_display_name(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)

    with pytest.raises(EditingConflictError, match="requires a non-empty copy_display_name"):
        commit_editing(editing_record, store, strategy="copy_as_new")
    with pytest.raises(EditingConflictError, match="requires a non-empty copy_display_name"):
        commit_editing(editing_record, store, strategy="copy_as_new", copy_display_name="   ")


def test_supported_commit_strategies_contract() -> None:
    """Locked so frontend can code against this set directly."""
    assert SUPPORTED_COMMIT_STRATEGIES == frozenset({"overwrite", "copy_as_new"})


# ---------------------------------------------------------------------------
# editing.touch_editing
# ---------------------------------------------------------------------------


def test_touch_editing_refreshes_touched_at(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)
    original = editing_record.editing_touched_at
    assert original is not None

    time.sleep(0.005)  # ensure observable delta in ISO timestamp string ordering

    touched = touch_editing(editing_record, store)

    assert touched.editing_touched_at is not None
    assert touched.editing_touched_at > original
    assert touched.status == JOB_STATUS_EDITING
    # Persisted
    assert store.require_job("job_123").editing_touched_at == touched.editing_touched_at


def test_touch_editing_noop_when_not_editing(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)

    result = touch_editing(record, store)

    # Succeeded job — touch should return the record unchanged and NOT
    # persist a bogus touched_at.
    assert result.status == JOB_STATUS_SUCCEEDED
    assert result.editing_touched_at is None


# ---------------------------------------------------------------------------
# Event emission (append_event on enter + cancel)
# ---------------------------------------------------------------------------


def test_enter_editing_emits_status_event(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    enter_editing(record, store)

    events = store.load_events("job_123")
    enter_events = [e for e in events if e.message and "editing.entered" in e.message]
    assert len(enter_events) == 1
    assert enter_events[0].status == JOB_STATUS_EDITING
    assert enter_events[0].event_type == "status"


def test_enter_then_cancel_emits_two_events_in_order(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    editing_record = enter_editing(record, store)
    cancel_editing(editing_record, store, reason="user_cancel")

    events = store.load_events("job_123")
    editing_events = [
        e for e in events
        if e.message and ("editing.entered" in e.message or "editing.cancelled" in e.message)
    ]
    assert len(editing_events) == 2
    assert "editing.entered" in editing_events[0].message
    assert "editing.cancelled" in editing_events[1].message


# ---------------------------------------------------------------------------
# JobService delegates
# ---------------------------------------------------------------------------


def test_service_enter_editing_delegates(tmp_path: Path) -> None:
    service, project_dir = _build_service_with_editing_fixture(tmp_path)

    updated = service.enter_editing("job_123")

    assert updated.status == JOB_STATUS_EDITING
    assert (project_dir / EDITING_SUBDIR).is_dir()


def test_service_cancel_editing_passes_reason_through(tmp_path: Path) -> None:
    service, _ = _build_service_with_editing_fixture(tmp_path)
    service.enter_editing("job_123")

    reverted = service.cancel_editing("job_123", reason="idle_24h_auto_cancel")

    assert reverted.status == JOB_STATUS_SUCCEEDED
    events = service.read_logs("job_123")
    assert any("idle_24h_auto_cancel" in (e.message or "") for e in events)


def test_service_commit_editing_raises_not_implemented(tmp_path: Path) -> None:
    service, _ = _build_service_with_editing_fixture(tmp_path)
    service.enter_editing("job_123")

    with pytest.raises(NotImplementedError):
        service.commit_editing("job_123", strategy="overwrite")


def test_service_enter_editing_on_nonexistent_job(tmp_path: Path) -> None:
    from services.jobs.service import JobNotFoundError

    store = JobStore(tmp_path / "jobs")
    service = JobService(store=store, runner=_NullRunner())

    with pytest.raises(JobNotFoundError):
        service.enter_editing("ghost")


# ---------------------------------------------------------------------------
# Cross-module contract smoke: editing_touched_at persists through store
# round-trip, and cancel clears it back to None.
# ---------------------------------------------------------------------------


def test_editing_touched_at_round_trips_through_store(tmp_path: Path) -> None:
    record, store, _ = _build_studio_succeeded_record(tmp_path)
    enter_editing(record, store)

    # Reload from disk
    reloaded = store.require_job("job_123")
    assert reloaded.status == JOB_STATUS_EDITING
    assert reloaded.editing_touched_at is not None

    # Cancel and reload again
    cancel_editing(reloaded, store, reason="user_cancel")
    reloaded2 = store.require_job("job_123")
    assert reloaded2.status == JOB_STATUS_SUCCEEDED
    assert reloaded2.editing_touched_at is None
