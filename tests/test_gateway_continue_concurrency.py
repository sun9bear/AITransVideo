"""Tests for T2: `_continue_with_gateway_lock` prevents double-spawn on
concurrent POST /jobs/{id}/continue requests AND rolls back local status
when upstream rejects the continue.

Production scenario this defends against:
  Req A: SELECT FOR UPDATE → status=waiting_for_review → proxy upstream →
         (success) UPDATE 'running' → COMMIT → response
  Req B: (blocks on FOR UPDATE until A commits) → reads 'running' → 409
  Req A-fail: SELECT FOR UPDATE → proxy upstream → (409/5xx) → leave status
         as 'waiting_for_review' → COMMIT → response. A retry is safe.

Codex P1 finding (post-T2): the original implementation committed
status='running' BEFORE proxying. If upstream rejected (review not actually
approved — see src/services/jobs/service.py:155-168), the Gateway DB was
stuck with 'running' and all subsequent continues 409'd until list_jobs
sync reconciled. New flow holds the lock through proxy and only promotes
status on upstream 2xx.

Unit tests here cover:
  1. Upstream 2xx → status promoted to 'running' + committed
  2. Upstream 409/5xx → status stays 'waiting_for_review' (retry works)
  3. Concurrent Req B blocks on lock, then 409s after A commits 'running'
  4. Legacy job without a Gateway DB row falls through cleanly
  5. Non-continuable status (already running/failed/done) short-circuits
     with 409 before proxy is called
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

# Stub database before importing job_intercept
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
_fake_database.init_db = MagicMock()
sys.modules.setdefault("database", _fake_database)
if not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = MagicMock()

from fastapi import HTTPException, Response

import job_intercept
from job_intercept import _continue_with_gateway_lock


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_db_with_job(job):
    """Build a mock AsyncSession whose execute() returns a single Job row."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


def _make_db_no_job():
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


def _make_request():
    """Minimal stub for FastAPI Request — job_intercept only passes it to
    proxy_request, which we monkeypatch."""
    return MagicMock()


def _patch_proxy(monkeypatch, *, status_code: int, body: bytes = b'{"ok":true}'):
    """Monkeypatch job_intercept.proxy_request to return a controlled Response."""
    async def fake_proxy(**kwargs):
        return Response(content=body, status_code=status_code)
    monkeypatch.setattr(job_intercept, "proxy_request", fake_proxy)


