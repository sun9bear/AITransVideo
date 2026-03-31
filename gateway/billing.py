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
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import AdminAuditLog, PaymentOrder, PaymentWebhookEvent, User
from payment_providers import get_provider, is_provider_operational, list_providers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["billing"])

VALID_TARGET_PLANS = {"plus", "pro"}
VALID_BILLING_PERIODS = {"monthly", "quarterly", "annual"}

# Price table (in fen / 分)
PLAN_PRICES_CNY = {
    ("plus", "monthly"): 6900,     # ¥69/月
    ("plus", "quarterly"): 17900,  # ¥179/季
    ("plus", "annual"): 59900,     # ¥599/年
    ("pro", "monthly"): 29900,     # ¥299/月
    ("pro", "quarterly"): 79900,   # ¥799/季
    ("pro", "annual"): 259900,     # ¥2599/年
}

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

    if body.target_plan_code not in VALID_TARGET_PLANS:
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

    price_key = (body.target_plan_code, body.billing_period)
    amount = PLAN_PRICES_CNY.get(price_key, 0)
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


# --- Fake pay endpoint ---

@router.post("/fake-pay/{order_id}")
async def fake_pay(
    order_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Simulate a successful payment via fake provider adapter."""
    result = await db.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status not in ("created", "pending"):
        raise HTTPException(status_code=409, detail=f"订单状态({order.status})不可支付")

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

    return {"ok": True, "settled": settled, "order_id": str(order.id)}


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

    # Only process if order is not already in terminal state
    if order.status in ("paid", "refunded", "cancelled"):
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
        # Upgrade user plan
        user_result = await db.execute(select(User).where(User.id == order.user_id))
        user = user_result.scalar_one_or_none()
        if user is not None:
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
                logger.info("User %s upgraded %s → %s via payment order %s",
                            user.id, old_plan, order.target_plan_code, order_id)

    event.processed = True
    event.processed_at = now
    order.updated_at = now

    await db.commit()
    return entitlements_updated
