"""Billing API: order creation, order query, webhook processing.

Phase 6 architecture:
- Provider abstraction via payment_providers.py
- All provider-specific logic (checkout, signature, webhook parsing) goes through adapters
- Core settlement logic (_process_payment_event) remains provider-agnostic
- Key invariants preserved:
  - Webhook processing is idempotent (keyed on provider_event_id)
  - Payment only modifies user entitlements, never touches job snapshots
  - Unverified signatures are recorded but never settle orders
  - Duplicate callbacks do not re-upgrade plan_code
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import (
    AdminAuditLog,
    BillingInvoice,
    PaymentOrder,
    PaymentWebhookEvent,
    User,
)
from payment_providers import get_provider, is_provider_operational, list_providers
from plan_catalog import (
    VALID_BILLING_PERIODS as _CATALOG_BILLING_PERIODS,
    get_legacy_price_table,
    get_price,
    valid_target_plan_codes,
)
from subscriptions import (
    record_invoice_for_order,
    upsert_active_subscription,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["billing"])

# Frozen import-time snapshots kept for backward-compatible test imports.
# Request-time code paths call the live functions (valid_target_plan_codes(),
# get_price()) directly so they pick up runtime pricing changes.
VALID_TARGET_PLANS: set[str] = valid_target_plan_codes()
VALID_BILLING_PERIODS: set[str] = set(_CATALOG_BILLING_PERIODS)
PLAN_PRICES_CNY: dict[tuple[str, str], int] = get_legacy_price_table()

ORDER_EXPIRY_MINUTES = 30


# --- Request/Response models ---

class CreateOrderRequest(BaseModel):
    target_plan_code: str
    billing_period: str = "monthly"
    provider: str = "fake"


# --- Order creation (provider-dispatched) ---

@router.post("/orders")
async def create_order(
    body: CreateOrderRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict:
    """Create a payment order for plan upgrade, dispatched through provider adapter."""
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    if body.target_plan_code not in valid_target_plan_codes():
        raise HTTPException(status_code=400, detail=f"无效的目标套餐: {body.target_plan_code}")
    if body.billing_period not in VALID_BILLING_PERIODS:
        raise HTTPException(status_code=400, detail=f"无效的计费周期: {body.billing_period}")

    # Resolve provider
    try:
        provider = get_provider(body.provider)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"不支持的支付渠道: {body.provider}")

    # Check if provider is operational (not a stub)
    if not is_provider_operational(body.provider):
        raise HTTPException(
            status_code=501,
            detail=f"支付渠道 {body.provider} 尚未接入，当前仅支持: {', '.join(p for p in list_providers() if is_provider_operational(p))}"
        )

    current_plan = getattr(user, "plan_code", "free") or "free"
    plan_rank = {"free": 0, "plus": 1, "pro": 2}
    if plan_rank.get(body.target_plan_code, 0) <= plan_rank.get(current_plan, 0):
        raise HTTPException(
            status_code=400,
            detail=f"当前套餐({current_plan})已等于或高于目标套餐({body.target_plan_code})"
        )

    amount = get_price(body.target_plan_code, body.billing_period) or 0
    if amount <= 0:
        raise HTTPException(status_code=400, detail="无法确定价格")

    now = datetime.now(timezone.utc)
    order = PaymentOrder(
        user_id=user.id,
        provider=body.provider,
        target_plan_code=body.target_plan_code,
        billing_period=body.billing_period,
        amount_cny=amount,
        status="created",
        expires_at=now + timedelta(minutes=ORDER_EXPIRY_MINUTES),
    )
    db.add(order)
    # Flush to get order.id without committing — if adapter fails, we rollback
    await db.flush()

    # Create checkout through provider adapter
    try:
        checkout = provider.create_checkout(
            order_id=str(order.id),
            amount_cny=amount,
            target_plan_code=body.target_plan_code,
            billing_period=body.billing_period,
        )
    except Exception as exc:
        await db.rollback()
        logger.error("Provider %s checkout failed for order %s: %s",
                      body.provider, order.id, exc)
        raise HTTPException(
            status_code=502,
            detail=f"支付渠道创建 checkout 失败: {exc}"
        )

    order.checkout_url = checkout.checkout_url
    if checkout.provider_order_id:
        order.provider_order_id = checkout.provider_order_id
    order.status = "pending"
    await db.commit()

    logger.info("Order %s created via %s for user %s: %s %s ¥%.2f",
                order.id, body.provider, user.id,
                body.target_plan_code, body.billing_period, amount / 100)

    return {
        "order_id": str(order.id),
        "status": order.status,
        "amount_cny": order.amount_cny,
        "target_plan_code": order.target_plan_code,
        "billing_period": order.billing_period,
        "provider": order.provider,
        "checkout_url": checkout.checkout_url,
        "expires_at": order.expires_at.isoformat() if order.expires_at else None,
    }


# --- Order query ---

@router.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    result = await db.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if str(order.user_id) != str(user.id):
        role = getattr(user, "role", "user") or "user"
        if role != "admin":
            raise HTTPException(status_code=403, detail="无权查看此订单")

    return {
        "order_id": str(order.id),
        "status": order.status,
        "amount_cny": order.amount_cny,
        "target_plan_code": order.target_plan_code,
        "billing_period": order.billing_period,
        "provider": order.provider,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


# --- Checkout config (Task 5) ---


_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "fake": "测试支付",
    "alipay": "支付宝",
    "wechatpay": "微信支付",
    "stripe": "Stripe",
}


def _display_name(provider_code: str) -> str:
    return _PROVIDER_DISPLAY_NAMES.get(provider_code, provider_code)


@router.get("/checkout-config")
async def get_checkout_config(
    user: User | None = Depends(get_current_user),
) -> dict:
    """Return the list of checkout providers currently usable by this gateway.

    Gateway owns provider availability (Task 5 §"Gateway owns provider
    availability"). The frontend must read this endpoint rather than deciding
    operational state from env vars or hardcoded client-side logic.

    Availability rule used here:
    - every known provider is listed with its `operational` flag
    - `default_provider` is the first operational provider in preference order
      [alipay, wechatpay, stripe, fake]
    - if nothing else is operational, `fake` is the default (local dev safety)

    Pricing facts are NOT returned here. Prices continue to come from
    `/api/plans`. This endpoint is strictly about "can we currently charge a
    card via X" — nothing more.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    preference = ["alipay", "wechatpay", "stripe", "fake"]
    all_providers = list_providers()

    providers_payload: list[dict] = []
    # Emit in preference order so the frontend's default pick (first entry)
    # matches `default_provider` without extra sorting on the client.
    for code in preference:
        if code in all_providers:
            providers_payload.append(
                {
                    "code": code,
                    "display_name": _display_name(code),
                    "operational": is_provider_operational(code),
                }
            )
    # Surface any provider not in the preference list (future providers) at
    # the end, keeping the contract open to additions.
    for code in all_providers:
        if code not in preference:
            providers_payload.append(
                {
                    "code": code,
                    "display_name": _display_name(code),
                    "operational": is_provider_operational(code),
                }
            )

    default_provider = next(
        (p["code"] for p in providers_payload if p["operational"]),
        "fake",
    )

    return {
        "default_provider": default_provider,
        "providers": providers_payload,
    }


# --- Billing history (Task 4) ---


def _serialize_invoice(invoice: BillingInvoice) -> dict:
    return {
        "id": str(invoice.id),
        "subscription_id": (
            str(invoice.subscription_id) if invoice.subscription_id else None
        ),
        "payment_order_id": str(invoice.payment_order_id),
        "provider": invoice.provider,
        "provider_order_id": invoice.provider_order_id,
        "plan_code": invoice.plan_code,
        "billing_period": invoice.billing_period,
        "amount_cny": invoice.amount_cny,
        "currency": invoice.currency,
        "status": invoice.status,
        "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "created_at": (
            invoice.created_at.isoformat() if invoice.created_at else None
        ),
    }


