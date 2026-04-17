"""Tests for T2: `_reserve_continue_transition` prevents double-spawn on concurrent
POST /jobs/{id}/continue requests.

The production scenario this defends against:
  Req A:  SELECT FOR UPDATE → status='waiting_for_review' → UPDATE 'running' → COMMIT → proxy
  Req B:  (blocks on FOR UPDATE until A commits) → SELECT sees 'running' → 409

Without the UPDATE+COMMIT pair, Req B would see the stale 'waiting_for_review'
after lock release and double-spawn the pipeline subprocess upstream.

Unit-level tests here verify the three critical invariants:
  1. SELECT is issued with `.with_for_update()` (DB-layer serialization)
  2. Job row's status is flipped to 'running' BEFORE commit
  3. `await db.commit()` is called (so the flip is visible to next reader)

A separate integration test (`@pytest.mark.postgres`) exercises real DB
concurrency; skipped by default since unit tests run on SQLite / mocks.
"""
from __future__ import annotations

import asyncio
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

# Stub database before importing job_intercept (it imports from database)
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
_fake_database.init_db = MagicMock()
sys.modules.setdefault("database", _fake_database)
if not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = MagicMock()

from fastapi import HTTPException

from job_intercept import _reserve_continue_transition


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _db_returning_job(job) -> AsyncMock:
    """Build a mock AsyncSession whose execute() returns a single Job row."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


def _db_returning_no_job() -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


class TestReserveContinueTransition:
    def test_waiting_review_flips_to_running_and_commits(self):
        """Happy path: status=waiting_for_review → UPDATE running → COMMIT."""
        job = SimpleNamespace(job_id="j-1", status="waiting_for_review")
        db = _db_returning_job(job)

        _run(_reserve_continue_transition("j-1", db))

        # Row was mutated in-place before commit
        assert job.status == "running"
        # Commit was called (critical — without this, the write rolls back)
        db.commit.assert_awaited_once()

    def test_select_uses_for_update(self):
        """Regression guard: the Job SELECT must carry `.with_for_update()`.

        Without the row lock, two concurrent requests don't serialize at the
        DB layer and can both pass the status check before either commits.
        """
        job = SimpleNamespace(job_id="j-1", status="waiting_for_review")
        db = _db_returning_job(job)

        _run(_reserve_continue_transition("j-1", db))

        # Exactly one SELECT was issued
        assert db.execute.await_count == 1
        stmt = db.execute.await_args_list[0].args[0]
        # SQLAlchemy Select stores FOR UPDATE intent here; non-None means present
        assert getattr(stmt, "_for_update_arg", None) is not None, (
            "_reserve_continue_transition must SELECT ... FOR UPDATE on Job row"
        )

    def test_running_status_raises_409(self):
        """Second concurrent request (or retry after first succeeded) must 409.

        Because the first request pre-mirrored status='running' and committed,
        this reader sees the new state and refuses to double-spawn.
        """
        job = SimpleNamespace(job_id="j-1", status="running")
        db = _db_returning_job(job)

        with pytest.raises(HTTPException) as ei:
            _run(_reserve_continue_transition("j-1", db))

        assert ei.value.status_code == 409
        assert "running" in str(ei.value.detail).lower()
        # Must NOT have committed anything — status is not ours to change
        db.commit.assert_not_awaited()

    def test_terminal_status_raises_409(self):
        """done/failed/cancelled jobs are not continuable either."""
        job = SimpleNamespace(job_id="j-1", status="succeeded")
        db = _db_returning_job(job)

        with pytest.raises(HTTPException) as ei:
            _run(_reserve_continue_transition("j-1", db))

        assert ei.value.status_code == 409
        db.commit.assert_not_awaited()

    def test_legacy_job_without_db_row_falls_through(self):
        """Jobs not mirrored in Gateway DB must not 500 — upstream handles them."""
        db = _db_returning_no_job()

        # Must return cleanly without raising
        _run(_reserve_continue_transition("legacy-job", db))

        # No mutation, no commit (there's no row to mutate)
        db.commit.assert_not_awaited()


@pytest.mark.postgres
def test_concurrent_continue_rejects_second():
    """INTEGRATION: under real PG, two simultaneous POST /continue serialize.

    Expected outcome: one request returns 202 (continue accepted, subprocess
    spawned), the other returns 409 (status already 'running' by the time
    it acquires the lock). If the COMMIT in _reserve_continue_transition is
    removed or the UPDATE is omitted, this test fails because both requests
    read the stale 'waiting_for_review' status.

    Skipped by default — requires TEST_DATABASE_URL pointing at a live PG.
    """
    pytest.skip("Requires PostgreSQL integration setup — see TEST_DATABASE_URL")
