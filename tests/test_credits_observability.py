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

from credits_observability import (
    credits_shadow_summary,
    credits_cost_metrics,
    credits_provider_breakdown,
    credits_outliers,
    _parse_window,
    FIELD_STATUS,
)


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
            "metering_snapshot.rewrite_count", "metering_snapshot.quality_tier",
        ]
        for field in required:
            assert field in FIELD_STATUS, f"Missing field: {field}"


# ===========================================================================
# _parse_window
# ===========================================================================


class TestParseWindow:
    def test_default(self):
        days, cutoff = _parse_window()
        assert days == 7

    def test_valid_value(self):
        days, _ = _parse_window("30")
        assert days == 30

    def test_clamp_low(self):
        days, _ = _parse_window("0")
        assert days == 1

    def test_clamp_high(self):
        days, _ = _parse_window("999")
        assert days == 90

    def test_invalid_string(self):
        days, _ = _parse_window("abc")
        assert days == 7

    def test_none_input(self):
        days, _ = _parse_window(None)
        assert days == 7


# ===========================================================================
# GET /cost-metrics
# ===========================================================================


class TestCostMetricsAuth:
    def test_unauthenticated_returns_401(self):
        db = AsyncMock()
        with pytest.raises(Exception) as exc_info:
            _run(credits_cost_metrics(window="7", db=db, user=None))
        assert "401" in str(exc_info.value.status_code)

    def test_non_admin_returns_403(self):
        db = AsyncMock()
        user = _make_user(role="user")
        with pytest.raises(Exception) as exc_info:
            _run(credits_cost_metrics(window="7", db=db, user=user))
        assert "403" in str(exc_info.value.status_code)


class TestCostMetricsResponse:
    """Test cost-metrics endpoint with mocked DB.

    DB call sequence (9 queries):
      1. jobs total (scalar)
      2. credits est/act sums (one row: .est, .act)
      3. K value percentiles (one row: .avg, .p50, .p75, .p90)
      4. rewrite stats (one row: .total, .with_rewrite, .avg_count)
      5. service mode distribution (rows: .service_mode, .count)
      6. tts coverage (one row: .total, .with_tts)
      7. unsettled: reserve job-ids
      8. unsettled: settle job-ids
    """

    def _mock_db(self, *, jobs_total=0, est_sum=0, act_sum=0,
                 k_avg=None, k_p50=None, k_p75=None, k_p90=None,
                 rw_total=0, rw_with_rewrite=0, rw_avg_count=None,
                 mode_rows=None, tts_total=0, tts_with=0,
                 reserve_ids=None, settle_ids=None):
        db = AsyncMock()
        call_n = {"n": 0}

        async def smart_execute(*args, **kwargs):
            call_n["n"] += 1
            r = MagicMock()
            if call_n["n"] == 1:  # jobs total
                r.scalar.return_value = jobs_total
            elif call_n["n"] == 2:  # credits sums
                row = SimpleNamespace(est=est_sum, act=act_sum)
                r.one.return_value = row
            elif call_n["n"] == 3:  # K percentiles
                row = SimpleNamespace(avg=k_avg, p50=k_p50, p75=k_p75, p90=k_p90)
                r.one.return_value = row
            elif call_n["n"] == 4:  # rewrite stats
                row = SimpleNamespace(total=rw_total, with_rewrite=rw_with_rewrite, avg_count=rw_avg_count)
                r.one.return_value = row
            elif call_n["n"] == 5:  # mode distribution
                r.all.return_value = mode_rows or []
            elif call_n["n"] == 6:  # tts coverage
                row = SimpleNamespace(total=tts_total, with_tts=tts_with)
                r.one.return_value = row
            elif call_n["n"] == 7:  # reserve ids
                r.all.return_value = [(jid,) for jid in (reserve_ids or [])]
            elif call_n["n"] == 8:  # settle ids
                r.all.return_value = [(jid,) for jid in (settle_ids or [])]
            return r

        db.execute = smart_execute
        return db

    def test_empty_system(self):
        user = _make_user()
        db = self._mock_db()
        resp = _run(credits_cost_metrics(window="7", db=db, user=user))

        assert resp["window_days"] == 7
        assert resp["jobs_total"] == 0
        assert resp["credits_estimated_sum"] == 0
        assert resp["credits_actual_sum"] == 0
        assert resp["estimate_actual_delta_pct"] is None
        assert resp["k_actual"]["avg"] is None
        assert resp["rewrite_rate_pct"] is None
        assert resp["jobs_unsettled"] == 0

    def test_with_data(self):
        user = _make_user()
        db = self._mock_db(
            jobs_total=20, est_sum=3200, act_sum=2850,
            k_avg=281.0, k_p50=275.0, k_p75=310.0, k_p90=350.0,
            rw_total=20, rw_with_rewrite=17, rw_avg_count=2.3,
            mode_rows=[
                SimpleNamespace(service_mode="express", count=10),
                SimpleNamespace(service_mode="studio", count=10),
            ],
            tts_total=20, tts_with=17,
            reserve_ids=["j1"], settle_ids=[],
        )
        resp = _run(credits_cost_metrics(window="7", db=db, user=user))

        assert resp["jobs_total"] == 20
        assert resp["credits_estimated_sum"] == 3200
        assert resp["credits_actual_sum"] == 2850
        assert resp["estimate_actual_delta_pct"] == 12.3
        assert resp["k_actual"]["avg"] == 281
        assert resp["k_actual"]["p90"] == 350
        assert resp["rewrite_rate_pct"] == 85.0
        assert resp["rewrite_count_avg"] == 2.3
        assert resp["service_mode_dist"]["express"] == 10
        assert resp["tts_billed_chars_coverage_pct"] == 85.0
        assert resp["jobs_unsettled"] == 1

    def test_window_30(self):
        user = _make_user()
        db = self._mock_db(jobs_total=50)
        resp = _run(credits_cost_metrics(window="30", db=db, user=user))
        assert resp["window_days"] == 30
        assert resp["jobs_total"] == 50

    def test_unsettled_uses_window(self):
        """Unsettled count should reflect the window, not full history."""
        user = _make_user()
        # If reserve_ids has "old_job" but settle_ids has it too → 0 unsettled
        db = self._mock_db(reserve_ids=["j1", "j2"], settle_ids=["j1"])
        resp = _run(credits_cost_metrics(window="7", db=db, user=user))
        assert resp["jobs_unsettled"] == 1


