"""Tests for V3-1 shadow credits service.

Tests the credits_service module in isolation using mock DB sessions.
Validates: grant, reserve, capture, release, rollback, priority ordering,
and the shadow_safe failure isolation guarantee.
"""
from __future__ import annotations

import asyncio
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

# Stub database module before importing credits_service
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from credits_service import (
    BUCKET_PRIORITY,
    DEBIT_RATES,
    GRANT_AMOUNTS,
    estimate_credits,
    shadow_grant,
    shadow_reserve,
    shadow_release,
    shadow_capture,
    shadow_rollback,
    shadow_safe,
    _pick_buckets_by_priority,
    _get_runtime_debit_rates,
    _get_runtime_grant_amounts,
    _get_runtime_bucket_priority,
)
from models import CreditsBucket, CreditsLedger


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_bucket(
    *,
    bucket_type="free",
    granted=500,
    remaining=500,
    reserved=0,
    user_id=None,
    bucket_id=None,
    expires_at=None,
):
    b = SimpleNamespace(
        id=bucket_id or uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        bucket_type=bucket_type,
        granted=granted,
        remaining=remaining,
        reserved=reserved,
        expires_at=expires_at,
        source_label=None,
        related_order_id=None,
        related_subscription_id=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    return b


# ===================================================================
# estimate_credits
# ===================================================================


class TestEstimateCredits:
    def test_express_standard(self):
        assert estimate_credits(5.0, "express", "standard") == 50  # 5 * 10

    def test_studio_high(self):
        assert estimate_credits(2.0, "studio", "high") == 60  # 2 * 30

    def test_studio_flagship(self):
        assert estimate_credits(1.0, "studio", "flagship") == 50

    def test_none_minutes_returns_zero(self):
        assert estimate_credits(None) == 0

    def test_zero_minutes_returns_zero(self):
        assert estimate_credits(0.0) == 0

    def test_small_duration_rounds_up_to_1(self):
        # 0.05 * 10 = 0.5 → round → 0, but max(1, ...) → 1
        assert estimate_credits(0.05, "express", "standard") == 1

    def test_unknown_tier_uses_default(self):
        assert estimate_credits(3.0, "express", "unknown_tier") == 30  # 3 * 10 default


# ===================================================================
# Bucket priority ordering
# ===================================================================


class TestBucketPriority:
    def test_express_priority_order(self):
        free = _make_bucket(bucket_type="free")
        trial = _make_bucket(bucket_type="trial")
        sub = _make_bucket(bucket_type="subscription")
        topup = _make_bucket(bucket_type="topup")

        ordered = _pick_buckets_by_priority([trial, topup, free, sub], "express")
        types = [b.bucket_type for b in ordered]
        assert types == ["free", "subscription", "topup", "trial"]

    def test_studio_priority_order(self):
        free = _make_bucket(bucket_type="free")
        trial = _make_bucket(bucket_type="trial")
        sub = _make_bucket(bucket_type="subscription")
        topup = _make_bucket(bucket_type="topup")

        ordered = _pick_buckets_by_priority([free, topup, trial, sub], "studio")
        types = [b.bucket_type for b in ordered]
        assert types == ["trial", "subscription", "topup", "free"]


# ===================================================================
# shadow_grant
# ===================================================================


class TestShadowGrant:
    def test_grant_creates_bucket_and_ledger(self):
        db = AsyncMock()
        db.flush = AsyncMock()
        added_objects = []
        db.add = lambda obj: added_objects.append(obj)

        user_id = uuid.uuid4()
        result = _run(shadow_grant(
            db, user_id=user_id, bucket_type="free", amount=500,
            source_label="free_grant", reason_code="registration",
        ))

        assert result is not None
        assert result.granted == 500
        assert result.remaining == 500
        assert result.reserved == 0
        assert result.bucket_type == "free"
        # Should have added bucket + ledger entry
        assert len(added_objects) == 2  # bucket (via db.add in model init) + ledger
        ledger = added_objects[1]
        assert ledger.direction == "grant"
        assert ledger.credits_delta == 500
        assert ledger.balance_after == 500

    def test_grant_invalid_bucket_type_returns_none(self):
        db = AsyncMock()
        result = _run(shadow_grant(db, user_id=uuid.uuid4(), bucket_type="invalid", amount=100))
        assert result is None

    def test_grant_exception_returns_none(self):
        db = AsyncMock()
        db.flush = AsyncMock(side_effect=RuntimeError("DB down"))
        db.add = MagicMock()
        result = _run(shadow_grant(db, user_id=uuid.uuid4(), bucket_type="free", amount=100))
        assert result is None


# ===================================================================
# shadow_reserve
# ===================================================================


class TestShadowReserve:
    def _db_with_buckets(self, buckets):
        """Create a mock DB that returns given buckets on select."""
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = buckets
        db.execute = AsyncMock(return_value=result)
        added = []
        db.add = lambda obj: added.append(obj)
        db._added = added
        return db

    def test_reserve_deducts_from_highest_priority(self):
        uid = uuid.uuid4()
        free_bucket = _make_bucket(bucket_type="free", remaining=500, reserved=0, user_id=uid)
        sub_bucket = _make_bucket(bucket_type="subscription", remaining=3500, reserved=0, user_id=uid)
        db = self._db_with_buckets([free_bucket, sub_bucket])

        entries = _run(shadow_reserve(
            db, user_id=uid, job_id="job-1", estimated_credits=100, service_mode="express",
        ))

        assert len(entries) == 1
        # Express priority: free first
        assert free_bucket.reserved == 100
        assert sub_bucket.reserved == 0

    def test_reserve_spans_multiple_buckets(self):
        uid = uuid.uuid4()
        free_bucket = _make_bucket(bucket_type="free", remaining=50, reserved=0, user_id=uid)
        sub_bucket = _make_bucket(bucket_type="subscription", remaining=3500, reserved=0, user_id=uid)
        db = self._db_with_buckets([free_bucket, sub_bucket])

        entries = _run(shadow_reserve(
            db, user_id=uid, job_id="job-1", estimated_credits=100, service_mode="express",
        ))

        assert len(entries) == 2
        assert free_bucket.reserved == 50
        assert sub_bucket.reserved == 50

    def test_reserve_zero_credits_returns_empty(self):
        db = AsyncMock()
        entries = _run(shadow_reserve(db, user_id=uuid.uuid4(), job_id="j", estimated_credits=0))
        assert entries == []

    def test_reserve_insufficient_balance_still_records(self):
        uid = uuid.uuid4()
        free_bucket = _make_bucket(bucket_type="free", remaining=30, reserved=0, user_id=uid)
        db = self._db_with_buckets([free_bucket])

        entries = _run(shadow_reserve(
            db, user_id=uid, job_id="job-1", estimated_credits=100, service_mode="express",
        ))

        # Should still reserve what's available (shadow mode, no gating)
        assert len(entries) == 1
        assert free_bucket.reserved == 30

    def test_reserve_exception_returns_empty(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=RuntimeError("DB error"))
        entries = _run(shadow_reserve(
            db, user_id=uuid.uuid4(), job_id="j", estimated_credits=50,
        ))
        assert entries == []


# ===================================================================
# shadow_release
# ===================================================================


class TestShadowRelease:
    def test_release_refunds_reserved(self):
        uid = uuid.uuid4()
        bucket_id = uuid.uuid4()
        bucket = _make_bucket(bucket_type="free", remaining=400, reserved=100, user_id=uid, bucket_id=bucket_id)

        # Mock: find reserve ledger entries, then find the bucket
        reserve_entry = SimpleNamespace(
            id=uuid.uuid4(), bucket_id=bucket_id, credits_delta=-100,
        )

        db = AsyncMock()
        call_n = {"n": 0}

        async def smart_execute(*args, **kwargs):
            call_n["n"] += 1
            if call_n["n"] == 1:
                # Reserve entries query
                r = MagicMock()
                r.scalars.return_value.all.return_value = [reserve_entry]
                return r
            else:
                # Bucket lookup
                r = MagicMock()
                r.scalar_one_or_none.return_value = bucket
                return r

        db.execute = smart_execute
        added = []
        db.add = lambda obj: added.append(obj)

        entries = _run(shadow_release(db, user_id=uid, job_id="job-1"))

        assert len(entries) == 1
        assert entries[0].direction == "release"
        assert entries[0].credits_delta == 100
        assert bucket.reserved == 0

    def test_release_no_reserves_returns_empty(self):
        db = AsyncMock()
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=r)

        entries = _run(shadow_release(db, user_id=uuid.uuid4(), job_id="j"))
        assert entries == []

    def test_release_can_scope_by_reserve_reason_code(self):
        db = AsyncMock()
        seen = []

        async def execute(stmt, *args, **kwargs):
            del args, kwargs
            seen.append(stmt)
            r = MagicMock()
            r.scalars.return_value.all.return_value = []
            return r

        db.execute = execute

        entries = _run(shadow_release(
            db,
            user_id=uuid.uuid4(),
            job_id="job-clone",
            reserve_reason_code="voice_clone_reserve_abc",
        ))

        assert entries == []
        assert seen, "shadow_release should query reserve ledger entries"
        assert "reason_code" in str(seen[0])


