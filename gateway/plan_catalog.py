"""Central source of truth for plan / pricing / entitlement facts.

This module is the single place where plan definitions, prices, and trial rules live.
Both ``billing.py`` and ``job_intercept.py`` consume from here. Frontend reads via the
public ``GET /api/plans`` endpoint exposed below.

Design principles:
- Lightweight: plain frozen dataclasses + a module-level dict. No registry, no DI.
- Replaceable: legacy views (``get_legacy_plan_gate_dict`` / ``get_legacy_price_table``)
  let existing consumers keep their import surface while the truth moves here.
- Testable: the plans-endpoint body is extracted to ``_build_plans_response`` so it
  can be asserted without spinning up FastAPI.

Trial / Pricing frozen status (2026-04-06, H1 decision):
- All pricing and Trial facts are now **frozen** by project-owner approval.
- ``TRIAL_CONFIG["frozen"]`` is ``True``. Frozen Trial rules:
  - 7 days, 20 source minutes, Studio included
  - Requires phone + captcha + risk control
  - Same phone only once; same IP only once (lifetime)
  - No auto-charge; expires to Free
- Pricing: Plus ¥99/269/999 (monthly/quarterly/annual),
  Pro ¥299/799/2999. Plus 45-min cap, Pro 5 concurrent.
- Changes to any of these values require explicit project-owner re-approval.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter


# ---------------------------------------------------------------------------
# Plan definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanPrice:
    """CNY fen (分) prices for a given billing period. ``None`` means unavailable."""

    monthly_cny_fen: int | None
    quarterly_cny_fen: int | None
    annual_cny_fen: int | None


@dataclass(frozen=True)
class PlanDefinition:
    """All facts about a single plan tier.

    Gate fields (``max_duration_minutes`` / ``max_concurrent_jobs`` /
    ``allowed_service_modes``) are consumed by ``job_intercept.py``. Pricing is
    consumed by ``billing.py``. ``self_serve`` controls whether the frontend offers
    an in-app upgrade CTA for this tier.
    """

    code: str
    display_name: str
    max_duration_minutes: int
    max_concurrent_jobs: int
    allowed_service_modes: tuple[str, ...]
    free_quota_total: int | None
    price: PlanPrice | None
    self_serve: bool


# The authoritative plan table. Values frozen by project-owner decision H1
# (2026-04-06). Changes require explicit project-owner re-approval.
PLANS: dict[str, PlanDefinition] = {
    "free": PlanDefinition(
        code="free",
        display_name="Free",
        max_duration_minutes=10,
        max_concurrent_jobs=1,
        allowed_service_modes=("express",),
        free_quota_total=5,
        price=None,
        self_serve=False,
    ),
    "plus": PlanDefinition(
        code="plus",
        display_name="Plus",
        max_duration_minutes=45,
        max_concurrent_jobs=3,
        # PR#3C-b3g (2026-05-15): smart auto-decision mode opens to
        # plus/pro tiers per plan §4.2. Smart is auto-quality-tradeoff,
        # not strictly heavier than studio — pricing tier matches studio.
        allowed_service_modes=("express", "studio", "smart"),
        free_quota_total=None,
        price=PlanPrice(
            monthly_cny_fen=9900,      # ¥99 / 月
            quarterly_cny_fen=26900,   # ¥269 / 季
            annual_cny_fen=99900,      # ¥999 / 年
        ),
        self_serve=True,
    ),
    "pro": PlanDefinition(
        code="pro",
        display_name="Pro",
        max_duration_minutes=180,
        max_concurrent_jobs=5,
        allowed_service_modes=("express", "studio", "smart"),
        free_quota_total=None,
        price=PlanPrice(
            monthly_cny_fen=29900,     # ¥299 / 月
            quarterly_cny_fen=79900,   # ¥799 / 季
            annual_cny_fen=299900,     # ¥2999 / 年
        ),
        self_serve=True,
    ),
}


VALID_BILLING_PERIODS: tuple[str, ...] = ("monthly", "quarterly", "annual")


# ---------------------------------------------------------------------------
# Trial configuration — FROZEN by project-owner decision H1 (2026-04-06)
# ---------------------------------------------------------------------------

TRIAL_CONFIG: dict[str, Any] = {
    "frozen": True,
    "days": 7,
    "source_minutes": 20,
    "includes_studio": True,
    "phone_required": True,
    "auto_charge": False,
    "fallback_plan": "free",
    "notes": (
        "Trial facts frozen 2026-04-06. 7 days, 20 source minutes, Studio included. "
        "Requires phone + captcha + risk control. Same phone only once. Same IP only once. "
        "Expires to Free. No auto-charge."
    ),
}


# ---------------------------------------------------------------------------
# Runtime pricing bridge (lazy — avoids circular imports)
# ---------------------------------------------------------------------------


def _get_runtime_plans() -> dict[str, PlanDefinition]:
    """Derive PlanDefinition dict from the runtime pricing payload.

    Falls back to the module-level ``PLANS`` snapshot if the runtime layer
    is unavailable (e.g. during early import or test isolation).
    """
    try:
        from pricing_runtime import get_runtime_pricing

        payload = get_runtime_pricing()
        result: dict[str, PlanDefinition] = {}
        for code, pc in payload.plans.items():
            price: PlanPrice | None = None
            if pc.price_cny_fen is not None:
                price = PlanPrice(
                    monthly_cny_fen=pc.price_cny_fen.monthly,
                    quarterly_cny_fen=pc.price_cny_fen.quarterly,
                    annual_cny_fen=pc.price_cny_fen.annual,
                )
            result[code] = PlanDefinition(
                code=code,
                display_name=pc.display_name,
                max_duration_minutes=pc.max_duration_minutes,
                max_concurrent_jobs=pc.max_concurrent_jobs,
                allowed_service_modes=tuple(pc.allowed_service_modes),
                free_quota_total=pc.free_quota_total,
                price=price,
                self_serve=pc.self_serve,
            )
        return result
    except Exception:
        return PLANS


def _get_runtime_trial_config() -> dict[str, Any]:
    """Derive trial config dict from the runtime pricing payload.

    Falls back to the module-level ``TRIAL_CONFIG`` snapshot on failure.
    The ``notes`` field is always sourced from the frozen ``TRIAL_CONFIG``
    since it has no counterpart in ``TrialConfig``.
    """
    try:
        from pricing_runtime import get_runtime_pricing

        trial = get_runtime_pricing().trial
        result: dict[str, Any] = {
            "frozen": trial.frozen,
            "days": trial.days,
            "source_minutes": trial.source_minutes,
            "includes_studio": trial.includes_studio,
            "phone_required": trial.phone_required,
            "auto_charge": trial.auto_charge,
            "fallback_plan": trial.fallback_plan,
        }
        # Preserve the frozen notes string from the module-level constant
        if "notes" in TRIAL_CONFIG:
            result["notes"] = TRIAL_CONFIG["notes"]
        return result
    except Exception:
        return TRIAL_CONFIG


# ---------------------------------------------------------------------------
# Helpers (consumed by billing.py / job_intercept.py / tests)
# ---------------------------------------------------------------------------


def list_plan_codes() -> list[str]:
    return list(_get_runtime_plans().keys())


def get_plan(code: str) -> PlanDefinition:
    """Return the plan definition, falling back to ``free`` if unknown."""
    plans = _get_runtime_plans()
    return plans.get(code, plans["free"])


def get_price(plan_code: str, billing_period: str) -> int | None:
    """Return the price in CNY fen, or ``None`` if unavailable."""
    plan = _get_runtime_plans().get(plan_code)
    if plan is None or plan.price is None:
        return None
    if billing_period == "monthly":
        return plan.price.monthly_cny_fen
    if billing_period == "quarterly":
        return plan.price.quarterly_cny_fen
    if billing_period == "annual":
        return plan.price.annual_cny_fen
    return None


def valid_target_plan_codes() -> set[str]:
    """Plan codes that can be the *target* of a paid upgrade (i.e. priced plans)."""
    return {code for code, plan in _get_runtime_plans().items() if plan.price is not None}


def is_user_in_active_trial(user) -> bool:
    """Return True if the user is currently within a valid trial window.

    Checks ``trial_granted_at`` and ``trial_ends_at`` on the user object.
    Does NOT check ``plan_code`` — trial is a temporary overlay, not a tier.
    """
    from datetime import datetime, timezone

    granted = getattr(user, "trial_granted_at", None)
    ends = getattr(user, "trial_ends_at", None)
    if not granted or not ends:
        return False
    now = datetime.now(timezone.utc)
    return now < ends


def get_effective_plan_gate(user) -> dict[str, Any]:
    """Return the plan gate dict for this user, accounting for active trial.

    If the user is in an active trial window, their capabilities are elevated
    to match ``TRIAL_CONFIG`` rules (Studio included, Plus-like duration/
    concurrency) while ``user.plan_code`` stays ``"free"``. This keeps trial
    as a temporary overlay, not a paid tier.

    Consumers (``entitlements.py``, ``job_intercept.py``) should call this
    instead of directly reading ``PLAN_CATALOG[user.plan_code]``.
    """
    plan_code = getattr(user, "plan_code", "free") or "free"
    base = get_legacy_plan_gate_dict().get(plan_code, get_legacy_plan_gate_dict()["free"])

    if is_user_in_active_trial(user) and _get_runtime_trial_config().get("frozen"):
        # Overlay trial capabilities on top of the base plan.
        # Trial includes Studio and gets Plus-tier duration/concurrency limits.
        plus_gate = get_legacy_plan_gate_dict().get("plus", base)
        return {
            "max_duration_minutes": plus_gate["max_duration_minutes"],
            "max_concurrent_jobs": plus_gate["max_concurrent_jobs"],
            "allowed_service_modes": list(plus_gate["allowed_service_modes"]),
            # Preserve free_quota_total if present in base (trial doesn't change quota)
            **({k: base[k] for k in ("free_quota_total",) if k in base}),
        }

    return dict(base)


def get_legacy_plan_gate_dict() -> dict[str, dict[str, Any]]:
    """Return the legacy ``PLAN_CATALOG`` shape historically exposed by ``job_intercept``.

    Kept so existing call sites and tests that import ``PLAN_CATALOG`` from
    ``job_intercept`` keep working while the underlying truth moves here.
    """
    result: dict[str, dict[str, Any]] = {}
    for code, plan in _get_runtime_plans().items():
        entry: dict[str, Any] = {
            "max_duration_minutes": plan.max_duration_minutes,
            "max_concurrent_jobs": plan.max_concurrent_jobs,
            "allowed_service_modes": list(plan.allowed_service_modes),
        }
        if plan.free_quota_total is not None:
            entry["free_quota_total"] = plan.free_quota_total
        result[code] = entry
    return result


def get_legacy_price_table() -> dict[tuple[str, str], int]:
    """Return the legacy ``PLAN_PRICES_CNY`` shape historically exposed by ``billing``."""
    result: dict[tuple[str, str], int] = {}
    for code, plan in _get_runtime_plans().items():
        if plan.price is None:
            continue
        if plan.price.monthly_cny_fen is not None:
            result[(code, "monthly")] = plan.price.monthly_cny_fen
        if plan.price.quarterly_cny_fen is not None:
            result[(code, "quarterly")] = plan.price.quarterly_cny_fen
        if plan.price.annual_cny_fen is not None:
            result[(code, "annual")] = plan.price.annual_cny_fen
    return result


# ---------------------------------------------------------------------------
# Public API — GET /api/plans
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api", tags=["plans"])


def _plan_to_public_dict(plan: PlanDefinition) -> dict[str, Any]:
    """Serialize a plan for the public ``/api/plans`` response.

    All fields here are safe to show anonymously. See the API contract doc at
    ``docs/specs/2026-04-04-pricing-and-plans-api-contract.md``.
    """
    if plan.price is None:
        price_payload: dict[str, int | None] | None = None
    else:
        price_payload = {
            "monthly": plan.price.monthly_cny_fen,
            "quarterly": plan.price.quarterly_cny_fen,
            "annual": plan.price.annual_cny_fen,
        }
    entry: dict[str, Any] = {
        "code": plan.code,
        "display_name": plan.display_name,
        "max_duration_minutes": plan.max_duration_minutes,
        "max_concurrent_jobs": plan.max_concurrent_jobs,
        "allowed_service_modes": list(plan.allowed_service_modes),
        "self_serve": plan.self_serve,
        "price_cny_fen": price_payload,
    }
    if plan.free_quota_total is not None:
        entry["free_quota_total"] = plan.free_quota_total

    # Monthly subscription credit grant. Plus / Pro get a fresh bucket per
    # billing period (see credits_service.ensure_subscription_bucket).
    # Free tier gets a recurring small grant too (free_grant_credits).
    # Marketing pricing card uses this together with `credits_per_minute`
    # (top-level on the response) to display approximate "约 N 分钟 Express
    # / N 分钟 Studio" per tier so the buyer can compare the headline price
    # against what they actually get.
    grants = _get_runtime_grant_amounts_safe()
    grant = grants.get(plan.code)
    if grant is not None:
        entry["monthly_grant_credits"] = int(grant)

    return entry


def _get_runtime_grant_amounts_safe() -> dict[str, int]:
    """Local mirror of credits_service._get_runtime_grant_amounts.

    Duplicated as a local helper to avoid a hard import dependency on
    credits_service from the public-facing plans module — keeps the
    /api/plans endpoint resilient if credits_service has a transient
    import error during startup. Always falls back to the frozen V3
    GRANT_AMOUNTS dict on any failure.
    """
    try:
        from credits_service import GRANT_AMOUNTS as _GA  # type: ignore
    except Exception:
        _GA = {"free": 500, "trial": 300, "plus": 3500, "pro": 12000}

    try:
        from pricing_runtime import get_runtime_pricing  # type: ignore
        payload = get_runtime_pricing()
        result: dict[str, int] = {}
        # Free tier grant from credits config
        free = getattr(getattr(payload, "credits", None), "free_grant_credits", None)
        if free is not None:
            result["free"] = int(free)
        # Trial bucket (kept for consistency, not exposed in plans response)
        trial = getattr(getattr(payload, "trial", None), "grant_credits", None)
        if trial is not None:
            result["trial"] = int(trial)
        # Per-plan grants
        plans_dict = getattr(payload, "plans", {}) or {}
        for code, plan in plans_dict.items():
            mgc = getattr(plan, "monthly_grant_credits", None)
            if mgc is not None:
                result[code] = int(mgc)
        return result if result else dict(_GA)
    except Exception:
        return dict(_GA)


def _get_runtime_debit_rates_safe() -> dict[str, int]:
    """Flatten DEBIT_RATES into the public credits_per_minute payload.

    Output shape: {"express_standard": 10, "studio_standard": 15, ...}
    derived from credits_service.DEBIT_RATES which is keyed by
    (service_mode, quality_tier) tuples internally. The flat string-key
    form is friendlier to JSON consumers (no tuple-key gymnastics).

    Marketing pricing card uses Express standard + Studio standard as
    the two reference rates to display "约 N min Express / M min Studio"
    on each paid tier.
    """
    try:
        from credits_service import DEBIT_RATES as _DR  # type: ignore
        flat: dict[str, int] = {}
        for (mode, tier), val in _DR.items():
            flat[f"{mode}_{tier}"] = int(val)
        return flat
    except Exception:
        # Frozen V3 fallback
        return {
            "express_standard": 10,
            "studio_standard": 15,
            "studio_high": 30,
            "studio_flagship": 50,
        }


def _build_plans_response() -> dict[str, Any]:
    """Build the ``/api/plans`` response dict (extracted for unit testing)."""
    return {
        "plans": [_plan_to_public_dict(plan) for plan in _get_runtime_plans().values()],
        "trial": dict(_get_runtime_trial_config()),
        "credits_per_minute": _get_runtime_debit_rates_safe(),
    }


@router.get("/plans")
async def get_plans_endpoint() -> dict[str, Any]:
    """Public (no-auth) plan catalog for the marketing / pricing UI.

    This endpoint is intentionally unauthenticated — it is the only supported way
    for an anonymous marketing visitor to obtain pricing and plan entitlements.
    Do not add ``Depends(require_auth)`` here.
    """
    return _build_plans_response()
