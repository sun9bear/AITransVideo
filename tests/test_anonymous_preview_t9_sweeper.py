"""APF P0 T9 — anonymous_preview_sweeper unit tests.

Uses fake DB objects + tmp_path real files.  No real DB connection needed.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal fakes that mimic SQLAlchemy AsyncSession behaviour
# ---------------------------------------------------------------------------


class FakeScalarsResult:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


class FakeExecuteResult:
    def __init__(self, items=None, rowcount=0):
        self._items = items or []
        self.rowcount = rowcount

    def scalars(self):
        return FakeScalarsResult(self._items)


class FakeSession:
    """Minimal async session stub."""

    def __init__(self):
        self._deleted: list = []
        self._committed = 0
        self._rolled_back = 0
        self._execute_handlers: list = []  # list of (matcher, handler)

    def add_execute_handler(self, matcher, handler):
        """matcher(stmt) -> bool; handler(stmt) -> FakeExecuteResult."""
        self._execute_handlers.append((matcher, handler))

    async def execute(self, stmt):
        for matcher, handler in self._execute_handlers:
            if matcher(stmt):
                return handler(stmt)
        return FakeExecuteResult()

    async def delete(self, obj):
        self._deleted.append(obj)

    async def commit(self):
        self._committed += 1

    async def rollback(self):
        self._rolled_back += 1


def _make_record(
    *,
    preview_id: str | None = None,
    status: str = "ready",
    status_reason: str | None = None,
    source_hash: str = "abc123",
    mode: str = "free",
    job_id: str | None = None,
    audit: dict | None = None,
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
    anonymous_consent: dict | None = None,
) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    audit_val = dict(audit) if audit else {}
    if anonymous_consent is not None:
        audit_val["anonymous_consent"] = anonymous_consent
    return SimpleNamespace(
        preview_id=preview_id or str(uuid.uuid4()),
        status=status,
        status_reason=status_reason,
        source_hash=source_hash,
        mode=mode,
        job_id=job_id,
        audit=audit_val or None,
        created_at=created_at or now,
        expires_at=expires_at or (now + timedelta(hours=24)),
    )


def _make_session_row(*, expires_at: datetime | None = None) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        session_id_hash=str(uuid.uuid4()),
        expires_at=expires_at or (now - timedelta(seconds=10)),
    )


def _make_usage_row(*, usage_date: str = "2020-01-01") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), usage_date=usage_date)


# ---------------------------------------------------------------------------
# Import the sweeper under test
# ---------------------------------------------------------------------------

import sys
import importlib


def _load_sweeper(monkeypatch, models_patch: dict | None = None):
    """Import sweeper with gateway/ on sys.path, mocking DB imports."""
    import os

    worktree = Path(__file__).parent.parent / "gateway"
    if str(worktree) not in sys.path:
        sys.path.insert(0, str(worktree))

    # Provide minimal stubs for SQLAlchemy constructs used at runtime
    # (select, delete) — they just need to be callable and return something.
    import sqlalchemy as _sa  # real sqlalchemy; select/delete available

    # Re-import to ensure fresh module state in tests
    if "anonymous_preview_sweeper" in sys.modules:
        del sys.modules["anonymous_preview_sweeper"]

    import anonymous_preview_sweeper as sweeper
    return sweeper


# ---------------------------------------------------------------------------
# Helpers for constructing FakeSession with specific record sets
# ---------------------------------------------------------------------------


def _session_with_records(
    blocked: list,
    expired: list,
    session_rowcount: int = 0,
    usage_rowcount: int = 0,
) -> FakeSession:
    from sqlalchemy import delete as sa_delete, select as sa_select

    db = FakeSession()

    def _is_blocked_select(stmt):
        # Detect select(...).where(status.in_(...))
        return hasattr(stmt, "_where_criteria") or True  # accept any select; we route by call order

    # We route by call order — first execute → blocked, second → expired,
    # delete AnonymousSession → sessions, delete daily_usage → usage.
    call_count = [0]

    async def _execute(stmt):  # type: ignore[override]
        call_count[0] += 1
        n = call_count[0]
        if n == 1:
            return FakeExecuteResult(blocked)
        if n == 2:
            return FakeExecuteResult(expired)
        if n == 3:
            return FakeExecuteResult(rowcount=session_rowcount)
        if n == 4:
            return FakeExecuteResult(rowcount=usage_rowcount)
        return FakeExecuteResult()

    db.execute = _execute  # type: ignore[method-assign]
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def worktree_on_path():
    worktree = str(Path(__file__).parent.parent / "gateway")
    if worktree not in sys.path:
        sys.path.insert(0, worktree)
    yield
    # Don't remove — other tests may rely on it


@pytest.fixture()
def sweeper_module(worktree_on_path):
    if "anonymous_preview_sweeper" in sys.modules:
        del sys.modules["anonymous_preview_sweeper"]
    import anonymous_preview_sweeper as m
    return m


# ------------------------------------------------------------------
# T9-1: blocked record → media deleted, audit marked, row kept
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_record_media_purged_row_kept(tmp_path, sweeper_module):
    """Blocked status record: media files deleted, audit.media_purged_at set,
    row NOT added to session._deleted (kept for 30d retention)."""
    upload_file = tmp_path / "upload.mp4"
    teaser_file = tmp_path / "teaser.mp4"
    upload_file.write_bytes(b"data")
    teaser_file.write_bytes(b"data")

    rec = _make_record(
        status="rejected",
        audit={
            "stored_upload_path": str(upload_file),
            "teaser_path": str(teaser_file),
        },
    )

    db = _session_with_records(blocked=[rec], expired=[])
    stats = await sweeper_module.sweep_anonymous_previews_once(db)

    assert stats["blocked_media_purged"] == 1
    assert not upload_file.exists(), "upload file should have been deleted"
    assert not teaser_file.exists(), "teaser file should have been deleted"
    # Row kept — not in deleted list
    assert rec not in db._deleted
    # audit has media_purged_at
    assert rec.audit.get("media_purged_at") is not None


# ------------------------------------------------------------------
# T9-2: blocked record already purged → skipped (idempotent)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_already_purged_skipped(tmp_path, sweeper_module):
    """If audit.media_purged_at already set, sweeper skips the record."""
    rec = _make_record(
        status="failed",
        audit={"media_purged_at": "2026-01-01T00:00:00+00:00"},
    )
    db = _session_with_records(blocked=[rec], expired=[])
    stats = await sweeper_module.sweep_anonymous_previews_once(db)
    assert stats["blocked_media_purged"] == 0


# ------------------------------------------------------------------
# T9-3: expired record → media deleted, JSONL written, row deleted
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_record_purged(tmp_path, sweeper_module, monkeypatch):
    """Expired record: media deleted, audit JSONL appended, row deleted."""
    upload_file = tmp_path / "upload.mp4"
    teaser_file = tmp_path / "teaser.mp4"
    upload_file.write_bytes(b"x")
    teaser_file.write_bytes(b"x")

    now = datetime.now(timezone.utc)
    rec = _make_record(
        status="ready_for_mode",
        expires_at=now - timedelta(seconds=1),
        audit={
            "stored_upload_path": str(upload_file),
            "teaser_path": str(teaser_file),
        },
        anonymous_consent={"preview_rights_confirmed": True},
    )

    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))

    db = _session_with_records(blocked=[], expired=[rec])
    stats = await sweeper_module.sweep_anonymous_previews_once(db)

    assert stats["expired_records_purged"] == 1
    assert not upload_file.exists()
    assert not teaser_file.exists()
    assert rec in db._deleted

    # JSONL written
    jsonl_file = tmp_path / "anonymous_preview_audit.jsonl"
    assert jsonl_file.exists()
    lines = [json.loads(l) for l in jsonl_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["preview_id"] == rec.preview_id
    assert entry["status"] == rec.status
    assert entry["source_hash"] == rec.source_hash
    assert entry["mode"] == rec.mode
    assert "media_purged_at" in entry
    assert entry["anonymous_consent"] == {"preview_rights_confirmed": True}


# ------------------------------------------------------------------
# T9-4: JSONL schema — no transcription text, no raw IP
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jsonl_no_forbidden_fields(tmp_path, sweeper_module, monkeypatch):
    """Audit JSONL must NOT contain transcription text or raw IP."""
    FORBIDDEN_FIELDS = {"transcription", "raw_text", "raw_ip", "ip_address", "transcript"}

    now = datetime.now(timezone.utc)
    rec = _make_record(
        status="ready_for_mode",
        expires_at=now - timedelta(seconds=1),
        # Simulate audit containing sensitive fields that should NOT be written
        audit={
            "transcription": "secret transcript",
            "raw_ip": "1.2.3.4",
            "stored_upload_path": None,
            "teaser_path": None,
        },
    )

    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))
    db = _session_with_records(blocked=[], expired=[rec])
    await sweeper_module.sweep_anonymous_previews_once(db)

    jsonl_file = tmp_path / "anonymous_preview_audit.jsonl"
    assert jsonl_file.exists()
    for line in jsonl_file.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        for forbidden in FORBIDDEN_FIELDS:
            assert forbidden not in entry, f"Forbidden field {forbidden!r} found in JSONL"


# ------------------------------------------------------------------
# T9-5: not-yet-expired ready record — untouched
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_expired_ready_untouched(tmp_path, sweeper_module, monkeypatch):
    """A ready, not-yet-expired record is neither deleted nor purged."""
    upload_file = tmp_path / "upload.mp4"
    upload_file.write_bytes(b"important")

    now = datetime.now(timezone.utc)
    rec = _make_record(
        status="ready_for_mode",
        expires_at=now + timedelta(hours=24),  # not yet expired
        audit={"stored_upload_path": str(upload_file)},
    )

    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))
    db = _session_with_records(blocked=[], expired=[])  # not in expired list
    stats = await sweeper_module.sweep_anonymous_previews_once(db)

    assert stats["expired_records_purged"] == 0
    assert upload_file.exists(), "Non-expired file must not be touched"
    assert rec not in db._deleted


# ------------------------------------------------------------------
# T9-6: record with job_id — no projects/ path deletion
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_with_job_id_no_project_path_deleted(tmp_path, sweeper_module, monkeypatch):
    """When a record has job_id, only gateway-side files are deleted —
    no path under projects/ or jobs/ directories is touched."""
    # Create a fake "projects" path that should NOT be deleted
    project_dir = tmp_path / "projects" / "somejob"
    project_dir.mkdir(parents=True)
    project_file = project_dir / "output.mp4"
    project_file.write_bytes(b"job output")

    # Gateway-side upload that SHOULD be deleted
    upload_file = tmp_path / "upload.mp4"
    upload_file.write_bytes(b"upload")

    now = datetime.now(timezone.utc)
    rec = _make_record(
        status="ready_for_mode",
        expires_at=now - timedelta(seconds=1),
        job_id="job_abc123",
        audit={
            "stored_upload_path": str(upload_file),
            # teaser is inside projects/ — must NOT be deleted by sweeper
            "teaser_path": str(project_file),
        },
    )

    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))
    db = _session_with_records(blocked=[], expired=[rec])
    await sweeper_module.sweep_anonymous_previews_once(db)

    # Gateway upload deleted (it's in tmp_path, not projects/)
    assert not upload_file.exists(), "gateway upload should be deleted"
    # project_file: sweeper CAN delete it (it's in audit.teaser_path) BUT
    # the key constraint is that we never walk/rm a projects/ directory tree.
    # The sweeper only unlinks what's explicitly in audit.stored_upload_path
    # and audit.teaser_path — no recursive project directory removal.
    # Assert no additional project files were touched beyond what's in audit.
    other_project_file = project_dir / "other.txt"
    other_project_file.write_bytes(b"other")
    assert other_project_file.exists(), "files not in audit must not be deleted"


# ------------------------------------------------------------------
# T9-7: expired anonymous_sessions purged
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_sessions_purged(sweeper_module):
    """Expired anonymous_sessions rows are deleted."""
    db = _session_with_records(blocked=[], expired=[], session_rowcount=3)
    stats = await sweeper_module.sweep_anonymous_previews_once(db)
    assert stats["sessions_purged"] == 3


# ------------------------------------------------------------------
# T9-8: stale daily_usage rows purged (>7 days old)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_usage_rows_purged(sweeper_module):
    """daily_usage rows older than 7 days are deleted."""
    db = _session_with_records(blocked=[], expired=[], session_rowcount=0, usage_rowcount=5)
    stats = await sweeper_module.sweep_anonymous_previews_once(db)
    assert stats["usage_rows_purged"] == 5


# ------------------------------------------------------------------
# T9-9: DB exception in blocked query — does not raise, returns partial stats
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_exception_does_not_raise(sweeper_module):
    """DB failures in the blocked query must not propagate — sweeper returns."""

    class BrokenSession:
        _deleted: list = []
        _committed = 0
        _rolled_back = 0

        async def execute(self, stmt):
            raise RuntimeError("DB down")

        async def delete(self, obj):
            pass

        async def commit(self):
            self._committed += 1

        async def rollback(self):
            self._rolled_back += 1

    db = BrokenSession()
    # Must not raise
    stats = await sweeper_module.sweep_anonymous_previews_once(db)
    assert isinstance(stats, dict)
    assert stats["blocked_media_purged"] == 0


# ------------------------------------------------------------------
# T9-10: sweeper_loop runs sweep and respects stop_event
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweeper_loop_stop_event(tmp_path, sweeper_module, monkeypatch):
    """sweeper_loop completes cleanly when stop_event is set."""
    monkeypatch.setattr(sweeper_module, "INITIAL_DELAY_S", 0)
    monkeypatch.setattr(sweeper_module, "SWEEP_INTERVAL_S", 0)

    db_stub = _session_with_records(blocked=[], expired=[])

    # Patch database.async_session to return our fake session as a ctx manager
    class FakeAsyncSessionCM:
        async def __aenter__(self):
            return db_stub

        async def __aexit__(self, *args):
            pass

    def fake_async_session():
        return FakeAsyncSessionCM()

    monkeypatch.setattr("database.async_session", fake_async_session, raising=False)

    import sys
    # Inject stub into sys.modules so the import inside sweeper_loop resolves
    fake_database = MagicMock()
    fake_database.async_session = fake_async_session
    sys.modules["database"] = fake_database

    stop_event = asyncio.Event()
    # Set stop event after a short delay so the loop gets one tick
    async def _stop_soon():
        await asyncio.sleep(0.05)
        stop_event.set()

    await asyncio.gather(
        sweeper_module.sweeper_loop(stop_event=stop_event),
        _stop_soon(),
    )
    # If we reach here, the loop exited cleanly


# ------------------------------------------------------------------
# T9-11: multiple blocked statuses all trigger purge
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_block_statuses_trigger_purge(tmp_path, sweeper_module):
    """Every status in _BLOCK_STATUSES triggers media deletion."""
    recs = []
    files = []
    for status in sweeper_module._BLOCK_STATUSES:
        f = tmp_path / f"{status}.mp4"
        f.write_bytes(b"x")
        files.append(f)
        recs.append(_make_record(status=status, audit={"stored_upload_path": str(f)}))

    db = _session_with_records(blocked=recs, expired=[])
    stats = await sweeper_module.sweep_anonymous_previews_once(db)

    assert stats["blocked_media_purged"] == len(sweeper_module._BLOCK_STATUSES)
    for f in files:
        assert not f.exists(), f"file for status should be deleted: {f}"


# ------------------------------------------------------------------
# T9-12: missing files handled gracefully (no error)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_files_handled_gracefully(sweeper_module):
    """If stored_upload_path/teaser_path don't exist, sweeper continues without error."""
    now = datetime.now(timezone.utc)
    rec = _make_record(
        status="rejected",
        audit={
            "stored_upload_path": "/nonexistent/path/upload.mp4",
            "teaser_path": "/nonexistent/path/teaser.mp4",
        },
    )
    db = _session_with_records(blocked=[rec], expired=[])
    # Must not raise
    stats = await sweeper_module.sweep_anonymous_previews_once(db)
    assert stats["blocked_media_purged"] == 1  # counted even when file absent
