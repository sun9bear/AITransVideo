"""Tests for Phase 3: Free user quota lifecycle.

Tests the real quota.py module and its integration with intercept_create_job.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from quota import check_quota, reserve_quota, commit_quota, release_quota, settle_job_quota  # noqa: E402
from job_intercept import intercept_create_job  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user(*, role="user", plan_code="free", quota_total=5, quota_used=0):
    return SimpleNamespace(
        id="uid-1", email="u@test.com", display_name="Test",
        role=role, plan_code=plan_code,
        free_jobs_quota_total=quota_total,
        free_jobs_quota_used=quota_used,
    )


def _make_job(*, quota_state="none", user_id="uid-1"):
    return SimpleNamespace(
        job_id="job-test-1", user_id=user_id, quota_state=quota_state,
    )


def _mock_db_returning(user_obj):
    """DB session that returns user_obj on select(User)."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user_obj
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


# ===================================================================
# quota.py unit tests — real module, mock only DB
# ===================================================================

class TestCheckQuota:
    def test_free_user_has_quota(self):
        user = _make_user(quota_total=5, quota_used=2)
        db = AsyncMock()
        ok, used, total = _run(check_quota(db, user))
        assert ok is True
        assert used == 2
        assert total == 5

    def test_free_user_exhausted(self):
        user = _make_user(quota_total=5, quota_used=5)
        db = AsyncMock()
        ok, used, total = _run(check_quota(db, user))
        assert ok is False
        assert used == 5

    def test_plus_user_always_has_quota(self):
        user = _make_user(plan_code="plus")
        db = AsyncMock()
        ok, _, _ = _run(check_quota(db, user))
        assert ok is True


class TestReserveQuota:
    def test_reserve_increments_quota_used(self):
        user = _make_user(quota_total=5, quota_used=2)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="none")

        ok = _run(reserve_quota(db, "uid-1", job))
        assert ok is True
        assert job.quota_state == "reserved"
        assert user.free_jobs_quota_used == 3

    def test_reserve_fails_when_exhausted(self):
        user = _make_user(quota_total=5, quota_used=5)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="none")

        ok = _run(reserve_quota(db, "uid-1", job))
        assert ok is False
        assert job.quota_state == "none"  # unchanged
        assert user.free_jobs_quota_used == 5  # unchanged

    def test_reserve_skips_if_already_reserved(self):
        user = _make_user(quota_total=5, quota_used=2)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="reserved")

        ok = _run(reserve_quota(db, "uid-1", job))
        assert ok is False
        assert user.free_jobs_quota_used == 2  # no double-deduct

    def test_reserve_non_free_user_marks_reserved_no_counter(self):
        user = _make_user(plan_code="plus", quota_used=0)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="none")

        ok = _run(reserve_quota(db, "uid-1", job))
        assert ok is True
        assert job.quota_state == "reserved"
        assert user.free_jobs_quota_used == 0  # counter untouched

    def test_reserve_admin_free_user_marks_reserved_no_counter(self):
        user = _make_user(role="admin", plan_code="free", quota_total=5, quota_used=5)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="none")

        ok = _run(reserve_quota(db, "uid-1", job))
        assert ok is True
        assert job.quota_state == "reserved"
        assert user.free_jobs_quota_used == 5  # admin should not consume free quota


class TestCommitQuota:
    def test_commit_from_reserved(self):
        db = AsyncMock()
        job = _make_job(quota_state="reserved")
        ok = _run(commit_quota(db, job))
        assert ok is True
        assert job.quota_state == "committed"

    def test_commit_from_none_is_noop(self):
        db = AsyncMock()
        job = _make_job(quota_state="none")
        ok = _run(commit_quota(db, job))
        assert ok is False
        assert job.quota_state == "none"

    def test_commit_from_released_is_noop(self):
        db = AsyncMock()
        job = _make_job(quota_state="released")
        ok = _run(commit_quota(db, job))
        assert ok is False
        assert job.quota_state == "released"

    def test_double_commit_is_noop(self):
        db = AsyncMock()
        job = _make_job(quota_state="committed")
        ok = _run(commit_quota(db, job))
        assert ok is False


class TestReleaseQuota:
    def test_release_decrements_quota(self):
        user = _make_user(quota_total=5, quota_used=3)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="reserved")

        ok = _run(release_quota(db, job))
        assert ok is True
        assert job.quota_state == "released"
        assert user.free_jobs_quota_used == 2

    def test_release_from_none_is_noop(self):
        user = _make_user(quota_used=3)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="none")

        ok = _run(release_quota(db, job))
        assert ok is False
        assert user.free_jobs_quota_used == 3

    def test_double_release_is_noop(self):
        user = _make_user(quota_used=3)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="released")

        ok = _run(release_quota(db, job))
        assert ok is False
        assert user.free_jobs_quota_used == 3

    def test_release_from_committed_is_noop(self):
        user = _make_user(quota_used=3)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="committed")

        ok = _run(release_quota(db, job))
        assert ok is False

    def test_release_does_not_go_below_zero(self):
        user = _make_user(quota_total=5, quota_used=0)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="reserved")

        ok = _run(release_quota(db, job))
        assert ok is True
        assert user.free_jobs_quota_used == 0  # clamped, not -1


