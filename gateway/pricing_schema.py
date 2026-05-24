from __future__ import annotations

from pydantic import BaseModel, model_validator


class PlanPriceConfig(BaseModel):
    monthly: int    # CNY fen
    quarterly: int
    annual: int


class PlanConfig(BaseModel):
    display_name: str
    free_quota_total: int | None = None
    max_duration_minutes: int
    max_concurrent_jobs: int
    allowed_service_modes: list[str]
    self_serve: bool
    price_cny_fen: PlanPriceConfig | None = None
    monthly_grant_credits: int | None = None


class TrialConfig(BaseModel):
    frozen: bool = True
    days: int = 7
    source_minutes: int = 20
    includes_studio: bool = True
    phone_required: bool = True
    auto_charge: bool = False
    fallback_plan: str = "free"
    grant_credits: int = 300


class CreditsConfig(BaseModel):
    free_grant_credits: int = 500
    debit_rates: dict[str, int]  # "express.standard" -> 10
    bucket_priority: dict[str, list[str]]
    voice_clone_cost_credits: int = 500


class TopupPackage(BaseModel):
    code: str
    credits: int
    price_cny_fen: int
    active: bool = True
    sort_order: int = 0


class TopupConfig(BaseModel):
    enabled: bool = False
    packages: list[TopupPackage] = []


class CostModelConfig(BaseModel):
    point_cost_rmb: float = 0.015
    point_price_rmb: float = 0.03
    target_gross_margin: float = 0.5
    k_cn_chars_per_src_min: int = 250
    fx_usd_cny: float = 7.0
    translate_cost_rmb_per_src_min: float = 0.03
    s2_review_cost_rmb_per_src_min: float = 0.02
    rewrite_cost_rmb_per_src_min: float = 0.02
    server_cost_rmb_per_src_min: float = 0.03


class PricingPayload(BaseModel):
    version: int = 1
    catalog_frozen: bool = True
    plans: dict[str, PlanConfig]
    trial: TrialConfig
    credits: CreditsConfig
    topup: TopupConfig
    cost_model: CostModelConfig

    @model_validator(mode="after")
    def validate_cross_refs(self):
        if self.trial.fallback_plan not in self.plans:
            raise ValueError("trial fallback_plan must reference an existing plan")
        return self


def detect_frozen_field_changes(
    old: PricingPayload, new: PricingPayload
) -> list[str]:
    """Compare two payloads and return list of changed frozen field paths.

    Frozen fields:
    - plans.*.price_cny_fen
    - credits.debit_rates
    - trial.days, trial.source_minutes, trial.grant_credits
    """
    changes: list[str] = []

    # plans.*.price_cny_fen
    all_plan_keys = set(old.plans.keys()) | set(new.plans.keys())
    for key in sorted(all_plan_keys):
        old_plan = old.plans.get(key)
        new_plan = new.plans.get(key)
        old_price = old_plan.price_cny_fen if old_plan else None
        new_price = new_plan.price_cny_fen if new_plan else None
        if old_price != new_price:
            changes.append(f"plans.{key}.price_cny_fen")

    # credits.debit_rates
    if old.credits.debit_rates != new.credits.debit_rates:
        changes.append("credits.debit_rates")

    # trial frozen fields
    if old.trial.days != new.trial.days:
        changes.append("trial.days")
    if old.trial.source_minutes != new.trial.source_minutes:
        changes.append("trial.source_minutes")
    if old.trial.grant_credits != new.trial.grant_credits:
        changes.append("trial.grant_credits")

    return changes


def build_default_pricing_payload() -> PricingPayload:
    """Return the current frozen pricing payload with all default values."""
    return PricingPayload(
        plans={
            "free": PlanConfig(
                display_name="Free",
                free_quota_total=5,
                max_duration_minutes=10,
                max_concurrent_jobs=1,
                allowed_service_modes=["express"],
                self_serve=False,
            ),
            "plus": PlanConfig(
                display_name="Plus",
                price_cny_fen=PlanPriceConfig(
                    monthly=9900,
                    quarterly=26900,
                    annual=99900,
                ),
                max_duration_minutes=45,
                max_concurrent_jobs=3,
                # Task #24 (P2 launch blocker #2): smart added to mirror
                # plan_catalog.py PLANS — clean-local and production must
                # agree on Plus tier entitlement.
                allowed_service_modes=["express", "studio", "smart"],
                self_serve=True,
                monthly_grant_credits=3500,
            ),
            "pro": PlanConfig(
                display_name="Pro",
                price_cny_fen=PlanPriceConfig(
                    monthly=29900,
                    quarterly=79900,
                    annual=299900,
                ),
                max_duration_minutes=180,
                max_concurrent_jobs=5,
                # Task #24 (P2 launch blocker #2): same as Plus — smart
                # is a Plus/Pro feature in the canonical plan_catalog.
                allowed_service_modes=["express", "studio", "smart"],
                self_serve=True,
                monthly_grant_credits=12000,
            ),
        },
        trial=TrialConfig(
            frozen=True,
            days=7,
            source_minutes=20,
            includes_studio=True,
            phone_required=True,
            auto_charge=False,
            fallback_plan="free",
            grant_credits=300,
        ),
        credits=CreditsConfig(
            free_grant_credits=500,
            debit_rates={
                "express.standard": 10,
                "studio.standard": 15,
                "studio.high": 30,
                "studio.flagship": 50,
                # Task #24 (P2 launch blocker #2): smart fixed rate per
                # docs/plans/2026-05-13-smart-mvp-p2-implementation-plan.md §5.3.
                # Without this entry, _get_runtime_debit_rates() falls back to
                # DEFAULT_DEBIT_RATE=10 for smart — 10× under-reservation.
                "smart.standard": 100,
            },
            bucket_priority={
                "express": ["free", "subscription", "topup", "trial"],
                "studio": ["trial", "subscription", "topup", "free"],
                # Task #24: smart is a paid feature like studio — consume
                # paid buckets (trial/subscription/topup) before free, so
                # paid Plus/Pro grants don't subsidize free quota usage.
                "smart": ["trial", "subscription", "topup", "free"],
            },
            voice_clone_cost_credits=500,
        ),
        topup=TopupConfig(
            enabled=False,
            packages=[
                TopupPackage(
                    code="topup_1000",
                    credits=1000,
                    price_cny_fen=3900,
                    active=True,
                    sort_order=10,
                ),
                TopupPackage(
                    code="topup_3000",
                    credits=3000,
                    price_cny_fen=9900,
                    active=True,
                    sort_order=20,
                ),
            ],
        ),
        cost_model=CostModelConfig(
            point_cost_rmb=0.015,
            point_price_rmb=0.03,
            target_gross_margin=0.5,
            k_cn_chars_per_src_min=250,
            fx_usd_cny=7.0,
            translate_cost_rmb_per_src_min=0.03,
            s2_review_cost_rmb_per_src_min=0.02,
            rewrite_cost_rmb_per_src_min=0.02,
            server_cost_rmb_per_src_min=0.03,
        ),
    )