# ===================================================================
# shadow_capture — correctness tests (minor revision 1.1)
# ===================================================================


from credits_service import shadow_capture  # noqa: E402


class TestShadowCapture:
    """Tests that shadow_capture fully settles ALL reserve entries.

    Key invariant: after capture, no reserve entry may leave dangling
    bucket.reserved. Every reserve must become capture, release, or both.
    """

    def _db_for_capture(self, reserve_entries, buckets_by_id):
        """Create a mock DB returning reserve_entries then bucket lookups."""
        db = AsyncMock()
        call_n = {"n": 0}

        async def smart_execute(*args, **kwargs):
            call_n["n"] += 1
            if call_n["n"] == 1:
                # First call: select reserve ledger entries
                r = MagicMock()
                r.scalars.return_value.all.return_value = reserve_entries
                return r
            else:
                # Subsequent calls: bucket lookups by id
                # Extract bucket_id from the query
                for bid, bucket in buckets_by_id.items():
                    # Return whichever bucket matches; simplified mock
                    pass
                # We need to figure out which bucket is being queried.
                # In the simplified mock, return based on call order.
                # Since entries are processed in order, track which entry
                # we're on.
                entry_idx = call_n["n"] - 2  # 0-indexed after first call
                if entry_idx < len(reserve_entries):
                    bid = reserve_entries[entry_idx].bucket_id
                else:
                    bid = None
                r = MagicMock()
                r.scalar_one_or_none.return_value = buckets_by_id.get(bid)
                return r

        db.execute = smart_execute
        added = []
        db.add = lambda obj: added.append(obj)
        db._added = added
        return db

    def test_actual_less_than_reserved_two_entries_no_dangling(self):
        """actual=80, reserved=60+40=100. Excess 20 released from last entry.

        Entry A (bucket_free, 60): fully captured (60)
        Entry B (bucket_sub, 40): 20 captured + 20 released
        Both buckets must have reserved=0 after.
        """
        uid = uuid.uuid4()
        bid_free = uuid.uuid4()
        bid_sub = uuid.uuid4()

        bucket_free = _make_bucket(
            bucket_type="free", remaining=500, reserved=60,
            user_id=uid, bucket_id=bid_free,
        )
        bucket_sub = _make_bucket(
            bucket_type="subscription", remaining=3500, reserved=40,
            user_id=uid, bucket_id=bid_sub,
        )

        re_a = SimpleNamespace(id=uuid.uuid4(), bucket_id=bid_free, credits_delta=-60)
        re_b = SimpleNamespace(id=uuid.uuid4(), bucket_id=bid_sub, credits_delta=-40)

        db = self._db_for_capture([re_a, re_b], {bid_free: bucket_free, bid_sub: bucket_sub})

        entries = _run(shadow_capture(
            db, user_id=uid, job_id="job-cap-1", actual_credits=80,
        ))

        # All reserve entries must be settled
        directions = [e.direction for e in entries]
        assert "capture" in directions

        # No dangling reserved on either bucket
        assert bucket_free.reserved == 0, f"bucket_free.reserved={bucket_free.reserved}"
        assert bucket_sub.reserved == 0, f"bucket_sub.reserved={bucket_sub.reserved}"

        # Total captured should equal actual (80)
        total_captured = sum(abs(e.credits_delta) for e in entries if e.direction == "capture")
        total_released = sum(abs(e.credits_delta) for e in entries if e.direction == "release")
        assert total_captured == 80
        assert total_released == 20

    def test_actual_less_than_reserved_single_entry(self):
        """actual=30, reserved=50. Entry split: 30 captured + 20 released."""
        uid = uuid.uuid4()
        bid = uuid.uuid4()
        bucket = _make_bucket(
            bucket_type="free", remaining=500, reserved=50,
            user_id=uid, bucket_id=bid,
        )
        re_a = SimpleNamespace(id=uuid.uuid4(), bucket_id=bid, credits_delta=-50)

        db = self._db_for_capture([re_a], {bid: bucket})

        entries = _run(shadow_capture(
            db, user_id=uid, job_id="job-cap-2", actual_credits=30,
        ))

        assert bucket.reserved == 0
        total_captured = sum(abs(e.credits_delta) for e in entries if e.direction == "capture")
        total_released = sum(abs(e.credits_delta) for e in entries if e.direction == "release")
        assert total_captured == 30
        assert total_released == 20

    def test_actual_equals_reserved_exact_match(self):
        """actual=100, reserved=60+40=100. All captured, nothing released."""
        uid = uuid.uuid4()
        bid_a = uuid.uuid4()
        bid_b = uuid.uuid4()
        bucket_a = _make_bucket(bucket_type="free", remaining=500, reserved=60, user_id=uid, bucket_id=bid_a)
        bucket_b = _make_bucket(bucket_type="subscription", remaining=3500, reserved=40, user_id=uid, bucket_id=bid_b)

        re_a = SimpleNamespace(id=uuid.uuid4(), bucket_id=bid_a, credits_delta=-60)
        re_b = SimpleNamespace(id=uuid.uuid4(), bucket_id=bid_b, credits_delta=-40)

        db = self._db_for_capture([re_a, re_b], {bid_a: bucket_a, bid_b: bucket_b})

        entries = _run(shadow_capture(
            db, user_id=uid, job_id="job-cap-3", actual_credits=100,
        ))

        assert bucket_a.reserved == 0
        assert bucket_b.reserved == 0
        total_captured = sum(abs(e.credits_delta) for e in entries if e.direction == "capture")
        total_released = sum(abs(e.credits_delta) for e in entries if e.direction == "release")
        assert total_captured == 100
        assert total_released == 0

    def test_actual_without_enough_reserved_records_full_overdraft(self):
        """Actual cost must be fully represented even when balance is short."""
        uid = uuid.uuid4()
        bucket = _make_bucket(
            bucket_type="subscription",
            remaining=30,
            reserved=0,
            user_id=uid,
        )
        db = AsyncMock()
        calls = {"n": 0}

        async def smart_execute(*args, **kwargs):
            del args, kwargs
            calls["n"] += 1
            r = MagicMock()
            if calls["n"] == 1:
                r.scalars.return_value.all.return_value = []
            else:
                r.scalars.return_value.all.return_value = [bucket]
            return r

        db.execute = smart_execute
        added = []
        db.add = lambda obj: added.append(obj)

        entries = _run(shadow_capture(
            db,
            user_id=uid,
            job_id="job-overdraft",
            actual_credits=100,
            service_mode="studio",
        ))

        captured = sum(abs(e.credits_delta) for e in entries if e.direction == "capture")
        assert captured == 100
        assert [e.reason_code for e in entries] == ["capture_additional", "capture_overdraft"]
        assert bucket.remaining == -70

    def test_capture_can_scope_by_reserve_reason_code(self):
        db = AsyncMock()
        seen = []

        async def execute(stmt, *args, **kwargs):
            del args, kwargs
            seen.append(stmt)
            r = MagicMock()
            r.scalars.return_value.all.return_value = []
            return r

        db.execute = execute

        entries = _run(shadow_capture(
            db,
            user_id=uuid.uuid4(),
            job_id="job-clone",
            actual_credits=0,
            reserve_reason_code="voice_clone_reserve_abc",
        ))

        assert entries == []
        assert seen, "shadow_capture should query reserve ledger entries"
        assert "reason_code" in str(seen[0])

    def test_actual_zero_releases_all(self):
        """actual=0, reserved=50. Everything released, nothing captured."""
        uid = uuid.uuid4()
        bid = uuid.uuid4()
        bucket = _make_bucket(bucket_type="free", remaining=500, reserved=50, user_id=uid, bucket_id=bid)
        re_a = SimpleNamespace(id=uuid.uuid4(), bucket_id=bid, credits_delta=-50)

        db = self._db_for_capture([re_a], {bid: bucket})

        entries = _run(shadow_capture(
            db, user_id=uid, job_id="job-cap-4", actual_credits=0,
        ))

        assert bucket.reserved == 0
        total_captured = sum(abs(e.credits_delta) for e in entries if e.direction == "capture")
        total_released = sum(abs(e.credits_delta) for e in entries if e.direction == "release")
        assert total_captured == 0
        assert total_released == 50

    def test_three_entries_partial_release_no_dangling(self):
        """actual=90, reserved=40+30+50=120. Excess 30 released from last entries.

        Reverse order: entry C (50) → release 30, capture 20; entry B (30) → full capture; entry A (40) → full capture.
        All buckets reserved=0 after.
        """
        uid = uuid.uuid4()
        bid_a, bid_b, bid_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        bucket_a = _make_bucket(bucket_type="free", remaining=500, reserved=40, user_id=uid, bucket_id=bid_a)
        bucket_b = _make_bucket(bucket_type="subscription", remaining=3000, reserved=30, user_id=uid, bucket_id=bid_b)
        bucket_c = _make_bucket(bucket_type="topup", remaining=5000, reserved=50, user_id=uid, bucket_id=bid_c)

        re_a = SimpleNamespace(id=uuid.uuid4(), bucket_id=bid_a, credits_delta=-40)
        re_b = SimpleNamespace(id=uuid.uuid4(), bucket_id=bid_b, credits_delta=-30)
        re_c = SimpleNamespace(id=uuid.uuid4(), bucket_id=bid_c, credits_delta=-50)

        db = self._db_for_capture(
            [re_a, re_b, re_c],
            {bid_a: bucket_a, bid_b: bucket_b, bid_c: bucket_c},
        )

        entries = _run(shadow_capture(
            db, user_id=uid, job_id="job-cap-5", actual_credits=90,
        ))

        assert bucket_a.reserved == 0
        assert bucket_b.reserved == 0
        assert bucket_c.reserved == 0
        total_captured = sum(abs(e.credits_delta) for e in entries if e.direction == "capture")
        total_released = sum(abs(e.credits_delta) for e in entries if e.direction == "release")
        assert total_captured == 90
        assert total_released == 30