@router.get("/history")
async def list_billing_history(
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict:
    """Return the current user's billing invoice history, newest first.

    Authenticated endpoint. Scoped strictly to `user.id` — admins do NOT get
    a tenant-wide view from this path; that belongs to a separate admin
    endpoint (out of Task 4 scope).

    No pagination framework, no filters, no export. Task 6 Billing UI can
    consume this directly.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    result = await db.execute(
        select(BillingInvoice)
        .where(BillingInvoice.user_id == user.id)
        .order_by(BillingInvoice.created_at.desc())
    )
    invoices = list(result.scalars().all())
    return {"invoices": [_serialize_invoice(inv) for inv in invoices]}


# --- Fake pay endpoint ---
#
# The fake provider returns `/api/billing/fake-pay/{order_id}` as its
# `checkout_url`. The frontend hands this URL off via `window.location.href`,
# which produces a **GET** navigation. Prior to the T5 minor revision only a
# POST handler existed here, so the normal browser path 405'd. We expose both:
#
#   POST /fake-pay/{order_id}  → JSON result, for programmatic callers / tests
#   GET  /fake-pay/{order_id}  → settles + 303 redirect back into the app
#
# Both routes share `_run_fake_payment` so the settlement logic stays single-
# sourced.


async def _run_fake_payment(order_id: str, db: AsyncSession) -> dict:
    """Run the fake-pay settlement flow for an order.

    Returns a dict with `{ok, settled, order_id, already_settled, not_found}`.
    Callers translate that into JSON (POST) or a redirect (GET).
    """
    result = await db.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        return {
            "ok": False,
            "settled": False,
            "order_id": order_id,
            "not_found": True,
            "already_settled": False,
        }
    if order.status not in ("created", "pending"):
        # Non-fatal for the browser flow — a duplicate click on the checkout
        # link is a normal user error, not a 500. The GET handler will still
        # redirect the user back to `/settings/billing` so they can see the
        # current state.
        return {
            "ok": True,
            "settled": False,
            "order_id": str(order.id),
            "not_found": False,
            "already_settled": True,
        }

    fake_event_id = f"fake_evt_{uuid.uuid4().hex[:12]}"
    settled = await _process_payment_event(
        db=db,
        provider="fake",
        provider_event_id=fake_event_id,
        event_type="payment.success",
        order_id=str(order.id),
        new_status="paid",
        signature_valid=True,  # fake provider: signature always valid
        raw_payload={"simulated": True, "order_id": str(order.id)},
    )
    return {
        "ok": True,
        "settled": settled,
        "order_id": str(order.id),
        "not_found": False,
        "already_settled": False,
    }


@router.post("/fake-pay/{order_id}")
async def fake_pay(
    order_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Simulate a successful payment via fake provider adapter (JSON path).

    Preserves the original POST contract: returns JSON and raises HTTP 404/409
    on not-found / already-terminal orders so programmatic callers (tests,
    scripts) still see structured errors.
    """
    result = await _run_fake_payment(order_id, db)
    if result["not_found"]:
        raise HTTPException(status_code=404, detail="订单不存在")
    if result["already_settled"]:
        raise HTTPException(status_code=409, detail="订单已处理,无法重复支付")
    return {
        "ok": result["ok"],
        "settled": result["settled"],
        "order_id": result["order_id"],
    }


@router.get("/fake-pay/{order_id}")
async def fake_pay_browser(
    order_id: str,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Browser-navigable fake-pay endpoint.

    Matches the URL that `FakeProvider.create_checkout` returns as its
    `checkout_url`. This exists so the default local/test checkout loop works
    end-to-end from inside `/settings/billing`:

      1. User clicks "立即支付" in `<CheckoutCard>`
      2. Frontend `POST /api/billing/orders` → receives `checkout_url`
      3. Frontend `window.location.href = checkout_url` → GET this endpoint
      4. We settle the order and 303-redirect back to `/settings/billing` with
         a status query param so the page can toast / refresh subscription state.

    Never raises 404/409 to the browser — always lands the user back in-app
    with a status that the billing page can render. HTTP 303 "See Other" is
    the correct redirect for a GET that performs a write-then-navigate.
    """
    result = await _run_fake_payment(order_id, db)
    if result["not_found"]:
        return RedirectResponse(
            url="/settings/billing?status=error&reason=order_not_found",
            status_code=303,
        )
    if result["already_settled"]:
        return RedirectResponse(
            url="/settings/billing?status=already_settled",
            status_code=303,
        )
    return RedirectResponse(
        url="/settings/billing?status=paid",
        status_code=303,
    )


# --- Webhook endpoint (provider-dispatched) ---

@router.post("/webhooks/{provider_name}")
async def receive_webhook(
    provider_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Receive and process a payment webhook through provider adapter.

    Flow:
    1. Resolve provider adapter
    2. Verify signature via adapter.verify_signature()
    3. Parse payload via adapter.parse_webhook()
    4. Pass to _process_payment_event with verified signature_valid
    """
    raw_body = await request.body()
    headers = dict(request.headers)

    # Resolve provider
    try:
        provider = get_provider(provider_name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider_name}")

    # Verify signature through adapter
    try:
        signature_valid = provider.verify_signature(raw_body, headers)
    except NotImplementedError:
        # Stub provider — signature verification not implemented
        signature_valid = False
    except Exception as exc:
        logger.warning("Signature verification error for %s: %s", provider_name, exc)
        signature_valid = False

    # Parse webhook through adapter
    try:
        event = provider.parse_webhook(raw_body)
    except NotImplementedError:
        # Stub provider — record raw payload as-is, mark unverified
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except Exception:
            payload = {}
        event_id = payload.get("provider_event_id", "")
        if not event_id:
            raise HTTPException(status_code=400, detail="missing provider_event_id")
        settled = await _process_payment_event(
            db=db,
            provider=provider_name,
            provider_event_id=event_id,
            event_type=payload.get("event_type", "unknown"),
            order_id=payload.get("order_id", ""),
            new_status=payload.get("status", ""),
            signature_valid=False,  # stub provider cannot verify
            raw_payload=payload,
        )
        return {"ok": True, "settled": settled}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}")

    if not event.provider_event_id:
        raise HTTPException(status_code=400, detail="missing provider_event_id")

    settled = await _process_payment_event(
        db=db,
        provider=provider_name,
        provider_event_id=event.provider_event_id,
        event_type=event.event_type,
        order_id=event.order_id,
        new_status=event.new_status,
        signature_valid=signature_valid,
        raw_payload=event.raw_payload,
    )

    return {"ok": True, "settled": settled}


# --- Core processing logic (provider-agnostic) ---

async def _process_payment_event(
    *,
    db: AsyncSession,
    provider: str,
    provider_event_id: str,
    event_type: str,
    order_id: str,
    new_status: str,
    signature_valid: bool,
    raw_payload: dict | None = None,
) -> bool:
    """Process a payment event idempotently. Returns True if entitlements were updated.

    This function is provider-agnostic. All provider-specific logic (signature
    verification, payload parsing, status mapping) happens in the caller.

    Args:
        signature_valid: Whether the webhook signature was verified by the provider adapter.
            Unverified events are recorded but never settle orders or upgrade plans.
    """
    # --- Idempotency check ---
    existing = await db.execute(
        select(PaymentWebhookEvent).where(
            PaymentWebhookEvent.provider_event_id == provider_event_id
        )
    )
    if existing.scalar_one_or_none() is not None:
        logger.info("Duplicate webhook event %s, skipping", provider_event_id)
        return False

    # Record the event
    now = datetime.now(timezone.utc)
    event = PaymentWebhookEvent(
        provider=provider,
        provider_event_id=provider_event_id,
        event_type=event_type,
        signature_valid=signature_valid,
        processed=False,
        payload=raw_payload,
        received_at=now,
    )
    db.add(event)

    # Find the order
    result = await db.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        event.processed = True
        event.error_message = f"Order {order_id} not found"
        event.processed_at = now
        await db.commit()
        logger.warning("Webhook for unknown order %s", order_id)
        return False

    # Terminal-state guard.
    #
    # Most combinations of (current order status, new status) are replays and
    # must short-circuit to preserve webhook idempotency. The single exception
    # is the paid → refunded transition, which a real provider refund callback
    # depends on to keep `billing_invoices.status` truthful. Without allowing
    # that transition here, a later refund webhook would be silently rejected
    # and billing history would lie.
    _is_paid_to_refund = order.status == "paid" and new_status == "refunded"
    if order.status in ("paid", "refunded", "cancelled") and not _is_paid_to_refund:
        event.processed = True
        event.error_message = f"Order already in terminal state: {order.status}"
        event.processed_at = now
        await db.commit()
        logger.info("Order %s already %s, skipping", order_id, order.status)
        return False

    # Guard: unverified signatures are recorded but never settle
    if not signature_valid:
        event.processed = True
        event.error_message = "Signature not verified — event recorded but not settled"
        event.processed_at = now
        await db.commit()
        logger.warning("Unverified webhook %s for order %s — recorded, not settled",
                        provider_event_id, order_id)
        return False

    # Update order status
    order.status = new_status
    if new_status == "paid":
        order.paid_at = now

    entitlements_updated = False

    if new_status == "paid":
        # --- Task 4 settlement order ---
        # 1. PaymentOrder status was already updated above.
        # 2. Write or update BillingInvoice (idempotent via unique
        #    `payment_order_id`; duplicate callbacks find the existing row).
        # 3. Create or update the user's active Subscription row.
        # 4. Only THEN update `user.plan_code` — the compatibility projection
        #    current gates still rely on. `subscriptions` is the canonical
        #    paid-state record; `user.plan_code` mirrors it so
        #    `entitlements.py` and `job_intercept.py` don't need to change.
        user_result = await db.execute(select(User).where(User.id == order.user_id))
        user = user_result.scalar_one_or_none()

        invoice = await record_invoice_for_order(
            db, order=order, settled_at=now, status="paid"
        )

        if user is not None:
            subscription = await upsert_active_subscription(
                db, user=user, order=order, paid_at=now
            )
            # Link invoice ↔ subscription so later Billing UI can render the
            # relationship without guessing. `flush` so the auto-generated PK
            # on a brand-new subscription row is available here.
            await db.flush()
            invoice.subscription_id = subscription.id

            old_plan = user.plan_code
            if old_plan != order.target_plan_code:
                user.plan_code = order.target_plan_code
                db.add(AdminAuditLog(
                    admin_user_id=user.id,
                    target_user_id=user.id,
                    action="payment_upgrade",
                    field_name="plan_code",
                    old_value=old_plan,
                    new_value=order.target_plan_code,
                ))
                entitlements_updated = True

            # V3-1 shadow: create subscription credits bucket (best-effort)
            try:
                from credits_service import ensure_subscription_bucket
                period_end = subscription.current_period_end if subscription else None
                await ensure_subscription_bucket(
                    db, user_id=user.id, plan_code=order.target_plan_code,
                    related_order_id=order.id, related_subscription_id=subscription.id,
                    expires_at=period_end,
                )
            except Exception:
                logger.warning("V3 shadow subscription grant failed (non-fatal)")
                logger.info("User %s upgraded %s → %s via payment order %s",
                            user.id, old_plan, order.target_plan_code, order_id)

    elif new_status == "refunded":
        # Refund truth layer (Task 4 minor revision): update billing history
        # to reflect the refund. We deliberately do NOT touch:
        #   - the user's active Subscription row (cancellation UX is Task 5/6)
        #   - `user.plan_code` (entitlement rollback UX is Task 5/6)
        # Only `billing_invoices.status` is made truthful here.
        await record_invoice_for_order(
            db, order=order, settled_at=now, status="refunded"
        )

    elif new_status == "failed":
        # Keep billing history honest for failed settlement attempts.
        await record_invoice_for_order(
            db, order=order, settled_at=now, status="failed"
        )

    event.processed = True
    event.processed_at = now
    order.updated_at = now

    await db.commit()
    return entitlements_updated