# ===========================================================================
# GET /provider-breakdown
# ===========================================================================


class TestProviderBreakdownAuth:
    def test_non_admin_returns_403(self):
        db = AsyncMock()
        user = _make_user(role="user")
        with pytest.raises(Exception) as exc_info:
            _run(credits_provider_breakdown(window="7", db=db, user=user))
        assert "403" in str(exc_info.value.status_code)


class TestProviderBreakdownResponse:
    """DB call sequence: 1 query returning grouped rows."""

    def _mock_db(self, rows=None):
        db = AsyncMock()

        async def smart_execute(*args, **kwargs):
            r = MagicMock()
            r.all.return_value = rows or []
            return r

        db.execute = smart_execute
        return db

    def test_empty(self):
        user = _make_user()
        db = self._mock_db()
        resp = _run(credits_provider_breakdown(window="7", db=db, user=user))
        assert resp["window_days"] == 7
        assert resp["providers"] == []

    def test_with_providers(self):
        user = _make_user()
        db = self._mock_db(rows=[
            SimpleNamespace(
                provider="minimax", model="speech-2.8-hd",
                job_count=8, total_minutes=45.2, total_billed_chars=128000,
                avg_billed_per_min=2832.0, avg_credits_per_min=14.5,
            ),
            SimpleNamespace(
                provider="cosyvoice", model="cosyvoice-v1",
                job_count=12, total_minutes=38.5, total_billed_chars=95000,
                avg_billed_per_min=2468.0, avg_credits_per_min=12.1,
            ),
        ])
        resp = _run(credits_provider_breakdown(window="7", db=db, user=user))

        assert len(resp["providers"]) == 2
        mm = resp["providers"][0]
        assert mm["provider"] == "minimax"
        assert mm["job_count"] == 8
        assert mm["total_minutes"] == 45.2
        assert mm["avg_credits_per_min"] == 14.5

    def test_null_provider_becomes_unknown(self):
        user = _make_user()
        db = self._mock_db(rows=[
            SimpleNamespace(
                provider=None, model=None,
                job_count=3, total_minutes=10.0, total_billed_chars=0,
                avg_billed_per_min=None, avg_credits_per_min=None,
            ),
        ])
        resp = _run(credits_provider_breakdown(window="7", db=db, user=user))
        assert resp["providers"][0]["provider"] == "unknown"
        assert resp["providers"][0]["model"] == "unknown"