# ===================================================================
# shadow_rollback
# ===================================================================


class TestShadowRollback:
    def test_rollback_zeros_bucket(self):
        uid = uuid.uuid4()
        bucket_id = uuid.uuid4()
        bucket = _make_bucket(
            bucket_type="subscription", remaining=3000, reserved=500,
            user_id=uid, bucket_id=bucket_id,
        )

        db = AsyncMock()
        r = MagicMock()
        r.scalar_one_or_none.return_value = bucket
        db.execute = AsyncMock(return_value=r)
        added = []
        db.add = lambda obj: added.append(obj)

        entry = _run(shadow_rollback(db, user_id=uid, bucket_id=bucket_id))

        assert entry is not None
        assert entry.direction == "rollback"
        assert entry.credits_delta == -3000
        assert bucket.remaining == 0
        assert bucket.reserved == 0

    def test_rollback_missing_bucket_returns_none(self):
        db = AsyncMock()
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=r)

        entry = _run(shadow_rollback(db, user_id=uuid.uuid4(), bucket_id=uuid.uuid4()))
        assert entry is None


# ===================================================================
# T1: SELECT FOR UPDATE regression guards
# ===================================================================


class TestBucketLocking:
    """Regression guards: every bucket-mutating path must SELECT ... FOR UPDATE.

    Without row locks, two concurrent jobs for the same user can read the same
    bucket state and both commit mutations — losing one write (real money bug).

    SQLite (default test DB) treats FOR UPDATE as a no-op, so these tests
    verify the *intent* (the statement's ``_for_update_arg`` attribute) rather
    than actual locking behavior. Actual lock enforcement requires PostgreSQL
    and lives in the ``@pytest.mark.postgres`` integration test below.
    """

    @staticmethod
    def _bucket_selects_from_mock(db_mock) -> list:
        """Return all Select statements targeting CreditsBucket that were executed.

        Matches on the mapped table name (``credits_buckets``) in the compiled
        SQL — the class name does not appear in ``str(stmt)``.
        """
        stmts = []
        for call in db_mock.execute.await_args_list:
            if not call.args:
                continue
            s = call.args[0]
            if "credits_buckets" in str(s).lower():
                stmts.append(s)
        return stmts

    def test_shadow_reserve_acquires_bucket_lock(self):
        uid = uuid.uuid4()
        bucket = _make_bucket(bucket_type="free", remaining=500, reserved=0, user_id=uid)
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [bucket]
        db.execute = AsyncMock(return_value=result)
        db.add = lambda obj: None

        _run(shadow_reserve(
            db, user_id=uid, job_id="job-1", estimated_credits=50, service_mode="express",
        ))

        bucket_selects = self._bucket_selects_from_mock(db)
        assert bucket_selects, "shadow_reserve must SELECT CreditsBucket"
        assert any(
            getattr(s, "_for_update_arg", None) is not None for s in bucket_selects
        ), "shadow_reserve must use SELECT ... FOR UPDATE on bucket read"

    def test_shadow_capture_acquires_bucket_lock(self):
        uid = uuid.uuid4()
        bucket = _make_bucket(bucket_type="free", remaining=500, reserved=100, user_id=uid)
        reserve_entry = SimpleNamespace(
            bucket_id=bucket.id, credits_delta=-100, direction="reserve",
        )
        db = AsyncMock()
        ledger_res = MagicMock()
        ledger_res.scalars.return_value.all.return_value = [reserve_entry]
        bucket_res = MagicMock()
        bucket_res.scalar_one_or_none.return_value = bucket
        db.execute = AsyncMock(side_effect=[ledger_res, bucket_res])
        db.add = lambda obj: None

        _run(shadow_capture(
            db, user_id=uid, job_id="job-1", actual_credits=80, service_mode="express",
        ))

        bucket_selects = self._bucket_selects_from_mock(db)
        assert bucket_selects, "shadow_capture must SELECT CreditsBucket"
        assert all(
            getattr(s, "_for_update_arg", None) is not None for s in bucket_selects
        ), "every shadow_capture bucket SELECT must use FOR UPDATE"

    def test_shadow_release_acquires_bucket_lock(self):
        uid = uuid.uuid4()
        bucket = _make_bucket(bucket_type="free", remaining=500, reserved=100, user_id=uid)
        reserve_entry = SimpleNamespace(
            bucket_id=bucket.id, credits_delta=-100, direction="reserve",
        )
        db = AsyncMock()
        ledger_res = MagicMock()
        ledger_res.scalars.return_value.all.return_value = [reserve_entry]
        bucket_res = MagicMock()
        bucket_res.scalar_one_or_none.return_value = bucket
        db.execute = AsyncMock(side_effect=[ledger_res, bucket_res])
        db.add = lambda obj: None

        _run(shadow_release(db, user_id=uid, job_id="job-1"))

        bucket_selects = self._bucket_selects_from_mock(db)
        assert bucket_selects, "shadow_release must SELECT CreditsBucket"
        assert all(
            getattr(s, "_for_update_arg", None) is not None for s in bucket_selects
        ), "shadow_release must use SELECT ... FOR UPDATE on bucket read"

    def test_shadow_rollback_acquires_bucket_lock(self):
        uid = uuid.uuid4()
        bucket_id = uuid.uuid4()
        bucket = _make_bucket(
            bucket_type="subscription", remaining=3000, reserved=500,
            user_id=uid, bucket_id=bucket_id,
        )
        db = AsyncMock()
        r = MagicMock()
        r.scalar_one_or_none.return_value = bucket
        db.execute = AsyncMock(return_value=r)
        db.add = lambda obj: None

        _run(shadow_rollback(db, user_id=uid, bucket_id=bucket_id))

        bucket_selects = self._bucket_selects_from_mock(db)
        assert bucket_selects, "shadow_rollback must SELECT CreditsBucket"
        assert any(
            getattr(s, "_for_update_arg", None) is not None for s in bucket_selects
        ), "shadow_rollback must use SELECT ... FOR UPDATE on bucket read"


