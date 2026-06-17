"""Subscription truth source — read API + settlement helpers.

Task 4 introduces `Subscription` + `BillingInvoice` as the stable paid-state
and billing-history truth sources. This module holds:

- Helpers that upsert both tables during webhook settlement (called from
  `billing._process_payment_event`). These helpers are kept here rather than
  inside `billing.py` so the responsibility split stays visible at the file
  level:

    * `billing.py`           → PaymentOrder + webhook idempotency shell
    * `subscriptions.py`     → Subscription + BillingInvoice truth

- `GET /api/me/subscription` — authenticated read API that exposes the current
  user's subscription state plus trial bookkeeping facts.

Boundaries:
- Trial is NEVER mapped into a paid subscription row here.
- Usage ledger / team seats / mandates / top-up are OUT OF SCOPE.
- `PaymentOrder` is not replaced. Duplicate webhooks must remain idempotent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import BillingInvoice, PaymentOrder, Subscription, User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["subscriptions"])


# ---------------------------------------------------------------------------
# Billing-period math
# ---------------------------------------------------------------------------

# Fixed-day windows. Good enough for the current Task 4 scope and keeps the
# initial schema simple. Calendar-aware math (month-end edge cases, leap years)
# is a later-milestone concern and should live behind a helper like
# `plan_catalog.period_length(...)` when it's introduced.
_PERIOD_DAYS: dict[str, int] = {
    "monthly": 30,
    "quarterly": 90,
    "annual": 365,
}


def _period_end(start: datetime, billing_period: str) -> datetime | None:
    days = _PERIOD_DAYS.get(billing_period)
    if days is None:
        return None
    return start + timedelta(days=days)


# ---------------------------------------------------------------------------
# Settlement helpers (called from billing._process_payment_event)
# ---------------------------------------------------------------------------


async def record_invoice_for_order(
    db: AsyncSession,
    *,
    order: PaymentOrder,
    settled_at: datetime,
    status: str,
) -> BillingInvoice:
    """Record a BillingInvoice row for a PaymentOrder settlement event.

    Keyed 1:1 on `payment_order_id` via the unique constraint on the column,
    which is what makes the whole settlement flow idempotent from the invoice
    side. Behavior depends on whether an invoice row already exists:

    - No existing row → create a new BillingInvoice with the given status.
    - Existing row with the same status → replay, return unchanged.
    - Existing row `paid` + new status `refunded` → transition the row to
      `refunded` in place (T4 minor revision: refund truth must be reachable).
    - Other existing-row transitions are refused (logged by the caller as an
      unexpected state change) — this shape keeps T4 scope minimal without
      silently rewriting history.

    Returns the invoice row attached to the session (not yet committed).

    `status` must be one of: "paid" | "failed" | "refunded" | "partial_refunded".
    """
    result = await db.execute(
        select(BillingInvoice).where(
            BillingInvoice.payment_order_id == order.id
        )
    )
    invoice = result.scalar_one_or_none()

    if invoice is not None:
        if invoice.status == status:
            # Pure replay — idempotent no-op.
            return invoice
        if invoice.status == "paid" and status in {"partial_refunded", "refunded"}:
            invoice.status = status
            invoice.updated_at = settled_at
            return invoice
        if invoice.status == "partial_refunded" and status == "refunded":
            invoice.status = "refunded"
            invoice.updated_at = settled_at
            return invoice
        if invoice.status == "partial_refunded" and status == "paid":
            if invoice.paid_at is None:
                invoice.paid_at = settled_at
            invoice.updated_at = settled_at
            return invoice
        # Any other transition (e.g. paid → failed, refunded → paid) is not
        # supported in the T4 minor revision scope. Caller should not ask for
        # these; returning the unchanged row preserves the existing truth.
        logger.warning(
            "invoice transition refused: order=%s existing=%s requested=%s",
            order.id,
            invoice.status,
            status,
        )
        return invoice

    invoice = BillingInvoice(
        user_id=order.user_id,
        payment_order_id=order.id,
        provider=order.provider,
        provider_order_id=order.provider_order_id,
        plan_code=order.target_plan_code,
        billing_period=order.billing_period,
        amount_cny=order.amount_cny,
        currency=order.currency or "CNY",
        status=status,
        issued_at=settled_at,
        paid_at=settled_at if status == "paid" else None,
    )
    db.add(invoice)
    return invoice


async def upsert_active_subscription(
    db: AsyncSession,
    *,
    user: User,
    order: PaymentOrder,
    paid_at: datetime,
) -> Subscription:
    """Create or update the user's active subscription row after a paid event.

    Rules:
    - At most one `status = "active"` row per user is expected. This helper
      finds the existing active row (if any) and updates plan_code /
      billing_period / period window in place; otherwise it creates a new row.
    - Period math is fixed-day based (see `_period_end`).
    - This helper does NOT touch `user.plan_code` — that projection update is
      handled by the caller so audit-log writes stay localized there.
    """
    result = await db.execute(
        select(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.status == "active",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    existing = result.scalar_one_or_none()

    period_end = _period_end(paid_at, order.billing_period)

    if existing is None:
        sub = Subscription(
            user_id=user.id,
            plan_code=order.target_plan_code,
            billing_period=order.billing_period,
            provider=order.provider,
            status="active",
            started_at=paid_at,
            current_period_start=paid_at,
            current_period_end=period_end,
        )
        db.add(sub)
        return sub

    # Existing active row — refresh plan / provider / period.
    existing.plan_code = order.target_plan_code
    existing.billing_period = order.billing_period
    existing.provider = order.provider
    existing.current_period_start = paid_at
    existing.current_period_end = period_end
    existing.updated_at = paid_at
    return existing


# ---------------------------------------------------------------------------
# Read API — GET /api/me/subscription
# ---------------------------------------------------------------------------


def _serialize_subscription(sub: Subscription | None) -> dict | None:
    if sub is None:
        return None
    return {
        "id": str(sub.id),
        "plan_code": sub.plan_code,
        "billing_period": sub.billing_period,
        "provider": sub.provider,
        "status": sub.status,
        "started_at": sub.started_at.isoformat() if sub.started_at else None,
        "current_period_start": (
            sub.current_period_start.isoformat()
            if sub.current_period_start
            else None
        ),
        "current_period_end": (
            sub.current_period_end.isoformat() if sub.current_period_end else None
        ),
        "cancelled_at": sub.cancelled_at.isoformat() if sub.cancelled_at else None,
    }


def _serialize_trial(user: User) -> dict:
    """Surface trial bookkeeping as bookkeeping only.

    `plan_catalog.TRIAL_CONFIG.frozen` is still false, so this response MUST
    NOT invent an `ends_at` when the database column is NULL. Callers that
    want to render "X days remaining" should do so from `granted_at` +
    authoritative trial rules once those rules ship.
    """
    granted_at = getattr(user, "trial_granted_at", None)
    ends_at = getattr(user, "trial_ends_at", None)
    return {
        "granted_at": granted_at.isoformat() if granted_at else None,
        "ends_at": ends_at.isoformat() if ends_at else None,
    }


@router.get("/api/me/subscription")
async def get_my_subscription(
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict:
    """Return the current authenticated user's subscription state.

    Response shape:
    ```
    {
      "plan_code": "plus",
      "subscription_status": "active",  # "active" | "none"
      "subscription": { ... } | null,
      "trial": { "granted_at": "...", "ends_at": null }
    }
    ```

    `subscription_status` is intentionally a tiny enum. Later milestones can
    expand it to include `past_due`, `cancelled`, `expired`, etc., but Task 4
    only needs to answer "does this user currently have a paid subscription?".
    """
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    result = await db.execute(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.status == "active",
        )
    )
    active = result.scalar_one_or_none()

    return {
        "plan_code": getattr(user, "plan_code", "free") or "free",
        "subscription_status": "active" if active is not None else "none",
        "subscription": _serialize_subscription(active),
        "trial": _serialize_trial(user),
    }
