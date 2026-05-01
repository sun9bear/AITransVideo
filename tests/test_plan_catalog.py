"""Tests for gateway.plan_catalog — the v2 Task 0 source of truth.

Goals covered by this suite:
1. The plan table contains the tiers we actually ship (free / plus / pro).
2. Pricing helpers return the values previously hardcoded in billing.py.
3. Legacy views (for billing.py and job_intercept.py backward compat) are consistent
   with the central table.
4. The ``GET /api/plans`` response payload has the shape promised by the API contract.
5. Trial config is NOT frozen and does not carry numeric values yet.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# Stub heavy deps before importing the gateway module, mirroring other gateway tests.
_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from plan_catalog import (  # noqa: E402
    PLANS,
    TRIAL_CONFIG,
    VALID_BILLING_PERIODS,
    PlanDefinition,
    _build_plans_response,
    _get_runtime_plans,
    _get_runtime_trial_config,
    get_legacy_plan_gate_dict,
    get_legacy_price_table,
    get_plan,
    get_price,
    list_plan_codes,
    valid_target_plan_codes,
)


# ---------------------------------------------------------------------------
# Plan table
# ---------------------------------------------------------------------------


class TestPlanTable:
    def test_expected_tiers_present(self):
        assert set(PLANS.keys()) == {"free", "plus", "pro"}

    def test_plan_definitions_are_frozen_dataclasses(self):
        for plan in PLANS.values():
            assert isinstance(plan, PlanDefinition)

    def test_free_plan_gate_fields(self):
        free = PLANS["free"]
        assert free.max_duration_minutes == 10
        assert free.max_concurrent_jobs == 1
        assert free.allowed_service_modes == ("express",)
        assert free.free_quota_total == 5
        assert free.price is None
        assert free.self_serve is False

    def test_plus_plan_gate_fields(self):
        plus = PLANS["plus"]
        assert plus.max_duration_minutes == 45
        assert plus.max_concurrent_jobs == 3
        assert "studio" in plus.allowed_service_modes
        assert plus.free_quota_total is None
        assert plus.self_serve is True

    def test_pro_plan_gate_fields(self):
        pro = PLANS["pro"]
        assert pro.max_duration_minutes == 180
        assert pro.max_concurrent_jobs == 5
        assert pro.self_serve is True

    def test_valid_billing_periods(self):
        assert set(VALID_BILLING_PERIODS) == {"monthly", "quarterly", "annual"}


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------


class TestPricingHelpers:
    def test_plus_prices(self):
        assert get_price("plus", "monthly") == 9900
        assert get_price("plus", "quarterly") == 26900
        assert get_price("plus", "annual") == 99900

    def test_pro_prices(self):
        assert get_price("pro", "monthly") == 29900
        assert get_price("pro", "quarterly") == 79900
        assert get_price("pro", "annual") == 299900

    def test_free_plan_has_no_price(self):
        assert get_price("free", "monthly") is None

    def test_unknown_plan_returns_none(self):
        assert get_price("enterprise", "monthly") is None

    def test_unknown_period_returns_none(self):
        assert get_price("plus", "weekly") is None

    def test_valid_target_plan_codes_excludes_free(self):
        assert valid_target_plan_codes() == {"plus", "pro"}

    def test_list_plan_codes_includes_free(self):
        assert set(list_plan_codes()) == {"free", "plus", "pro"}


# ---------------------------------------------------------------------------
# get_plan fallback
# ---------------------------------------------------------------------------


class TestGetPlan:
    def test_known_code(self):
        assert get_plan("plus").code == "plus"

    def test_unknown_code_falls_back_to_free(self):
        assert get_plan("enterprise").code == "free"


# ---------------------------------------------------------------------------
# Trial boundary — FROZEN by H1 decision 2026-04-06
# ---------------------------------------------------------------------------


class TestTrialBoundary:
    def test_trial_is_frozen(self):
        assert TRIAL_CONFIG["frozen"] is True

    def test_trial_has_explanatory_notes(self):
        assert "notes" in TRIAL_CONFIG
        assert isinstance(TRIAL_CONFIG["notes"], str)
        assert len(TRIAL_CONFIG["notes"]) > 0

    def test_trial_frozen_facts(self):
        """Frozen trial facts must match H1 decision."""
        assert TRIAL_CONFIG["days"] == 7
        assert TRIAL_CONFIG["source_minutes"] == 20
        assert TRIAL_CONFIG["includes_studio"] is True
        assert TRIAL_CONFIG["phone_required"] is True
        assert TRIAL_CONFIG["auto_charge"] is False
        assert TRIAL_CONFIG["fallback_plan"] == "free"


# ---------------------------------------------------------------------------
# Legacy views — downstream consumers must stay derivable from plan_catalog
# ---------------------------------------------------------------------------


class TestLegacyViews:
    def test_legacy_gate_dict_matches_plans(self):
        legacy = get_legacy_plan_gate_dict()
        assert set(legacy.keys()) == {"free", "plus", "pro"}
        assert legacy["free"]["max_duration_minutes"] == 10
        assert legacy["free"]["allowed_service_modes"] == ["express"]
        assert legacy["free"]["free_quota_total"] == 5
        assert legacy["plus"]["max_duration_minutes"] == 45
        assert "studio" in legacy["plus"]["allowed_service_modes"]
        assert "free_quota_total" not in legacy["plus"]
        assert legacy["pro"]["max_concurrent_jobs"] == 5

    def test_legacy_price_table_matches_plans(self):
        legacy = get_legacy_price_table()
        assert legacy[("plus", "monthly")] == 9900
        assert legacy[("plus", "quarterly")] == 26900
        assert legacy[("plus", "annual")] == 99900
        assert legacy[("pro", "monthly")] == 29900
        assert legacy[("pro", "quarterly")] == 79900
        assert legacy[("pro", "annual")] == 299900
        # Free plan never appears in the price table.
        for (code, _period) in legacy.keys():
            assert code != "free"

    def test_billing_module_derives_from_plan_catalog(self):
        """billing.PLAN_PRICES_CNY must be the exact view from plan_catalog."""
        from billing import PLAN_PRICES_CNY, VALID_TARGET_PLANS, VALID_BILLING_PERIODS as B_PERIODS

        assert PLAN_PRICES_CNY == get_legacy_price_table()
        assert VALID_TARGET_PLANS == valid_target_plan_codes()
        assert B_PERIODS == set(VALID_BILLING_PERIODS)

    def test_job_intercept_module_derives_from_plan_catalog(self):
        """job_intercept.PLAN_CATALOG must be the exact view from plan_catalog."""
        from job_intercept import PLAN_CATALOG

        assert PLAN_CATALOG == get_legacy_plan_gate_dict()


# ---------------------------------------------------------------------------
# /api/plans response payload
# ---------------------------------------------------------------------------


class TestPlansResponsePayload:
    def test_response_top_level_keys(self):
        payload = _build_plans_response()
        # 2026-05-02: added top-level `credits_per_minute` so the marketing
        # pricing card can compute "约 N 分钟 Express / N 分钟 Studio" without
        # hardcoding rates client-side. The flat string-key shape (e.g.
        # "express_standard": 10) is documented in the API contract doc.
        assert set(payload.keys()) == {"plans", "trial", "credits_per_minute"}

    def test_response_credits_per_minute_shape(self):
        payload = _build_plans_response()
        cpm = payload["credits_per_minute"]
        assert isinstance(cpm, dict)
        # Frozen V3 baseline rates — see credits_service.DEBIT_RATES
        assert cpm["express_standard"] == 10
        assert cpm["studio_standard"] == 15
        assert cpm["studio_high"] == 30
        assert cpm["studio_flagship"] == 50

    def test_plans_carry_monthly_grant_credits(self):
        payload = _build_plans_response()
        grants = {p["code"]: p.get("monthly_grant_credits") for p in payload["plans"]}
        # Frozen V3 grant amounts. Free is the recurring free-bucket grant
        # (separate from free_quota_total which counts task slots).
        assert grants == {"free": 500, "plus": 3500, "pro": 12000}

    def test_response_plans_order_and_codes(self):
        payload = _build_plans_response()
        codes = [p["code"] for p in payload["plans"]]
        assert codes == ["free", "plus", "pro"]

    def test_free_plan_public_fields(self):
        payload = _build_plans_response()
        free = next(p for p in payload["plans"] if p["code"] == "free")
        assert free["display_name"] == "Free"
        assert free["max_duration_minutes"] == 10
        assert free["max_concurrent_jobs"] == 1
        assert free["allowed_service_modes"] == ["express"]
        assert free["self_serve"] is False
        assert free["price_cny_fen"] is None
        assert free["free_quota_total"] == 5

    def test_plus_plan_public_fields(self):
        payload = _build_plans_response()
        plus = next(p for p in payload["plans"] if p["code"] == "plus")
        assert plus["display_name"] == "Plus"
        assert plus["self_serve"] is True
        assert plus["price_cny_fen"] == {
            "monthly": 9900,
            "quarterly": 26900,
            "annual": 99900,
        }
        assert plus["max_duration_minutes"] == 45
        # Plus is not the free tier, so this field must not leak.
        assert "free_quota_total" not in plus

    def test_pro_plan_public_fields(self):
        payload = _build_plans_response()
        pro = next(p for p in payload["plans"] if p["code"] == "pro")
        assert pro["price_cny_fen"]["annual"] == 299900
        assert pro["max_concurrent_jobs"] == 5
        assert "studio" in pro["allowed_service_modes"]

    def test_trial_payload_frozen(self):
        payload = _build_plans_response()
        trial = payload["trial"]
        assert trial["frozen"] is True
        assert trial["days"] == 7
        assert trial["source_minutes"] == 20
        assert trial["includes_studio"] is True


# ---------------------------------------------------------------------------
# Runtime pricing bridge
# ---------------------------------------------------------------------------


class TestRuntimePricingBridge:
    """Verify helpers reflect changes from the runtime pricing layer."""

    def test_runtime_plans_match_frozen_snapshot(self):
        """When runtime returns defaults, derived plans match the frozen PLANS."""
        runtime = _get_runtime_plans()
        assert set(runtime.keys()) == set(PLANS.keys())
        for code in PLANS:
            assert runtime[code].display_name == PLANS[code].display_name
            assert runtime[code].max_duration_minutes == PLANS[code].max_duration_minutes

    def test_runtime_trial_config_match_frozen_snapshot(self):
        """When runtime returns defaults, derived trial matches TRIAL_CONFIG."""
        runtime = _get_runtime_trial_config()
        assert runtime["frozen"] is TRIAL_CONFIG["frozen"]
        assert runtime["days"] == TRIAL_CONFIG["days"]
        assert runtime["source_minutes"] == TRIAL_CONFIG["source_minutes"]
        assert "notes" in runtime  # notes preserved from frozen constant

    def test_get_price_reads_runtime_pricing(self, monkeypatch):
        """When runtime pricing has different values, helpers reflect them."""
        import pricing_runtime
        from pricing_schema import PlanPriceConfig, build_default_pricing_payload

        modified = build_default_pricing_payload()
        # Construct a new PlanConfig with a changed monthly price
        original_plus = modified.plans["plus"]
        modified.plans["plus"] = original_plus.model_copy(
            update={"price_cny_fen": PlanPriceConfig(
                monthly=10900,
                quarterly=original_plus.price_cny_fen.quarterly,
                annual=original_plus.price_cny_fen.annual,
            )}
        )
        monkeypatch.setattr(
            pricing_runtime, "get_runtime_pricing",
            lambda force_reload=False: modified,
        )
        assert get_price("plus", "monthly") == 10900
        # Other prices unchanged
        assert get_price("plus", "quarterly") == 26900

    def test_build_plans_response_reads_runtime_trial(self, monkeypatch):
        """Trial section in /api/plans reflects runtime trial config."""
        import pricing_runtime
        from pricing_schema import build_default_pricing_payload

        modified = build_default_pricing_payload()
        modified.trial.days = 14
        monkeypatch.setattr(
            pricing_runtime, "get_runtime_pricing",
            lambda force_reload=False: modified,
        )
        resp = _build_plans_response()
        assert resp["trial"]["days"] == 14

    def test_fallback_to_frozen_on_runtime_error(self, monkeypatch):
        """If runtime raises, helpers fall back to module-level PLANS."""
        import pricing_runtime

        def _boom(force_reload=False):
            raise RuntimeError("runtime broken")

        monkeypatch.setattr(pricing_runtime, "get_runtime_pricing", _boom)
        # Should fall back gracefully
        assert get_price("plus", "monthly") == 9900
        assert set(list_plan_codes()) == {"free", "plus", "pro"}
