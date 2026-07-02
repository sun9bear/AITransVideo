from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class PlanPriceConfig(BaseModel):
    # Generic per-period integer triple. price_cny_fen uses CNY fen (分);
    # price_usd_cents reuses the same shape for USD cents (PayPal lane).
    monthly: int
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
    # PayPal lane USD list price (USD cents), set independently of CNY in the
    # admin pricing page (plan 2026-06-26 §5, option c). Optional so existing
    # on-disk pricing_runtime.json without this field still validates.
    price_usd_cents: PlanPriceConfig | None = None
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
    # plan 2026-06-14 §4.2：MiniMax/正式克隆 500→600。⚠️ 生产真源是
    # /opt/.../config/pricing_runtime.json（缺失才回退此默认），且**不受
    # default-OFF 保护**——发布走 admin/runtime snapshot，由项目主单独灰度。
    voice_clone_cost_credits: int = 600


# Topup SKU codes must carry this prefix (CM-01): the settle/refund lanes in
# billing.py discriminate on PaymentOrder.order_kind, and the prefix is the
# defense-in-depth convention that keeps topup codes disjoint from plan codes
# (enforced in PricingPayload.validate_cross_refs below).
TOPUP_CODE_PREFIX = "topup_"


class TopupPackage(BaseModel):
    code: str
    credits: int
    price_cny_fen: int
    # PayPal lane USD list price (USD cents), set independently of CNY like
    # plans.*.price_usd_cents (plan 2026-06-26 §5 option c). None → PayPal is
    # fail-closed hidden for this package; never derived from CNY by FX.
    price_usd_cents: int | None = None
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
    # Default keeps a pre-topup pricing_runtime.json valid (adversarial review
    # 2026-07-02 P1): without it, a missing key fails whole-payload validation
    # and pricing_runtime._load_from_file silently falls back to the hardcoded
    # defaults — wiping admin-published plan prices, not just topup. Mirrors
    # the PlanConfig.price_usd_cents optionality convention above.
    topup: TopupConfig = Field(default_factory=TopupConfig)
    cost_model: CostModelConfig

    @model_validator(mode="after")
    def validate_cross_refs(self):
        if self.trial.fallback_plan not in self.plans:
            raise ValueError("trial fallback_plan must reference an existing plan")

        # Codex follow-up B (2026-05-24): every service_mode key in
        # credits.bucket_priority must be reachable via at least one
        # plan's allowed_service_modes. Otherwise it's dead config —
        # users that pricing thinks should consume buckets in this
        # mode can never actually select it.
        #
        # This catches the exact 2026-05-24 incident: pricing_runtime
        # had bucket_priority['smart'] but no plan listed smart in
        # allowed_service_modes → entitlements API returned without
        # smart → frontend showed "即将开放" even though pricing was
        # ready. The clean-local default already satisfies this; the
        # check protects admin-edited runtime JSON from re-introducing
        # the drift.
        reachable_modes = {
            mode
            for plan in self.plans.values()
            for mode in plan.allowed_service_modes
        }
        for mode in self.credits.bucket_priority:
            if mode not in reachable_modes:
                raise ValueError(
                    f"credits.bucket_priority['{mode}'] is configured but "
                    f"no plan offers '{mode}' in allowed_service_modes "
                    f"(reachable modes: {sorted(reachable_modes)}). "
                    f"Either remove '{mode}' from bucket_priority, or add "
                    f"it to at least one plan's allowed_service_modes."
                )

        # CM-01 topup lane: package codes must be prefixed and disjoint from
        # plan codes. billing settle/refund discriminates on order_kind, but a
        # topup code that collides with a plan code (or vice versa) would make
        # order rows ambiguous to every human and admin-panel reader — reject
        # at config-validation time, before any order can be created.
        # The reverse also holds (adversarial review 2026-07-02 P2): provider
        # adapters dispatch on the code PREFIX, so a plan named "topup_*"
        # would silently break that plan's PayPal checkout.
        for plan_code in self.plans:
            if plan_code.startswith(TOPUP_CODE_PREFIX):
                raise ValueError(
                    f"plan code '{plan_code}' must not start with "
                    f"'{TOPUP_CODE_PREFIX}' (reserved for topup packages)"
                )
        seen_topup_codes: set[str] = set()
        for pkg in self.topup.packages:
            if not pkg.code.startswith(TOPUP_CODE_PREFIX):
                raise ValueError(
                    f"topup package code '{pkg.code}' must start with "
                    f"'{TOPUP_CODE_PREFIX}'"
                )
            if len(pkg.code) > 16:
                # PaymentOrder.target_plan_code / BillingInvoice.plan_code are
                # String(16) — over-long SKUs must fail here, not at checkout
                # flush on PostgreSQL (CodeX review 2026-07-02 P2).
                raise ValueError(
                    f"topup package code '{pkg.code}' exceeds 16 characters "
                    f"(payment_orders.target_plan_code column limit)"
                )
            if pkg.code in seen_topup_codes:
                raise ValueError(f"duplicate topup package code '{pkg.code}'")
            seen_topup_codes.add(pkg.code)
            if pkg.credits <= 0 or pkg.price_cny_fen <= 0:
                raise ValueError(
                    f"topup package '{pkg.code}' must have positive credits "
                    f"and price_cny_fen"
                )
        return self


def detect_frozen_field_changes(
    old: PricingPayload, new: PricingPayload
) -> list[str]:
    """Compare two payloads and return list of changed frozen field paths.

    Frozen fields:
    - plans.*.price_cny_fen
    - plans.*.price_usd_cents (PayPal lane; the actually-charged USD amount —
      flagged because a mid-flight USD price edit can false-reject an in-flight
      PayPal settlement, plan 2026-06-26 §17 B2/M6)
    - credits.debit_rates
    - trial.days, trial.source_minutes, trial.grant_credits
    """
    changes: list[str] = []

    # plans.*.price_cny_fen / plans.*.price_usd_cents
    all_plan_keys = set(old.plans.keys()) | set(new.plans.keys())
    for key in sorted(all_plan_keys):
        old_plan = old.plans.get(key)
        new_plan = new.plans.get(key)
        old_price = old_plan.price_cny_fen if old_plan else None
        new_price = new_plan.price_cny_fen if new_plan else None
        if old_price != new_price:
            changes.append(f"plans.{key}.price_cny_fen")
        old_usd = old_plan.price_usd_cents if old_plan else None
        new_usd = new_plan.price_usd_cents if new_plan else None
        if old_usd != new_usd:
            changes.append(f"plans.{key}.price_usd_cents")

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
                # PayPal USD list price (USD cents), plan 2026-06-26 §5.1.
                # $16.99 / $44.99 / $159.99 — covers PayPal cross-border +
                # FX repatriation cost so net CNY ≥ CNY list price.
                price_usd_cents=PlanPriceConfig(
                    monthly=1699,
                    quarterly=4499,
                    annual=15999,
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
                # PayPal USD list price (USD cents), plan 2026-06-26 §5.1.
                # $49.99 / $129.99 / $469.99.
                price_usd_cents=PlanPriceConfig(
                    monthly=4999,
                    quarterly=12999,
                    annual=46999,
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
                # Phase 2a free tier — free jobs never debit (credits=0). Runtime
                # truth (not just policy) so the reserve/settle path computes 0;
                # mirrored in credits_service.DEBIT_RATES frozen fallback.
                "free.standard": 0,
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
            voice_clone_cost_credits=600,  # plan 2026-06-14 §4.2（默认 fallback）
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