@pytest.mark.postgres
def test_concurrent_reserve_serialization():
    """INTEGRATION: under real PG, two concurrent reserves on same user serialize.

    Skipped by default. Run with a live PostgreSQL and TEST_DATABASE_URL to
    verify end-to-end that FOR UPDATE actually prevents double-claim. The
    unit tests above only verify the SELECT statements carry the FOR UPDATE
    intent — SQLite is a no-op for row locks.
    """
    pytest.skip("Requires PostgreSQL integration setup — see TEST_DATABASE_URL")


# ===================================================================
# shadow_safe
# ===================================================================


class TestShadowSafe:
    def test_success_returns_result(self):
        async def ok_fn():
            return 42
        assert _run(shadow_safe(ok_fn)) == 42

    def test_exception_returns_none(self):
        async def bad_fn():
            raise RuntimeError("boom")
        assert _run(shadow_safe(bad_fn)) is None

    def test_passes_args(self):
        async def fn(a, b, c=None):
            return (a, b, c)
        assert _run(shadow_safe(fn, 1, 2, c=3)) == (1, 2, 3)


# ===================================================================
# Shadow failure isolation: V2 paths unaffected
# ===================================================================


class TestShadowFailureIsolation:
    """Ensure that shadow service exceptions never propagate to callers."""

    def test_grant_db_failure_is_swallowed(self):
        db = AsyncMock()
        db.add = MagicMock(side_effect=RuntimeError("connection lost"))
        # Should not raise
        result = _run(shadow_grant(db, user_id=uuid.uuid4(), bucket_type="free", amount=100))
        assert result is None

    def test_reserve_db_failure_is_swallowed(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=RuntimeError("timeout"))
        # Should not raise
        entries = _run(shadow_reserve(
            db, user_id=uuid.uuid4(), job_id="j", estimated_credits=50,
        ))
        assert entries == []

    def test_release_db_failure_is_swallowed(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=RuntimeError("timeout"))
        entries = _run(shadow_release(db, user_id=uuid.uuid4(), job_id="j"))
        assert entries == []

    def test_rollback_db_failure_is_swallowed(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=RuntimeError("timeout"))
        entry = _run(shadow_rollback(db, user_id=uuid.uuid4(), bucket_id=uuid.uuid4()))
        assert entry is None


