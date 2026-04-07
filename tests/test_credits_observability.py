"""Tests for V3-3 shadow observability baseline endpoint."""
from __future__ import annotations

import asyncio
import sys
import types
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from credits_observability import credits_shadow_summary, FIELD_STATUS


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user(*, role="admin"):
    return SimpleNamespace(
        id=uuid.uuid4(), role=role, plan_code="free",
    )


class TestShadowSummaryAuth:
    def test_unauthenticated_returns_401(self):
        db = AsyncMock()
        with pytest.raises(Exception) as exc_info:
            _run(credits_shadow_summary(db=db, user=None))
        assert "401" in str(exc_info.value.status_code)

    def test_non_admin_returns_403(self):
        db = AsyncMock()
        user = _make_user(role="user")
        with pytest.raises(Exception) as exc_info:
            _run(credits_shadow_summary(db=db, user=user))
        assert "403" in str(exc_info.value.status_code)


class TestShadowSummaryResponse:
    def _mock_db(self, *, bucket_rows=None, ledger_rows=None, recent_entries=None,
                 total_jobs=0, has_estimated=0, has_actual=0, has_snapshot=0,
                 has_credits_est=0, has_credits_act=0,
                 reserve_job_ids=None, settle_job_ids=None):
        """Mock DB for credits_shadow_summary.

        reserve_job_ids / settle_job_ids: lists of job-id strings returned as
        row tuples from the DISTINCT queries. Defaults to empty.
        """
        db = AsyncMock()
        call_n = {"n": 0}
        _reserve_ids = reserve_job_ids or []
        _settle_ids = settle_job_ids or []

        async def smart_execute(*args, **kwargs):
            call_n["n"] += 1
            r = MagicMock()
            if call_n["n"] == 1:
                r.all.return_value = bucket_rows or []          # bucket summary
            elif call_n["n"] == 2:
                r.all.return_value = ledger_rows or []          # ledger by direction
            elif call_n["n"] == 3:
                r.all.return_value = recent_entries or []       # recent ledger
            elif call_n["n"] == 4:
                r.scalar.return_value = total_jobs              # total jobs
            elif call_n["n"] == 5:
                r.scalar.return_value = has_estimated           # with_estimated_minutes
            elif call_n["n"] == 6:
                r.scalar.return_value = has_actual              # with_actual_minutes
            elif call_n["n"] == 7:
                r.scalar.return_value = has_snapshot            # with_metering_snapshot
            elif call_n["n"] == 8:
                r.scalar.return_value = has_credits_est         # with_credits_estimated
            elif call_n["n"] == 9:
                r.scalar.return_value = has_credits_act         # with_credits_actual
            elif call_n["n"] == 10:
                # reserve job-id set (rows of single-element tuples)
                r.all.return_value = [(jid,) for jid in _reserve_ids]
            elif call_n["n"] == 11:
                # settle job-id set
                r.all.return_value = [(jid,) for jid in _settle_ids]
            return r

        db.execute = smart_execute
        return db

    def test_empty_system_returns_zeros(self):
        user = _make_user()
        db = self._mock_db()

        resp = _run(credits_shadow_summary(db=db, user=user))

        assert resp["buckets"] == []
        assert resp["ledger"]["total_entries"] == 0
        assert resp["metering"]["total_jobs"] == 0
        assert resp["metering"]["with_credits_estimated"] == 0
        assert resp["metering"]["with_credits_actual"] == 0
        assert resp["reserve_capture_closeness"]["jobs_with_reserve"] == 0
        assert resp["reserve_capture_closeness"]["jobs_unsettled"] == 0
        assert "field_status" in resp

    def test_with_data_returns_aggregates(self):
        user = _make_user()
        bucket_rows = [
            SimpleNamespace(bucket_type="free", count=10, total_granted=5000, total_remaining=4500, total_reserved=200),
            SimpleNamespace(bucket_type="subscription", count=3, total_granted=10500, total_remaining=9000, total_reserved=500),
        ]
        ledger_rows = [
            SimpleNamespace(direction="grant", count=13),
            SimpleNamespace(direction="reserve", count=8),
            SimpleNamespace(direction="capture", count=5),
            SimpleNamespace(direction="release", count=3),
        ]
        job_ids = [f"job-{i}" for i in range(8)]
        db = self._mock_db(
            bucket_rows=bucket_rows,
            ledger_rows=ledger_rows,
            total_jobs=25,
            has_estimated=20,
            has_actual=15,
            has_snapshot=18,
            has_credits_est=16,
            has_credits_act=12,
            reserve_job_ids=job_ids,
            settle_job_ids=job_ids,
        )

        resp = _run(credits_shadow_summary(db=db, user=user))

        assert len(resp["buckets"]) == 2
        assert resp["metering"]["with_credits_estimated"] == 16
        assert resp["metering"]["with_credits_actual"] == 12
        assert resp["reserve_capture_closeness"]["jobs_unsettled"] == 0

    def test_reserve_capture_closeness_healthy(self):
        user = _make_user()
        ids = ["j1", "j2", "j3"]
        db = self._mock_db(reserve_job_ids=ids, settle_job_ids=ids)

        resp = _run(credits_shadow_summary(db=db, user=user))

        closeness = resp["reserve_capture_closeness"]
        assert closeness["jobs_with_reserve"] == 3
        assert closeness["jobs_unsettled"] == 0
        assert "healthy" in closeness["note"]

    def test_reserve_capture_closeness_partial(self):
        user = _make_user()
        db = self._mock_db(
            reserve_job_ids=["j1", "j2", "j3", "j4"],
            settle_job_ids=["j1", "j2"],
        )

        resp = _run(credits_shadow_summary(db=db, user=user))

        closeness = resp["reserve_capture_closeness"]
        assert closeness["jobs_with_reserve"] == 4
        assert closeness["jobs_unsettled"] == 2
        assert "2 job(s)" in closeness["note"]
        assert "methodology" in closeness

    def test_same_cardinality_different_sets_not_healthy(self):
        """Key anti-false-positive test: reserve {A,B,C} vs settle {D,E,F}.

        Same count (3 vs 3) but zero overlap → 3 unsettled, NOT healthy.
        """
        user = _make_user()
        db = self._mock_db(
            reserve_job_ids=["A", "B", "C"],
            settle_job_ids=["D", "E", "F"],
        )

        resp = _run(credits_shadow_summary(db=db, user=user))

        closeness = resp["reserve_capture_closeness"]
        assert closeness["jobs_with_reserve"] == 3
        assert closeness["jobs_with_settle"] == 0  # intersection with reserve is empty
        assert closeness["jobs_unsettled"] == 3
        assert "healthy" not in closeness["note"]
        assert "3 job(s)" in closeness["note"]
        assert set(closeness["unsettled_job_ids_sample"]) == {"A", "B", "C"}