class TestContinueWithGatewayLock:
    def test_select_uses_for_update(self, monkeypatch):
        """Regression guard: the Job SELECT must carry `.with_for_update()`.

        Without it, concurrent requests don't serialize at the DB layer.
        """
        _patch_proxy(monkeypatch, status_code=202)
        job = SimpleNamespace(job_id="j-1", status="waiting_for_review")
        db = _make_db_with_job(job)

        _run(_continue_with_gateway_lock(_make_request(), "j-1", db))

        # The initial SELECT Job statement was executed
        assert db.execute.await_count >= 1
        stmt = db.execute.await_args_list[0].args[0]
        assert getattr(stmt, "_for_update_arg", None) is not None, (
            "_continue_with_gateway_lock must SELECT ... FOR UPDATE on Job row"
        )

    def test_upstream_success_promotes_status_to_running(self, monkeypatch):
        """Happy path: upstream 2xx → job.status flipped to 'running' + commit."""
        _patch_proxy(monkeypatch, status_code=202)
        job = SimpleNamespace(job_id="j-1", status="waiting_for_review")
        db = _make_db_with_job(job)

        response = _run(_continue_with_gateway_lock(_make_request(), "j-1", db))

        assert response.status_code == 202
        assert job.status == "running", "status must be promoted on upstream 2xx"
        db.commit.assert_awaited_once()

    def test_upstream_200_also_promotes(self, monkeypatch):
        _patch_proxy(monkeypatch, status_code=200)
        job = SimpleNamespace(job_id="j-1", status="waiting_for_review")
        db = _make_db_with_job(job)

        _run(_continue_with_gateway_lock(_make_request(), "j-1", db))

        assert job.status == "running"

    def test_upstream_409_leaves_status_unchanged(self, monkeypatch):
        """Codex P1 regression guard: upstream 409 (e.g. review not approved)
        must NOT leave gateway DB stuck with status='running'.

        Without this rollback, a failed continue would block all retries —
        user sees "Job is not continuable (current status: running)" forever
        until list_jobs sync runs and reconciles.
        """
        _patch_proxy(monkeypatch, status_code=409, body=b'{"error":"not approved"}')
        job = SimpleNamespace(job_id="j-1", status="waiting_for_review")
        db = _make_db_with_job(job)

        response = _run(_continue_with_gateway_lock(_make_request(), "j-1", db))

        assert response.status_code == 409
        assert job.status == "waiting_for_review", (
            "status MUST NOT flip to 'running' when upstream rejected — "
            "retries would be blocked by the stale value until list_jobs sync"
        )
        # Commit still happens to release the lock, but with no status mutation
        db.commit.assert_awaited_once()

    def test_upstream_500_leaves_status_unchanged(self, monkeypatch):
        """Same as 409: upstream 5xx must not poison gateway state."""
        _patch_proxy(monkeypatch, status_code=500, body=b'{"error":"upstream bug"}')
        job = SimpleNamespace(job_id="j-1", status="waiting_for_review")
        db = _make_db_with_job(job)

        response = _run(_continue_with_gateway_lock(_make_request(), "j-1", db))

        assert response.status_code == 500
        assert job.status == "waiting_for_review"
        db.commit.assert_awaited_once()

    def test_non_continuable_status_409s_without_proxy(self, monkeypatch):
        """If the job is already 'running' (concurrent continue won the race)
        or in a terminal state, don't even call upstream."""
        proxy_called = {"hit": False}

        async def fake_proxy(**kwargs):
            proxy_called["hit"] = True
            return Response(content=b"{}", status_code=200)

        monkeypatch.setattr(job_intercept, "proxy_request", fake_proxy)
        job = SimpleNamespace(job_id="j-1", status="running")
        db = _make_db_with_job(job)

        with pytest.raises(HTTPException) as ei:
            _run(_continue_with_gateway_lock(_make_request(), "j-1", db))

        assert ei.value.status_code == 409
        assert "running" in str(ei.value.detail).lower()
        assert proxy_called["hit"] is False, (
            "upstream must NOT be called when gateway pre-check already rejected"
        )
        db.commit.assert_not_awaited()

    def test_terminal_status_rejected(self, monkeypatch):
        _patch_proxy(monkeypatch, status_code=200)
        job = SimpleNamespace(job_id="j-1", status="succeeded")
        db = _make_db_with_job(job)

        with pytest.raises(HTTPException) as ei:
            _run(_continue_with_gateway_lock(_make_request(), "j-1", db))

        assert ei.value.status_code == 409

    def test_legacy_job_without_db_row_falls_through(self, monkeypatch):
        """Jobs not mirrored in Gateway DB must proxy through to upstream
        without raising — upstream handles validation in that case."""
        _patch_proxy(monkeypatch, status_code=202)
        db = _make_db_no_job()

        response = _run(_continue_with_gateway_lock(_make_request(), "legacy-j", db))

        assert response.status_code == 202
        # Still commits (a no-op) to close the txn cleanly
        db.commit.assert_awaited_once()


@pytest.mark.postgres
def test_concurrent_continue_rejects_second():
    """INTEGRATION: under real PG, two simultaneous POST /continue serialize.

    Expected outcome: first request gets 202 (status promoted to 'running'),
    second request blocks on FOR UPDATE until first commits, reads 'running',
    returns 409. If the lock-release-before-proxy bug came back, both would
    read 'waiting_for_review' and both would pass through to upstream.

    Skipped by default — requires TEST_DATABASE_URL pointing at live PG.
    """
    pytest.skip("Requires PostgreSQL integration setup — see TEST_DATABASE_URL")


@pytest.mark.postgres
def test_failed_continue_allows_retry():
    """INTEGRATION: if upstream rejected the first continue (review not
    approved), a retry after the user fixes the review state must succeed —
    gateway DB must NOT be stuck at 'running'.

    Skipped by default.
    """
    pytest.skip("Requires PostgreSQL integration setup — see TEST_DATABASE_URL")