# ===========================================================================
# GET /outliers
# ===========================================================================


class TestOutliersAuth:
    def test_non_admin_returns_403(self):
        db = AsyncMock()
        user = _make_user(role="user")
        with pytest.raises(Exception) as exc_info:
            _run(credits_outliers(window="7", db=db, user=user))
        assert "403" in str(exc_info.value.status_code)


class TestOutliersResponse:
    """DB call sequence (6 queries):
      1. estimate/actual delta top 10
      2. rewrite top 10
      3. unsettled: reserve ids
      4. unsettled: settle ids
      5. missing fields jobs
    """

    def _mock_db(self, *, delta_rows=None, rewrite_rows=None,
                 reserve_ids=None, settle_ids=None, missing_rows=None):
        db = AsyncMock()
        call_n = {"n": 0}

        async def smart_execute(*args, **kwargs):
            call_n["n"] += 1
            r = MagicMock()
            if call_n["n"] == 1:  # delta outliers
                r.all.return_value = delta_rows or []
            elif call_n["n"] == 2:  # rewrite top
                r.all.return_value = rewrite_rows or []
            elif call_n["n"] == 3:  # reserve ids
                r.all.return_value = [(jid,) for jid in (reserve_ids or [])]
            elif call_n["n"] == 4:  # settle ids
                r.all.return_value = [(jid,) for jid in (settle_ids or [])]
            elif call_n["n"] == 5:  # missing fields
                r.all.return_value = missing_rows or []
            return r

        db.execute = smart_execute
        return db

    def test_empty(self):
        user = _make_user()
        db = self._mock_db()
        resp = _run(credits_outliers(window="7", db=db, user=user))

        assert resp["window_days"] == 7
        assert resp["estimate_actual_outliers"] == []
        assert resp["rewrite_top"] == []
        assert resp["unsettled_jobs"] == []
        assert resp["missing_fields_jobs"] == []

    def test_with_outliers(self):
        user = _make_user()
        db = self._mock_db(
            delta_rows=[
                SimpleNamespace(
                    job_id="job-1", title="Test Video", service_mode="studio",
                    credits_estimated=90, credits_actual=45, delta=45,
                    actual_minutes=3.0,
                ),
            ],
            rewrite_rows=[
                SimpleNamespace(
                    job_id="job-2", title="Rewrite Heavy", rewrite_count=12,
                    actual_minutes=5.2,
                ),
            ],
            reserve_ids=["job-3", "job-4"],
            settle_ids=["job-3"],
            missing_rows=[
                SimpleNamespace(
                    job_id="job-5", final_cn_chars=None, credits_actual="45",
                ),
            ],
        )
        resp = _run(credits_outliers(window="7", db=db, user=user))

        assert len(resp["estimate_actual_outliers"]) == 1
        assert resp["estimate_actual_outliers"][0]["delta"] == 45

        assert len(resp["rewrite_top"]) == 1
        assert resp["rewrite_top"][0]["rewrite_count"] == 12

        assert resp["unsettled_jobs"] == ["job-4"]

        assert len(resp["missing_fields_jobs"]) == 1
        assert "final_cn_chars" in resp["missing_fields_jobs"][0]["missing"]
        assert "credits_actual" not in resp["missing_fields_jobs"][0]["missing"]

    def test_unsettled_limited_to_20(self):
        user = _make_user()
        many_ids = [f"j-{i}" for i in range(30)]
        db = self._mock_db(reserve_ids=many_ids, settle_ids=[])
        resp = _run(credits_outliers(window="7", db=db, user=user))
        assert len(resp["unsettled_jobs"]) == 20