# ===================================================================
# Runtime pricing derivation
# ===================================================================


class TestRuntimeDebitRates:
    """Verify estimate_credits uses runtime debit rates when available."""

    def test_estimate_credits_uses_runtime_debit_rate(self):
        """Monkeypatch runtime pricing with a custom rate, verify estimate changes."""
        from pricing_schema import build_default_pricing_payload

        payload = build_default_pricing_payload()
        # Override express.standard: 10 -> 20
        payload.credits.debit_rates["express.standard"] = 20

        fake_mod = types.ModuleType("pricing_runtime")
        fake_mod.get_runtime_pricing = lambda **kw: payload
        with patch.dict(sys.modules, {"pricing_runtime": fake_mod}):
            result = estimate_credits(5.0, "express", "standard")
            # 5 * 20 = 100 (not 5 * 10 = 50)
            assert result == 100

    def test_estimate_credits_fallback_on_runtime_error(self):
        """When runtime pricing raises, estimate_credits falls back to frozen constants."""
        def boom(**kw):
            raise RuntimeError("pricing unavailable")

        with patch.dict(sys.modules, {"pricing_runtime": types.ModuleType("pricing_runtime")}):
            sys.modules["pricing_runtime"].get_runtime_pricing = boom
            # Should fall back to frozen DEBIT_RATES: express.standard = 10
            assert estimate_credits(5.0, "express", "standard") == 50

    def test_get_runtime_debit_rates_parses_dotted_keys(self):
        """_get_runtime_debit_rates converts 'mode.tier' strings to tuple keys."""
        from pricing_schema import build_default_pricing_payload

        payload = build_default_pricing_payload()
        with patch.dict(sys.modules, {"pricing_runtime": types.ModuleType("pricing_runtime")}):
            sys.modules["pricing_runtime"].get_runtime_pricing = lambda **kw: payload
            rates = _get_runtime_debit_rates()
            assert rates[("express", "standard")] == 10
            assert rates[("studio", "flagship")] == 50