class TestSettleJobQuota:
    def test_settle_succeeded_commits(self):
        db = AsyncMock()
        job = _make_job(quota_state="reserved")
        _run(settle_job_quota(db, job, "succeeded"))
        assert job.quota_state == "committed"

    def test_settle_failed_releases(self):
        user = _make_user(quota_used=3)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="reserved")
        _run(settle_job_quota(db, job, "failed"))
        assert job.quota_state == "released"
        assert user.free_jobs_quota_used == 2

    def test_settle_cancelled_releases(self):
        user = _make_user(quota_used=3)
        db = _mock_db_returning(user)
        job = _make_job(quota_state="reserved")
        _run(settle_job_quota(db, job, "cancelled"))
        assert job.quota_state == "released"

    def test_settle_already_committed_is_noop(self):
        db = AsyncMock()
        job = _make_job(quota_state="committed")
        _run(settle_job_quota(db, job, "succeeded"))
        assert job.quota_state == "committed"

    def test_settle_none_state_is_noop(self):
        db = AsyncMock()
        job = _make_job(quota_state="none")
        _run(settle_job_quota(db, job, "failed"))
        assert job.quota_state == "none"  # pre-quota job, don't touch


# ===================================================================
# Integration: intercept_create_job with quota
# ===================================================================

class TestCreateJobQuotaIntegration:
    def _make_request(self, body):
        req = MagicMock()
        req.body = AsyncMock(return_value=json.dumps(body).encode())
        req.headers = {"content-type": "application/json"}
        req.method = "POST"
        req.url = MagicMock(); req.url.path = "/job-api/jobs"
        req.query_params = {}
        return req

    def _make_db(self, user, *, active_count=0):
        """DB that supports both count queries and user lookups for quota.

        Query order (post 2026-04-21 display_name orchestrator):
          1. concurrency COUNT
          2. SELECT jobs.display_name (existing names, new)
          3. SELECT COUNT(*) display_name LIKE (branch4, new, conditional)
          4. existing-job check
          5. reserve_quota user lookup

        Queries 2 + 3 target ``jobs.display_name``; we dispatch by SQL
        content so reordering stays robust."""
        db = AsyncMock()
        call_n = {"n": 0}

        count_result = MagicMock()
        count_result.scalar.return_value = active_count

        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = user

        no_job_result = MagicMock()
        no_job_result.scalar_one_or_none.return_value = None

        names_result = MagicMock()
        names_result.all.return_value = []  # no existing display_names

        branch4_result = MagicMock()
        branch4_result.scalar.return_value = 0  # no branch-4 names today

        async def smart_execute(stmt, *args, **kwargs):
            sql_text = str(stmt).lower()
            # Dispatch by unique WHERE clauses; ``select(Job)`` also
            # contains the column ``jobs.display_name`` so we can't just
            # check for its presence.
            if "display_name is not null" in sql_text:
                return names_result
            if "display_name like" in sql_text:
                return branch4_result
            call_n["n"] += 1
            if call_n["n"] == 1:
                return count_result  # concurrency count
            if call_n["n"] == 2:
                return no_job_result  # existing job check
            return user_result  # reserve_quota user lookup

        db.execute = smart_execute
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        return db

    def test_free_user_quota_exhausted_returns_403(self):
        user = _make_user(quota_total=5, quota_used=5)
        req = self._make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
        })
        db = self._make_db(user, active_count=0)

        with patch("job_intercept.proxy_request", new_callable=AsyncMock):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                resp = _run(intercept_create_job(req, db, user))

        body = json.loads(resp.body)
        assert resp.status_code == 403
        assert body["error"] == "quota_exhausted"
        assert body["detail"]["free_jobs_quota_used"] == 5
        assert body["detail"]["free_jobs_quota_total"] == 5

    def test_free_user_with_quota_succeeds_and_reserves(self):
        user = _make_user(quota_total=5, quota_used=2)
        req = self._make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
        })
        db = self._make_db(user, active_count=0)

        success_body = json.dumps({"job_id": "job_abc", "status": "queued"}).encode()

        async def fake_proxy(**kw):
            from fastapi import Response as FR
            return FR(content=success_body, status_code=202,
                      headers={"content-type": "application/json"})

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        # User quota should have been incremented by reserve_quota
        assert user.free_jobs_quota_used == 3

    def test_plus_user_skips_quota_check(self):
        user = _make_user(plan_code="plus")
        req = self._make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
        })
        db = self._make_db(user, active_count=0)

        success_body = json.dumps({"job_id": "job_plus", "status": "queued"}).encode()

        async def fake_proxy(**kw):
            from fastapi import Response as FR
            return FR(content=success_body, status_code=202,
                      headers={"content-type": "application/json"})

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        # Plus user quota counter stays 0
        assert user.free_jobs_quota_used == 0

    def test_admin_free_user_with_exhausted_quota_still_succeeds(self):
        user = _make_user(role="admin", plan_code="free", quota_total=5, quota_used=5)
        req = self._make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
        })
        db = self._make_db(user, active_count=0)

        success_body = json.dumps({"job_id": "job_admin", "status": "queued"}).encode()

        async def fake_proxy(**kw):
            from fastapi import Response as FR
            return FR(content=success_body, status_code=202,
                      headers={"content-type": "application/json"})

        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                resp = _run(intercept_create_job(req, db, user))

        assert resp.status_code == 202
        assert user.free_jobs_quota_used == 5  # unchanged for admin

    def test_reserve_failure_after_upstream_success_returns_quota_exhausted(self):
        """If upstream creates the job but reserve_quota fails, Gateway returns 403."""
        # User has 5/5 used — but check_quota passes because we set used=4 initially,
        # then simulate reserve_quota finding exhausted on its own re-read.
        user = _make_user(quota_total=5, quota_used=4)
        req = self._make_request({
            "service_mode": "express",
            "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
        })

        # Build a DB mock where:
        #   legacy call 1: concurrency count → 0 (OK)
        #   legacy call 2: existing-job lookup → None
        #   legacy call 3: reserve_quota user lookup → user with used=5 (exhausted)
        # Display-name orchestrator queries (dispatched by SQL content, not
        # call index) return "no existing names, 0 branch-4 names today" so
        # they don't interfere with the exhaustion simulation.
        exhausted_user = _make_user(quota_total=5, quota_used=5)
        db = AsyncMock()
        count_result = MagicMock(); count_result.scalar.return_value = 0
        no_job_result = MagicMock(); no_job_result.scalar_one_or_none.return_value = None
        exhausted_result = MagicMock(); exhausted_result.scalar_one_or_none.return_value = exhausted_user
        names_result = MagicMock(); names_result.all.return_value = []
        branch4_result = MagicMock(); branch4_result.scalar.return_value = 0
        call_n = {"n": 0}

        async def smart_execute(stmt, *args, **kwargs):
            sql_text = str(stmt).lower()
            if "display_name is not null" in sql_text:
                return names_result
            if "display_name like" in sql_text:
                return branch4_result
            call_n["n"] += 1
            if call_n["n"] == 1: return count_result
            if call_n["n"] == 2: return no_job_result
            return exhausted_result

        db.execute = smart_execute
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        success_body = json.dumps({"job_id": "job_race", "status": "queued"}).encode()

        async def fake_proxy(**kw):
            from fastapi import Response as FR
            return FR(content=success_body, status_code=202,
                      headers={"content-type": "application/json"})

        compensate_mock = AsyncMock()
        with patch("job_intercept.proxy_request", side_effect=fake_proxy):
            with patch("job_intercept._probe_youtube_metadata", return_value=None):
                with patch("job_intercept._compensate_upstream_job", compensate_mock):
                    resp = _run(intercept_create_job(req, db, user))

        body = json.loads(resp.body)
        assert resp.status_code == 403
        assert body["error"] == "quota_exhausted"
        # DB rollback should have been called
        db.rollback.assert_awaited()
        # Upstream compensation must have been called with the orphan job_id
        compensate_mock.assert_awaited_once_with("job_race")


class TestAdminDeleteReleasesQuota:
    """Admin delete_job must release reserved quota before removing the row."""

    def test_admin_delete_releases_reserved_quota(self):
        from quota import release_quota

        user = _make_user(quota_total=5, quota_used=3)
        job = _make_job(quota_state="reserved", user_id="uid-1")

        db = _mock_db_returning(user)
        # release_quota reads the job, then the user
        ok = _run(release_quota(db, job))
        assert ok is True
        assert job.quota_state == "released"
        assert user.free_jobs_quota_used == 2

    def test_admin_delete_committed_job_no_refund(self):
        from quota import release_quota

        user = _make_user(quota_total=5, quota_used=3)
        job = _make_job(quota_state="committed", user_id="uid-1")

        db = _mock_db_returning(user)
        ok = _run(release_quota(db, job))
        assert ok is False
        assert job.quota_state == "committed"
        assert user.free_jobs_quota_used == 3  # unchanged
