"""Tests for V3-2 credits read-only API endpoints.

Tests the credits_read module and its endpoint handlers using mock DB.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
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

from credits_read import get_my_credits, get_my_credits_ledger, estimate_job_credits
from credits_service import (
    GRANT_AMOUNTS,
    ensure_free_bucket,
    ensure_trial_bucket,
    ensure_subscription_bucket_from_v2,
    estimate_credits,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user(*, user_id=None, plan_code="free", trial_granted_at=None, trial_ends_at=None, role="user"):
    return SimpleNamespace(
        id=user_id or uuid.uuid4(),
        plan_code=plan_code,
        role=role,
        trial_granted_at=trial_granted_at,
        trial_ends_at=trial_ends_at,
    )


def _make_bucket(
    *,
    bucket_type="free",
    granted=500,
    remaining=400,
    reserved=50,
    expires_at=None,
    source_label=None,
    user_id=None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        bucket_type=bucket_type,
        granted=granted,
        remaining=remaining,
        reserved=reserved,
        expires_at=expires_at,
        source_label=source_label,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_ledger_entry(*, direction="grant", credits_delta=500, balance_after=500, job_id=None, reason_code="grant"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        direction=direction,
        credits_delta=credits_delta,
        balance_after=balance_after,
        related_job_id=job_id,
        reason_code=reason_code,
        created_at=datetime.now(timezone.utc),
    )


# ===================================================================
# GET /api/me/credits
# ===================================================================


class TestGetMyCredits:
    def test_unauthenticated_returns_401(self):
        db = AsyncMock()
        with pytest.raises(Exception) as exc_info:
            _run(get_my_credits(db=db, user=None))
        assert "401" in str(exc_info.value.status_code)

    def test_returns_total_and_buckets(self):
        uid = uuid.uuid4()
        user = _make_user(user_id=uid)
        free_bucket = _make_bucket(bucket_type="free", remaining=400, reserved=50, user_id=uid)
        sub_bucket = _make_bucket(bucket_type="subscription", remaining=3000, reserved=100, user_id=uid)

        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [free_bucket, sub_bucket]
        db.execute = AsyncMock(return_value=result)

        resp = _run(get_my_credits(db=db, user=user))

        assert resp["total_available"] == (400 - 50) + (3000 - 100)  # 3250
        assert len(resp["buckets"]) == 2
        assert resp["buckets"][0]["type"] == "free"
        assert resp["buckets"][0]["remaining"] == 400
        assert resp["buckets"][0]["reserved"] == 50
        assert resp["in_trial"] is False
        assert resp["trial_expires_at"] is None

    def test_expired_buckets_excluded(self):
        uid = uuid.uuid4()
        user = _make_user(user_id=uid)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        expired_bucket = _make_bucket(
            bucket_type="trial", remaining=200, reserved=0,
            expires_at=past, user_id=uid,
        )
        active_bucket = _make_bucket(
            bucket_type="free", remaining=500, reserved=0, user_id=uid,
        )

        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [expired_bucket, active_bucket]
        db.execute = AsyncMock(return_value=result)

        resp = _run(get_my_credits(db=db, user=user))

        assert len(resp["buckets"]) == 1
        assert resp["buckets"][0]["type"] == "free"
        assert resp["total_available"] == 500

    def test_trial_info_populated(self):
        uid = uuid.uuid4()
        future = datetime.now(timezone.utc) + timedelta(days=5)
        user = _make_user(
            user_id=uid,
            trial_granted_at=datetime.now(timezone.utc) - timedelta(days=2),
            trial_ends_at=future,
        )
        trial_bucket = _make_bucket(
            bucket_type="trial", remaining=200, reserved=0,
            expires_at=future, user_id=uid,
        )

        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [trial_bucket]
        db.execute = AsyncMock(return_value=result)

        resp = _run(get_my_credits(db=db, user=user))

        assert resp["in_trial"] is True
        assert resp["trial_expires_at"] is not None

    def test_empty_buckets_returns_zero(self):
        user = _make_user()
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        resp = _run(get_my_credits(db=db, user=user))

        assert resp["total_available"] == 0
        assert resp["buckets"] == []

    def test_admin_lazy_ensure_uses_admin_grant_bucket(self):
        uid = uuid.uuid4()
        user = _make_user(user_id=uid, role="admin")
        admin_bucket = _make_bucket(
            bucket_type="manual_adjustment",
            granted=1_000_000,
            remaining=1_000_000,
            reserved=0,
            source_label="admin_grant",
            user_id=uid,
        )

        with patch("credits_read.ensure_admin_credits_bucket", new_callable=AsyncMock, return_value=admin_bucket) as mock_admin:
            db = AsyncMock()
            result = MagicMock()
            result.scalars.return_value.all.return_value = [admin_bucket]
            db.execute = AsyncMock(return_value=result)

            resp = _run(get_my_credits(db=db, user=user))

        mock_admin.assert_awaited_once_with(db, uid)
        assert resp["total_available"] == 1_000_000
        assert resp["buckets"][0]["source_label"] == "admin_grant"


# ===================================================================
# GET /api/me/credits-ledger
# ===================================================================


class TestGetMyCreditsLedger:
    def test_unauthenticated_returns_401(self):
        db = AsyncMock()
        with pytest.raises(Exception) as exc_info:
            _run(get_my_credits_ledger(db=db, user=None))
        assert "401" in str(exc_info.value.status_code)

    def test_returns_entries(self):
        user = _make_user()
        e1 = _make_ledger_entry(direction="grant", credits_delta=500, balance_after=500)
        e2 = _make_ledger_entry(direction="reserve", credits_delta=-50, balance_after=450, job_id="job-1")

        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [e1, e2]
        db.execute = AsyncMock(return_value=result)

        resp = _run(get_my_credits_ledger(db=db, user=user, limit=50))

        assert resp["count"] == 2
        assert len(resp["entries"]) == 2
        assert resp["entries"][0]["direction"] == "grant"
        assert resp["entries"][1]["direction"] == "reserve"
        assert resp["entries"][1]["related_job_id"] == "job-1"

    def test_empty_ledger(self):
        user = _make_user()
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        resp = _run(get_my_credits_ledger(db=db, user=user, limit=50))

        assert resp["count"] == 0
        assert resp["entries"] == []


# ===================================================================
# GET /api/credits/estimate
# ===================================================================


class TestEstimateJobCredits:
    def test_express_standard(self):
        resp = _run(estimate_job_credits(minutes=5.0, service_mode="express", quality_tier="standard"))
        assert resp["estimated_credits"] == 50  # 5 * 10
        assert resp["minutes"] == 5.0
        assert resp["service_mode"] == "express"

    def test_studio_high(self):
        resp = _run(estimate_job_credits(minutes=2.0, service_mode="studio", quality_tier="high"))
        assert resp["estimated_credits"] == 60  # 2 * 30

    def test_zero_minutes(self):
        resp = _run(estimate_job_credits(minutes=0.0, service_mode="express", quality_tier="standard"))
        assert resp["estimated_credits"] == 0


# ===================================================================
# Live grant path tests (V3-2 subscription-read-gap follow-up)
# ===================================================================


class TestEnsureFreeBucket:
    def test_creates_bucket_when_none_exists(self):
        uid = uuid.uuid4()
        db = AsyncMock()
        db.flush = AsyncMock()
        # First query: check existing → None
        no_result = MagicMock()
        no_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=no_result)
        added = []
        db.add = lambda obj: added.append(obj)

        bucket = _run(ensure_free_bucket(db, uid))

        assert bucket is not None
        assert bucket.bucket_type == "free"
        assert bucket.granted == GRANT_AMOUNTS["free"]
        assert bucket.remaining == GRANT_AMOUNTS["free"]

    def test_returns_existing_bucket_idempotent(self):
        uid = uuid.uuid4()
        existing = _make_bucket(bucket_type="free", granted=500, remaining=400, user_id=uid)
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute = AsyncMock(return_value=result)

        bucket = _run(ensure_free_bucket(db, uid))

        assert bucket is existing
        # flush should NOT have been called (no new bucket)
        db.flush.assert_not_awaited()


class TestEnsureTrialBucket:
    def test_creates_trial_bucket_when_none_exists(self):
        uid = uuid.uuid4()
        trial_ends = datetime.now(timezone.utc) + timedelta(days=5)
        db = AsyncMock()
        db.flush = AsyncMock()
        no_result = MagicMock()
        no_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=no_result)
        added = []
        db.add = lambda obj: added.append(obj)

        bucket = _run(ensure_trial_bucket(db, uid, trial_ends))

        assert bucket is not None
        assert bucket.bucket_type == "trial"
        assert bucket.granted == GRANT_AMOUNTS["trial"]
        assert bucket.expires_at == trial_ends

    def test_returns_existing_trial_bucket(self):
        uid = uuid.uuid4()
        existing = _make_bucket(bucket_type="trial", granted=300, remaining=200, user_id=uid)
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        db.execute = AsyncMock(return_value=result)

        bucket = _run(ensure_trial_bucket(db, uid, None))

        assert bucket is existing


class TestEnsureSubscriptionBucketFromV2:
    def test_creates_subscription_bucket_for_active_sub(self):
        uid = uuid.uuid4()
        sub_id = uuid.uuid4()
        period_end = datetime.now(timezone.utc) + timedelta(days=25)
        active_sub = SimpleNamespace(
            id=sub_id, user_id=uid, plan_code="plus", status="active",
            current_period_end=period_end,
        )

        db = AsyncMock()
        db.flush = AsyncMock()
        call_n = {"n": 0}

        async def smart_execute(*args, **kwargs):
            call_n["n"] += 1
            r = MagicMock()
            if call_n["n"] == 1:
                # Subscription query → active sub found
                r.scalar_one_or_none.return_value = active_sub
            elif call_n["n"] == 2:
                # Check existing bucket for this subscription → None
                r.scalar_one_or_none.return_value = None
            else:
                r.scalar_one_or_none.return_value = None
            return r

        db.execute = smart_execute
        added = []
        db.add = lambda obj: added.append(obj)

        bucket = _run(ensure_subscription_bucket_from_v2(db, uid))

        assert bucket is not None
        assert bucket.bucket_type == "subscription"
        assert bucket.granted == GRANT_AMOUNTS["plus"]
        assert bucket.expires_at == period_end

    def test_idempotent_returns_existing_for_same_subscription(self):
        uid = uuid.uuid4()
        sub_id = uuid.uuid4()
        active_sub = SimpleNamespace(
            id=sub_id, user_id=uid, plan_code="plus", status="active",
            current_period_end=datetime.now(timezone.utc) + timedelta(days=25),
        )
        existing_bucket = _make_bucket(
            bucket_type="subscription", granted=3500, remaining=3000, user_id=uid,
        )

        db = AsyncMock()
        call_n = {"n": 0}

        async def smart_execute(*args, **kwargs):
            call_n["n"] += 1
            r = MagicMock()
            if call_n["n"] == 1:
                r.scalar_one_or_none.return_value = active_sub
            elif call_n["n"] == 2:
                r.scalar_one_or_none.return_value = existing_bucket
            return r

        db.execute = smart_execute

        bucket = _run(ensure_subscription_bucket_from_v2(db, uid))

        assert bucket is existing_bucket
        # No flush means no new bucket was created
        db.flush.assert_not_awaited()

    def test_no_active_subscription_returns_none(self):
        uid = uuid.uuid4()
        db = AsyncMock()
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=r)

        bucket = _run(ensure_subscription_bucket_from_v2(db, uid))

        assert bucket is None


class TestGetMyCreditsLiveGrant:
    """Test that get_my_credits calls ensure_* functions to create buckets."""

    def test_free_user_no_buckets_gets_free_bucket_via_lazy_ensure(self):
        uid = uuid.uuid4()
        user = _make_user(user_id=uid, plan_code="free")
        free_bucket = _make_bucket(bucket_type="free", granted=500, remaining=500, reserved=0, user_id=uid)

        with patch("credits_read.ensure_free_bucket", new_callable=AsyncMock, return_value=free_bucket) as mock_free, \
             patch("credits_read.ensure_trial_bucket", new_callable=AsyncMock) as mock_trial, \
             patch("credits_read.ensure_subscription_bucket_from_v2", new_callable=AsyncMock, return_value=None) as mock_sub:

            db = AsyncMock()
            db.commit = AsyncMock()
            result = MagicMock()
            result.scalars.return_value.all.return_value = [free_bucket]
            db.execute = AsyncMock(return_value=result)

            resp = _run(get_my_credits(db=db, user=user))

            mock_free.assert_awaited_once_with(db, uid)
            mock_sub.assert_awaited_once_with(db, uid)
            assert resp["total_available"] == 500
            assert len(resp["buckets"]) == 1
            assert resp["buckets"][0]["type"] == "free"

    def test_paid_user_no_buckets_gets_subscription_bucket_via_lazy_ensure(self):
        uid = uuid.uuid4()
        user = _make_user(user_id=uid, plan_code="plus")
        free_bucket = _make_bucket(bucket_type="free", granted=500, remaining=500, reserved=0, user_id=uid)
        sub_bucket = _make_bucket(bucket_type="subscription", granted=3500, remaining=3500, reserved=0, user_id=uid)

        with patch("credits_read.ensure_free_bucket", new_callable=AsyncMock, return_value=free_bucket), \
             patch("credits_read.ensure_trial_bucket", new_callable=AsyncMock), \
             patch("credits_read.ensure_subscription_bucket_from_v2", new_callable=AsyncMock, return_value=sub_bucket) as mock_sub:

            db = AsyncMock()
            db.commit = AsyncMock()
            result = MagicMock()
            result.scalars.return_value.all.return_value = [free_bucket, sub_bucket]
            db.execute = AsyncMock(return_value=result)

            resp = _run(get_my_credits(db=db, user=user))

            mock_sub.assert_awaited_once_with(db, uid)
            assert resp["total_available"] == 500 + 3500
            sub_in_resp = [b for b in resp["buckets"] if b["type"] == "subscription"]
            assert len(sub_in_resp) == 1
            assert sub_in_resp[0]["granted"] == 3500