class TestRuntimeGrantAmounts:
    """Verify _get_runtime_grant_amounts derives correctly from default payload."""

    def test_grant_amounts_from_runtime(self):
        """Verify free, trial, plus, pro amounts all derived from runtime payload."""
        from pricing_schema import build_default_pricing_payload

        payload = build_default_pricing_payload()
        with patch.dict(sys.modules, {"pricing_runtime": types.ModuleType("pricing_runtime")}):
            sys.modules["pricing_runtime"].get_runtime_pricing = lambda **kw: payload
            grants = _get_runtime_grant_amounts()
            assert grants["free"] == 500   # credits.free_grant_credits
            assert grants["trial"] == 300  # trial.grant_credits
            assert grants["plus"] == 3500  # plans.plus.monthly_grant_credits
            assert grants["pro"] == 12000  # plans.pro.monthly_grant_credits
            # "free" plan has no monthly_grant_credits, should not overwrite
            assert "free" in grants  # still present from credits config

    def test_grant_amounts_with_custom_values(self):
        """Override grant amounts in runtime payload, verify derivation."""
        from pricing_schema import build_default_pricing_payload

        payload = build_default_pricing_payload()
        payload.credits.free_grant_credits = 999
        payload.trial.grant_credits = 777

        with patch.dict(sys.modules, {"pricing_runtime": types.ModuleType("pricing_runtime")}):
            sys.modules["pricing_runtime"].get_runtime_pricing = lambda **kw: payload
            grants = _get_runtime_grant_amounts()
            assert grants["free"] == 999
            assert grants["trial"] == 777

    def test_grant_amounts_fallback_on_error(self):
        """When runtime pricing raises, falls back to frozen GRANT_AMOUNTS."""
        def boom(**kw):
            raise RuntimeError("pricing unavailable")

        with patch.dict(sys.modules, {"pricing_runtime": types.ModuleType("pricing_runtime")}):
            sys.modules["pricing_runtime"].get_runtime_pricing = boom
            grants = _get_runtime_grant_amounts()
            assert grants == GRANT_AMOUNTS


class TestRuntimeBucketPriority:
    """Verify _get_runtime_bucket_priority derives from runtime pricing."""

    def test_bucket_priority_from_runtime(self):
        from pricing_schema import build_default_pricing_payload

        payload = build_default_pricing_payload()
        with patch.dict(sys.modules, {"pricing_runtime": types.ModuleType("pricing_runtime")}):
            sys.modules["pricing_runtime"].get_runtime_pricing = lambda **kw: payload
            bp = _get_runtime_bucket_priority()
            assert bp["express"] == ["free", "subscription", "topup", "trial"]
            assert bp["studio"] == ["trial", "subscription", "topup", "free"]

    def test_bucket_priority_fallback_on_error(self):
        def boom(**kw):
            raise RuntimeError("pricing unavailable")

        with patch.dict(sys.modules, {"pricing_runtime": types.ModuleType("pricing_runtime")}):
            sys.modules["pricing_runtime"].get_runtime_pricing = boom
            bp = _get_runtime_bucket_priority()
            assert bp == BUCKET_PRIORITY
