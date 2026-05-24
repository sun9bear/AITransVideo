"""Pricing payload cross-ref consistency — codex follow-up B (2026-05-24).

After Task #24, we discovered that admin's pricing_runtime.json had
``debit_rates['smart.standard']`` and ``bucket_priority['smart']`` but
plus/pro plans' ``allowed_service_modes`` were missing ``smart``. The
result: entitlements returned without smart even though pricing knew
about it — frontend showed "即将开放" for users who should have access.

This module adds a defensive cross-ref check to PricingPayload that
rejects drift:

  Every service_mode key in ``credits.bucket_priority`` must appear in
  at least one plan's ``allowed_service_modes``.

Why this exact rule:
  - Catches "configured but unreachable" modes (the actual 2026-05-24
    incident shape — bucket_priority knew about smart, no plan offered it)
  - Doesn't over-constrain (e.g. doesn't require every plan to support
    every mode — free is allowed to be express-only)
  - Doesn't fight clean-local defaults (which already have a sensible
    smart configuration after Task #24)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


def _make_payload(*, plus_modes, pro_modes, bucket_priority):
    """Build a minimal-but-valid PricingPayload with the parts under test."""
    from pricing_schema import (
        PricingPayload, PlanConfig, TrialConfig, CreditsConfig,
        TopupConfig, CostModelConfig,
    )
    return PricingPayload(
        plans={
            "free": PlanConfig(
                display_name="Free",
                max_duration_minutes=10,
                max_concurrent_jobs=1,
                allowed_service_modes=["express"],
                self_serve=False,
            ),
            "plus": PlanConfig(
                display_name="Plus",
                max_duration_minutes=45,
                max_concurrent_jobs=3,
                allowed_service_modes=plus_modes,
                self_serve=True,
            ),
            "pro": PlanConfig(
                display_name="Pro",
                max_duration_minutes=180,
                max_concurrent_jobs=5,
                allowed_service_modes=pro_modes,
                self_serve=True,
            ),
        },
        trial=TrialConfig(fallback_plan="free"),
        credits=CreditsConfig(
            debit_rates={"express.standard": 10, "studio.standard": 15, "smart.standard": 100},
            bucket_priority=bucket_priority,
        ),
        topup=TopupConfig(),
        cost_model=CostModelConfig(),
    )


# ─────────────────────────────────────────────────────────────────────
# Happy path — current default payload must still validate
# ─────────────────────────────────────────────────────────────────────


def test_current_default_payload_passes_consistency_check():
    """The post-Task #24 default payload (smart present in bucket_priority
    AND plus/pro.allowed_service_modes) must validate cleanly."""
    from pricing_schema import build_default_pricing_payload

    # Should not raise.
    payload = build_default_pricing_payload()
    assert "smart" in payload.credits.bucket_priority
    assert "smart" in payload.plans["plus"].allowed_service_modes
    assert "smart" in payload.plans["pro"].allowed_service_modes


# ─────────────────────────────────────────────────────────────────────
# Negative cases — the actual 2026-05-24 drift shape and similar
# ─────────────────────────────────────────────────────────────────────


def test_bucket_priority_smart_but_no_plan_offers_smart_rejected():
    """The actual 2026-05-24 incident shape: bucket_priority knows
    about smart but plus/pro.allowed_service_modes don't include it.

    Pre-fix, this drift made entitlements API return without smart →
    frontend "即将开放" → users blocked even though pricing was ready."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="smart"):
        _make_payload(
            plus_modes=["express", "studio"],  # missing smart
            pro_modes=["express", "studio"],   # missing smart
            bucket_priority={
                "express": ["free", "subscription", "topup", "trial"],
                "studio": ["trial", "subscription", "topup", "free"],
                "smart": ["trial", "subscription", "topup", "free"],
            },
        )


def test_bucket_priority_unknown_mode_rejected():
    """Any mode in bucket_priority that no plan offers is dead config
    — catch it. ``hypothetical_mode`` should fail because no plan
    declares it."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="hypothetical_mode"):
        _make_payload(
            plus_modes=["express", "studio", "smart"],
            pro_modes=["express", "studio", "smart"],
            bucket_priority={
                "express": ["free", "subscription", "topup", "trial"],
                "studio": ["trial", "subscription", "topup", "free"],
                "smart": ["trial", "subscription", "topup", "free"],
                "hypothetical_mode": ["trial", "free"],  # no plan has this
            },
        )


def test_bucket_priority_present_in_only_plus_passes():
    """One paid plan offering the mode is enough — pro doesn't have
    to. (e.g., a future "studio-pro-only" tier; plus offering smart
    while pro doesn't would still be a valid configuration.)"""
    # Should not raise.
    _make_payload(
        plus_modes=["express", "studio", "smart"],
        pro_modes=["express", "studio"],  # no smart, but plus has it
        bucket_priority={
            "express": ["free", "subscription", "topup", "trial"],
            "studio": ["trial", "subscription", "topup", "free"],
            "smart": ["trial", "subscription", "topup", "free"],
        },
    )


def test_bucket_priority_present_in_only_free_passes():
    """Free plan also counts — express in bucket_priority + only in
    free.allowed_service_modes is OK."""
    # Should not raise.
    _make_payload(
        plus_modes=["studio"],
        pro_modes=["studio"],
        bucket_priority={
            "express": ["free", "subscription", "topup", "trial"],
            "studio": ["trial", "subscription", "topup", "free"],
        },
    )


# ─────────────────────────────────────────────────────────────────────
# pricing_admin endpoint integration — save_draft / publish reject too
# ─────────────────────────────────────────────────────────────────────


def test_admin_save_draft_rejects_drift_payload():
    """Defensive: even if someone POSTs a drifted payload directly to
    /api/admin/pricing/draft, pydantic validation in
    ``PricingPayload.model_validate`` must reject it before it touches
    the DB. (save_draft + publish both go through model_validate.)

    Path-level pin: pricing_admin.py calls model_validate; if a future
    refactor splits the endpoint into a different code path that bypasses
    pydantic, this test won't catch it. But the source-level test below
    pins that the consistency check is on the model itself, so all
    model_validate calls inherit it."""
    from pydantic import ValidationError
    from pricing_schema import PricingPayload

    drifted_raw = {
        "version": 1,
        "catalog_frozen": True,
        "plans": {
            "free": {
                "display_name": "Free",
                "max_duration_minutes": 10,
                "max_concurrent_jobs": 1,
                "allowed_service_modes": ["express"],
                "self_serve": False,
            },
            "plus": {
                "display_name": "Plus",
                "max_duration_minutes": 45,
                "max_concurrent_jobs": 3,
                "allowed_service_modes": ["express", "studio"],
                "self_serve": True,
            },
            "pro": {
                "display_name": "Pro",
                "max_duration_minutes": 180,
                "max_concurrent_jobs": 5,
                "allowed_service_modes": ["express", "studio"],
                "self_serve": True,
            },
        },
        "trial": {"fallback_plan": "free"},
        "credits": {
            "debit_rates": {"express.standard": 10, "smart.standard": 100},
            "bucket_priority": {
                "express": ["free"],
                "smart": ["trial", "subscription", "topup", "free"],
            },
        },
        "topup": {},
        "cost_model": {},
    }
    with pytest.raises(ValidationError, match="smart"):
        PricingPayload.model_validate(drifted_raw)
