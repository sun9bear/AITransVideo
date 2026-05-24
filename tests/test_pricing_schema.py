from __future__ import annotations

import re

import pytest
from pydantic import ValidationError


def test_default_payload_matches_current_frozen_values():
    from gateway.pricing_schema import build_default_pricing_payload

    p = build_default_pricing_payload()

    # version & frozen flag
    assert p.version == 1
    assert p.catalog_frozen is True

    # --- plans ---
    assert set(p.plans.keys()) == {"free", "plus", "pro"}

    free = p.plans["free"]
    assert free.display_name == "Free"
    assert free.free_quota_total == 5
    assert free.max_duration_minutes == 10
    assert free.max_concurrent_jobs == 1
    assert free.allowed_service_modes == ["express"]
    assert free.self_serve is False
    assert free.price_cny_fen is None
    assert free.monthly_grant_credits is None

    plus = p.plans["plus"]
    assert plus.display_name == "Plus"
    assert plus.price_cny_fen is not None
    assert plus.price_cny_fen.monthly == 9900
    assert plus.price_cny_fen.quarterly == 26900
    assert plus.price_cny_fen.annual == 99900
    assert plus.max_duration_minutes == 45
    assert plus.max_concurrent_jobs == 3
    # Task #24 (2026-05-24): smart added to Plus/Pro to mirror
    # plan_catalog.py PLANS — clean-local must match production.
    assert plus.allowed_service_modes == ["express", "studio", "smart"]
    assert plus.self_serve is True
    assert plus.monthly_grant_credits == 3500

    pro = p.plans["pro"]
    assert pro.display_name == "Pro"
    assert pro.price_cny_fen is not None
    assert pro.price_cny_fen.monthly == 29900
    assert pro.price_cny_fen.quarterly == 79900
    assert pro.price_cny_fen.annual == 299900
    assert pro.max_duration_minutes == 180
    assert pro.max_concurrent_jobs == 5
    assert pro.allowed_service_modes == ["express", "studio", "smart"]
    assert pro.self_serve is True
    assert pro.monthly_grant_credits == 12000

    # --- trial ---
    assert p.trial.frozen is True
    assert p.trial.days == 7
    assert p.trial.source_minutes == 20
    assert p.trial.includes_studio is True
    assert p.trial.phone_required is True
    assert p.trial.auto_charge is False
    assert p.trial.fallback_plan == "free"
    assert p.trial.grant_credits == 300

    # --- credits ---
    assert p.credits.free_grant_credits == 500
    # Task #24 (2026-05-24): smart.standard=100 added per spec
    # 2026-05-13-smart-mvp-p2-implementation-plan §5.3.
    assert p.credits.debit_rates == {
        "express.standard": 10,
        "studio.standard": 15,
        "studio.high": 30,
        "studio.flagship": 50,
        "smart.standard": 100,
    }
    assert p.credits.bucket_priority == {
        "express": ["free", "subscription", "topup", "trial"],
        "studio": ["trial", "subscription", "topup", "free"],
        # Smart is a paid feature like studio — paid-first consumption.
        "smart": ["trial", "subscription", "topup", "free"],
    }
    assert p.credits.voice_clone_cost_credits == 500

    # --- topup ---
    assert p.topup.enabled is False
    assert len(p.topup.packages) == 2
    pkg0 = p.topup.packages[0]
    assert pkg0.code == "topup_1000"
    assert pkg0.credits == 1000
    assert pkg0.price_cny_fen == 3900
    assert pkg0.active is True
    assert pkg0.sort_order == 10
    pkg1 = p.topup.packages[1]
    assert pkg1.code == "topup_3000"
    assert pkg1.credits == 3000
    assert pkg1.price_cny_fen == 9900
    assert pkg1.active is True
    assert pkg1.sort_order == 20

    # --- cost model ---
    assert p.cost_model.point_cost_rmb == 0.015
    assert p.cost_model.point_price_rmb == 0.03
    assert p.cost_model.target_gross_margin == 0.5
    assert p.cost_model.k_cn_chars_per_src_min == 250
    assert p.cost_model.fx_usd_cny == 7.0
    assert p.cost_model.translate_cost_rmb_per_src_min == 0.03
    assert p.cost_model.s2_review_cost_rmb_per_src_min == 0.02
    assert p.cost_model.rewrite_cost_rmb_per_src_min == 0.02
    assert p.cost_model.server_cost_rmb_per_src_min == 0.03


def test_trial_fallback_plan_must_exist():
    from gateway.pricing_schema import PlanConfig, PricingPayload, TrialConfig, CreditsConfig, TopupConfig, CostModelConfig

    with pytest.raises(ValidationError, match="trial fallback_plan must reference an existing plan"):
        PricingPayload(
            plans={
                "free": PlanConfig(
                    display_name="Free",
                    max_duration_minutes=10,
                    max_concurrent_jobs=1,
                    allowed_service_modes=["express"],
                    self_serve=False,
                ),
            },
            trial=TrialConfig(fallback_plan="nonexistent"),
            credits=CreditsConfig(
                debit_rates={"express.standard": 10},
                bucket_priority={"express": ["free"]},
            ),
            topup=TopupConfig(),
            cost_model=CostModelConfig(),
        )


def test_payload_round_trips_through_json():
    from gateway.pricing_schema import PricingPayload, build_default_pricing_payload

    original = build_default_pricing_payload()
    dumped = original.model_dump()
    restored = PricingPayload.model_validate(dumped)
    assert restored == original


def test_debit_rates_keys_format():
    from gateway.pricing_schema import build_default_pricing_payload

    p = build_default_pricing_payload()
    pattern = re.compile(r"^[a-z]+\.[a-z]+$")
    for key in p.credits.debit_rates:
        assert pattern.match(key), f"debit_rates key {key!r} does not match 'mode.tier' pattern"