class TestFieldStatus:
    def test_live_fields_marked_correctly(self):
        live_fields = [k for k, v in FIELD_STATUS.items() if v["status"] == "LIVE"]
        assert "estimated_minutes" in live_fields
        assert "actual_minutes" in live_fields
        assert "metering_snapshot.credits_estimated" in live_fields
        assert "metering_snapshot.credits_actual" in live_fields
        assert "metering_snapshot.service_mode" in live_fields
        assert "metering_snapshot.tts_provider" in live_fields
        assert "metering_snapshot.tts_model" in live_fields
        # V3-4: pipeline metering writeback
        assert "metering_snapshot.final_cn_chars" in live_fields
        assert "metering_snapshot.rewrite_triggered" in live_fields
    def test_tts_billed_chars_is_live_partial(self):
        """V3-5 truth gap: tts_billed_chars is LIVE_PARTIAL (MiMo excluded)."""
        status = FIELD_STATUS["metering_snapshot.tts_billed_chars"]
        assert status["status"] == "LIVE_PARTIAL"
        assert "coverage" in status
        assert status["coverage"]["minimax"].startswith("LIVE")
        assert status["coverage"]["cosyvoice"].startswith("LIVE")
        assert status["coverage"]["volcengine"].startswith("LIVE")
        assert status["coverage"]["mimo"].startswith("NOT_COVERED")

    def test_quality_tier_is_live(self):
        """V3-6: quality_tier is now LIVE (from compute_job_policy)."""
        status = FIELD_STATUS["metering_snapshot.quality_tier"]
        assert status["status"] == "LIVE"

    def test_no_reserved_fields_remain(self):
        """After V3-6, all metering fields should be LIVE or LIVE_PARTIAL."""
        reserved_fields = [k for k, v in FIELD_STATUS.items() if v["status"] == "RESERVED"]
        assert reserved_fields == [], f"Unexpected RESERVED fields: {reserved_fields}"

    def test_all_requested_fields_present(self):
        required = [
            "estimated_minutes", "actual_minutes",
            "metering_snapshot.credits_estimated", "metering_snapshot.credits_actual",
            "metering_snapshot.service_mode", "metering_snapshot.tts_provider",
            "metering_snapshot.tts_model", "metering_snapshot.final_cn_chars",
            "metering_snapshot.tts_billed_chars", "metering_snapshot.rewrite_triggered",
            "metering_snapshot.quality_tier",
        ]
        for field in required:
            assert field in FIELD_STATUS, f"Missing field: {field}"
