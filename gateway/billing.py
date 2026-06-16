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

import functools
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import anyio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from csrf import require_same_origin_state_change
from database import get_db
from payment_provider_alipay import (
    detect_checkout_surface,
    validate_alipay_notify_payload,
    validate_alipay_query_payload,
)
from models import (
    AdminAuditLog,
    BillingInvoice,
    PaymentOrder,
    PaymentWebhookEvent,
    Subscription,
    User,
)
from payment_providers import (
    get_provider,
    is_fake_payment_enabled,
    is_provider_operational,
    list_providers,
)
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

# Provider webhooks are unauthenticated endpoints: cap the body BEFORE any
# parse/base64/AES work (security review 2026-06-10, HIGH). Real provider
# notifications are a few KB; 64 KiB leaves 10x headroom.
WEBHOOK_MAX_BODY_BYTES = 64 * 1024

# Frontend polls GET /orders/{id}?refresh=true every few seconds (QR dialog 3s,
# banner 15s). Throttle the upstream provider query per order so polling can't
# amplify into the provider's rate limits (security review 2026-06-10, HIGH).
# Skipped refreshes still return the current DB state — webhooks remain the
# settlement truth, so confirmation is delayed by at most the interval.
_REFRESH_MIN_INTERVAL_S = 5.0
_refresh_last_at: dict[str, float] = {}


def _refresh_allowed(order_id: str) -> bool:
    now = time.monotonic()
    last = _refresh_last_at.get(order_id)
    if last is not None and (now - last) < _REFRESH_MIN_INTERVAL_S:
        return False
    if len(_refresh_last_at) > 2048:
        cutoff = now - 3600.0
        for key, stamp in list(_refresh_last_at.items()):
            if stamp < cutoff:
                _refresh_last_at.pop(key, None)
    _refresh_last_at[order_id] = now
    return True


# --- Request/Response models ---

class CreateOrderRequest(BaseModel):
    target_plan_code: str
    billing_period: str = "monthly"
    provider: str = "fake"
    checkout_surface: str | None = None


# --- Order creation (provider-dispatched) ---

@router.post("/orders", dependencies=[Depends(require_same_origin_state_change)])
async def create_order(
    body: CreateOrderRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
    request: Request = None,  # type: ignore[assignment]
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

    checkout_surface = detect_checkout_surface(
        body.checkout_surface,
        request.headers.get("user-agent") if request is not None else None,
    )

    now = datetime.now(timezone.utc)
    order = PaymentOrder(
        user_id=user.id,
        provider=body.provider,
        target_plan_code=body.target_plan_code,
        billing_period=body.billing_period,
        amount_cny=amount,
        status="created",
        expires_at=now + timedelta(minutes=ORDER_EXPIRY_MINUTES),
        metadata_json={"checkout_surface": checkout_surface},
    )
    db.add(order)
    # Flush to get order.id without committing — if adapter fails, we rollback
    await db.flush()

    # Create checkout through provider adapter. customer_email prefills the
    # buyer email on hosted checkouts (Paddle); phone-only accounts have no
    # email (User.email is nullable) and pass None — providers must treat it
    # as a UX nicety, never a checkout gate.
    # Providers are sync (blocking httpx) — run off the event loop so a slow
    # provider API can't stall the whole gateway (auth, webhooks, other users).
    account_email = (getattr(user, "email", None) or "").strip() or None
    try:
        checkout = await anyio.to_thread.run_sync(
            functools.partial(
                provider.create_checkout,
                order_id=str(order.id),
                amount_cny=amount,
                target_plan_code=body.target_plan_code,
                billing_period=body.billing_period,
                checkout_surface=checkout_surface,
                customer_email=account_email,
            )
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
        "checkout_surface": checkout_surface,
        "checkout_url": checkout.checkout_url,
        # "qrcode" => frontend renders qr_code_url in-page (WeChat Native);
        # "redirect" (default) => frontend navigates to checkout_url.
        "display_mode": getattr(checkout, "display_mode", "redirect"),
        "qr_code_url": getattr(checkout, "qr_code_url", None),
        "expires_at": order.expires_at.isoformat() if order.expires_at else None,
    }


# --- Order query ---

@router.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
    refresh: bool = False,
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

    # §7.5/R2: any provider with a query_order impl can refresh on demand (was
    # alipay-only). Stub providers raise NotImplementedError, which
    # _refresh_order_from_provider swallows.
    if (
        refresh
        and order.status in ("created", "pending")
        and _refresh_allowed(str(order.id))
    ):
        await _refresh_order_from_provider(db=db, order=order)

    return {
        "order_id": str(order.id),
        "status": order.status,
        "amount_cny": order.amount_cny,
        "target_plan_code": order.target_plan_code,
        "billing_period": order.billing_period,
        "provider": order.provider,
        "provider_order_id": order.provider_order_id,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


async def _refresh_order_from_provider(
    *,
    db: AsyncSession,
    order: PaymentOrder,
) -> None:
    try:
        provider = get_provider(order.provider)
    except KeyError:
        return

    try:
        query_result = await provider.query_order(
            order_id=str(order.id),
            provider_order_id=order.provider_order_id,
        )
    except NotImplementedError:
        return
    except Exception as exc:
        logger.warning("Provider %s order query failed for %s: %s", order.provider, order.id, exc)
        return

    if query_result is None:
        return

    if order.provider == "alipay":
        try:
            validate_alipay_query_payload(
                _load_live_alipay_config(),
                query_result.raw_payload,
                order_id=str(order.id),
                amount_cny=order.amount_cny,
            )
        except ValueError as exc:
            logger.warning("Ignoring alipay query result for %s: %s", order.id, exc)
            return

    if order.provider == "paddle":
        from payment_provider_paddle import PaddleConfig, validate_paddle_webhook_payload

        try:
            validate_paddle_webhook_payload(
                PaddleConfig.from_env(),
                query_result.raw_payload,
                order_id=str(order.id),
                target_plan_code=order.target_plan_code,
                billing_period=order.billing_period,
                provider_order_id=order.provider_order_id,
            )
        except ValueError as exc:
            logger.warning("Ignoring paddle query result for %s: %s", order.id, exc)
            return

    if order.provider == "wechatpay":
        from payment_provider_wechat import (
            WechatPayConfig,
            map_wechat_trade_state,
            validate_wechat_webhook_payload,
        )

        # NOTPAY/USERPAYING query responses may omit attach/amount — only gate
        # the transitions that can settle. Pending never settles downstream.
        if map_wechat_trade_state(query_result.provider_status) != "pending":
            try:
                # The query response IS the transaction object (same shape as
                # the decrypted webhook resource): same fact gates apply.
                validate_wechat_webhook_payload(
                    WechatPayConfig.from_env(),
                    query_result.raw_payload,
                    order_id=str(order.id),
                    amount_cny=order.amount_cny,
                    provider_order_id=order.provider_order_id,
                )
            except ValueError as exc:
                logger.warning("Ignoring wechat query result for %s: %s", order.id, exc)
                return

    if query_result.provider_order_id and order.provider_order_id != query_result.provider_order_id:
        order.provider_order_id = query_result.provider_order_id

    new_status = provider.map_status(query_result.provider_status)
    if new_status == "pending":
        order.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return

    await _process_payment_event(
        db=db,
        provider=order.provider,
        provider_event_id=query_result.provider_event_id,
        event_type=f"payment.query.{new_status}",
        order_id=str(order.id),
        new_status=new_status,
        signature_valid=True,
        raw_payload=query_result.raw_payload,
    )


def _load_live_alipay_config():
    from payment_provider_alipay import AlipayConfig

    return AlipayConfig.from_env()


# --- Checkout config (Task 5) ---


_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "fake": "测试支付",
    "alipay": "支付宝",
    "wechatpay": "微信支付",
    "stripe": "Stripe",
    "paddle": "信用卡 / 微信 (Paddle)",
}


def _display_name(provider_code: str) -> str:
    return _PROVIDER_DISPLAY_NAMES.get(provider_code, provider_code)


@router.get("/checkout-config")
async def get_checkout_config(
    user: User | None = Depends(get_current_user),
    request: Request = None,  # type: ignore[assignment]
) -> dict:
    """Return the list of checkout providers currently usable by this gateway.

    Gateway owns provider availability (Task 5 §"Gateway owns provider
    availability"). The frontend must read this endpoint rather than deciding
    operational state from env vars or hardcoded client-side logic.

    Availability rule used here:
    - every known provider is listed with its `operational` flag
    - `default_provider` is the first operational provider in preference order
      [alipay, wechatpay, stripe, fake]
    - fake is operational in dev/test; production must explicitly opt in with
      AVT_ENABLE_FAKE_PAYMENT=true
    - if nothing is operational, `fake` is returned as a compatibility fallback
      but remains non-operational

    Pricing facts are NOT returned here. Prices continue to come from
    `/api/plans`. This endpoint is strictly about "can we currently charge a
    card via X" — nothing more.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    preference = ["alipay", "wechatpay", "paddle", "stripe", "fake"]
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

    # Surface-aware recommendation (plan 2026-06-08 §4 three-rail routing):
    # - desktop + domestic: own WeChat Native QR (~0.6% fee) when operational
    # - mobile web: Paddle — WeChat Native QR needs a SECOND device on mobile
    #   (the weixin:// QR cannot be long-press-recognized inside WeChat), and
    #   WeChat-via-Paddle is desktop-only, so Paddle (card / future Alipay)
    #   is the usable mobile rail.
    # default_provider keeps its historical "first operational" semantics for
    # backward compatibility; the frontend should prefer recommended_provider.
    operational_codes = {p["code"] for p in providers_payload if p["operational"]}
    surface = detect_checkout_surface(
        None,
        request.headers.get("user-agent") if request is not None else None,
    )
    if surface == "mobile_web":
        surface_preference = ["paddle", "wechatpay"]
    else:
        surface_preference = ["wechatpay", "paddle"]
    recommended_provider = next(
        (code for code in surface_preference if code in operational_codes),
        default_provider,
    )

    return {
        "default_provider": default_provider,
        "recommended_provider": recommended_provider,
        "checkout_surface": surface,
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
    if not is_fake_payment_enabled():
        return {
            "ok": False,
            "settled": False,
            "order_id": order_id,
            "not_found": False,
            "already_settled": False,
            "fake_payment_disabled": True,
        }

    result = await db.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        return {
            "ok": False,
            "settled": False,
            "order_id": order_id,
            "not_found": True,
            "already_settled": False,
            "fake_payment_disabled": False,
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
            "fake_payment_disabled": False,
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
        "fake_payment_disabled": False,
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
    if result.get("fake_payment_disabled"):
        raise HTTPException(status_code=403, detail="fake payment provider is disabled")
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
    if result.get("fake_payment_disabled"):
        return RedirectResponse(
            url="/settings/billing?status=error&reason=fake_payment_disabled",
            status_code=303,
        )
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

@router.post("/webhooks/{provider_name}", response_model=None)
async def receive_webhook(
    provider_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict | PlainTextResponse:
    """Receive and process a payment webhook through provider adapter.

    Flow:
    1. Resolve provider adapter
    2. Verify signature via adapter.verify_signature()
    3. Parse payload via adapter.parse_webhook()
    4. Pass to _process_payment_event with verified signature_valid
    """
    # Reject oversized bodies before reading where possible (Content-Length),
    # and unconditionally after reading (chunked encoding has no length header).
    # This is an unauthenticated endpoint; the wechatpay path additionally
    # base64-decodes and AES-decrypts the body, so each extra MB allocates
    # several times over.
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > WEBHOOK_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="webhook body too large")
    raw_body = await request.body()
    if len(raw_body) > WEBHOOK_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="webhook body too large")
    headers = dict(request.headers)

    # Resolve provider
    try:
        provider = get_provider(provider_name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider_name}")

    try:
        signature_valid = provider.verify_signature(raw_body, headers)
    except NotImplementedError:
        signature_valid = False
    except Exception as exc:
        logger.warning("Signature verification error for %s: %s", provider_name, exc)
        signature_valid = False

    try:
        event = provider.parse_webhook(raw_body)
    except NotImplementedError:
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
            signature_valid=False,
            raw_payload=payload,
        )
        return _provider_webhook_response(provider_name, settled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}")

    if not event.provider_event_id:
        raise HTTPException(status_code=400, detail="missing provider_event_id")

    # R7 退款绑定：退款语义事件不携带我们的 order_id（paddle adjustment 无
    # custom_data、微信 refund resource 无 attach），用 provider 侧主键反查
    # provider_order_id。只为 refunded 事件做绑定——paid 语义事件缺 order_id
    # 仍走 order-not-found no-op（见 _validate_*_event_against_order 的
    # INVARIANT 注记），不得借道结算。
    resolved_order_id = event.order_id
    if event.new_status == "refunded" and not resolved_order_id:
        resolved_order_id = await _resolve_refund_order_id(
            db, provider_name=provider_name, raw_payload=event.raw_payload
        )

    if provider_name == "alipay":
        signature_valid = signature_valid and await _validate_alipay_event_against_order(
            db=db,
            order_id=resolved_order_id,
            payload=event.raw_payload,
            is_query_result=False,
        )

    if provider_name == "paddle":
        signature_valid = signature_valid and await _validate_paddle_event_against_order(
            db=db,
            order_id=resolved_order_id,
            payload=event.raw_payload,
        )

    if provider_name == "wechatpay":
        signature_valid = signature_valid and await _validate_wechat_event_against_order(
            db=db,
            order_id=resolved_order_id,
            payload=event.raw_payload,
        )

    settled = await _process_payment_event(
        db=db,
        provider=provider_name,
        provider_event_id=event.provider_event_id,
        event_type=event.event_type,
        order_id=resolved_order_id,
        new_status=event.new_status,
        signature_valid=signature_valid,
        raw_payload=event.raw_payload,
        refund_amount_fen=(
            _extract_refund_amount_fen(provider_name, event.raw_payload)
            if event.new_status == "refunded"
            else None
        ),
    )

    return _provider_webhook_response(provider_name, settled)


async def _resolve_refund_order_id(
    db: AsyncSession,
    *,
    provider_name: str,
    raw_payload: dict | None,
) -> str:
    """退款事件 → 我方订单 ID 的反查绑定（R7）。

    - paddle:    adjustment.data.transaction_id == PaymentOrder.provider_order_id
    - wechatpay: refund.out_trade_no            == PaymentOrder.provider_order_id
    查不到返回 ""——下游 _process_payment_event 记录事件后 no-op（admin
    unsettled 面板的 suspect_webhook_events 可见），不会误结算。
    """
    payload = raw_payload or {}
    token = ""
    if provider_name == "paddle":
        token = str(((payload.get("data") or {}).get("transaction_id")) or "").strip()
    elif provider_name == "wechatpay":
        token = str(((payload.get("transaction") or {}).get("out_trade_no")) or "").strip()
    if not token:
        return ""
    result = await db.execute(
        select(PaymentOrder).where(
            PaymentOrder.provider == provider_name,
            PaymentOrder.provider_order_id == token,
        )
    )
    order = result.scalar_one_or_none()
    return str(order.id) if order is not None else ""


def _extract_refund_amount_fen(provider_name: str, raw_payload: dict | None) -> int | None:
    """从退款事件里提取退款金额（分）。取不到返回 None（按全额处理）。"""
    payload = raw_payload or {}

    def _int_or_none(value: object) -> int | None:
        return int(str(value)) if value not in (None, "") else None

    try:
        if provider_name == "wechatpay":
            amount = ((payload.get("transaction") or {}).get("amount")) or {}
            refund = amount.get("refund")
            return _int_or_none(refund)
        if provider_name == "paddle":
            data = (payload.get("data") or {})
            totals = data.get("totals") or {}
            subtotal = _int_or_none(totals.get("subtotal"))
            if subtotal is not None:
                return subtotal

            item_subtotals: list[int] = []
            for item in data.get("items") or []:
                item_totals = (item or {}).get("totals") or {}
                item_subtotal = _int_or_none(item_totals.get("subtotal"))
                if item_subtotal is not None:
                    item_subtotals.append(item_subtotal)
            if item_subtotals:
                return sum(item_subtotals)
            return None
    except (TypeError, ValueError):
        return None
    return None


def _provider_webhook_response(
    provider_name: str,
    settled: bool,
) -> dict | PlainTextResponse:
    if provider_name == "alipay":
        return PlainTextResponse("success")
    if provider_name == "wechatpay":
        # WeChat Pay v3 treats HTTP 200 as the ACK; the spec response body is
        # {"code": "SUCCESS"}. Anything else triggers redelivery storms.
        return {"code": "SUCCESS"}
    return {"ok": True, "settled": settled}


async def _validate_alipay_event_against_order(
    *,
    db: AsyncSession,
    order_id: str,
    payload: dict | None,
    is_query_result: bool,
) -> bool:
    result = await db.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        return True

    try:
        if is_query_result:
            validate_alipay_query_payload(
                _load_live_alipay_config(),
                payload,
                order_id=str(order.id),
                amount_cny=order.amount_cny,
            )
        else:
            validate_alipay_notify_payload(
                _load_live_alipay_config(),
                payload,
                order_id=str(order.id),
                amount_cny=order.amount_cny,
            )
        return True
    except ValueError as exc:
        logger.warning("Alipay payload validation failed for %s: %s", order_id, exc)
        return False


async def _validate_paddle_event_against_order(
    *,
    db: AsyncSession,
    order_id: str,
    payload: dict | None,
) -> bool:
    result = await db.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        # Order-less event (e.g. adjustment.created carries no
        # custom_data.order_id). _process_payment_event records it and no-ops —
        # a safe 200-ACK. Refund credit clawback via transaction_id is a P2 task
        # (R7); P1 must not error here and trigger Paddle retry storms.
        #
        # INVARIANT (review F-E): this `return True` is safe ONLY because a
        # missing order cannot settle downstream. When R7 wires refund clawback
        # resolved by transaction_id under this path, a signed "paid"-type event
        # with an unknown order_id must NOT bypass binding — return False for
        # settlement events before adding clawback resolution here.
        return True

    data = (payload or {}).get("data") or {}

    if str((payload or {}).get("event_type") or "").strip() in (
        "adjustment.created", "adjustment.updated",
    ):
        # R7 退款 fact-gate：adjustment 不是 transaction 对象，不能走下面的
        # transaction 校验（必失败）。created/updated 同门控（审批通过走
        # updated）。绑定即事实：adjustment.transaction_id
        # 必须等于本单 provider_order_id，且必须是已批准的退款语义。
        # 金额不做 gate：Paddle 是 MoR，adjustment totals 含税，与
        # amount_cny 没有恒等关系（见文件头 Amount-gate design note）。
        txn_ref = str(data.get("transaction_id") or "").strip()
        if not order.provider_order_id or txn_ref != str(order.provider_order_id):
            logger.warning("Paddle adjustment transaction mismatch for %s", order_id)
            return False
        action = str(data.get("action") or "").strip().lower()
        adj_status = str(data.get("status") or "").strip().lower()
        if action not in ("refund", "chargeback") or adj_status != "approved":
            logger.warning(
                "Paddle adjustment not an approved refund for %s (action=%s, status=%s)",
                order_id, action, adj_status,
            )
            return False
        return True

    from payment_provider_paddle import PaddleConfig, validate_paddle_webhook_payload

    try:
        validate_paddle_webhook_payload(
            PaddleConfig.from_env(),
            data,
            order_id=str(order.id),
            target_plan_code=order.target_plan_code,
            billing_period=order.billing_period,
            provider_order_id=order.provider_order_id,
        )
        return True
    except ValueError as exc:
        logger.warning("Paddle payload validation failed for %s: %s", order_id, exc)
        return False


async def _validate_wechat_event_against_order(
    *,
    db: AsyncSession,
    order_id: str,
    payload: dict | None,
) -> bool:
    result = await db.execute(select(PaymentOrder).where(PaymentOrder.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        # Order-less event: recorded by _process_payment_event and no-op'd —
        # safe 200-ACK so WeChat doesn't retry-storm.
        #
        # INVARIANT (mirrors the Paddle F-E note): this `return True` is safe
        # ONLY because a missing order cannot settle downstream. WeChat's
        # provider_event_id IS the transaction_id, so if refund clawback /
        # reconciliation resolved by transaction_id is ever wired under this
        # path, a signed event with an unknown order_id must NOT bypass
        # binding — return False for settle/refund-semantic events first.
        return True

    transaction = (payload or {}).get("transaction") or {}

    if str((payload or {}).get("event_type") or "").strip().upper().startswith("REFUND."):
        # R7 退款 fact-gate：refund 对象没有 trade_state/attach/payer，不能走
        # 下面的 transaction 校验。专用 gate 与支付 gate 同层（provider 模块），
        # 含 mchid 硬门 / out_trade_no 绑定 / SUCCESS / 原单总额 fail-closed。
        from payment_provider_wechat import (
            WechatPayConfig,
            validate_wechat_refund_payload,
        )

        try:
            validate_wechat_refund_payload(
                WechatPayConfig.from_env(),
                transaction,
                amount_cny=order.amount_cny,
                provider_order_id=order.provider_order_id,
            )
            return True
        except ValueError as exc:
            logger.warning(
                "WeChat refund payload validation failed for %s: %s", order_id, exc
            )
            return False

    from payment_provider_wechat import (
        WechatPayConfig,
        validate_wechat_webhook_payload,
    )

    try:
        validate_wechat_webhook_payload(
            WechatPayConfig.from_env(),
            transaction,
            order_id=str(order.id),
            amount_cny=order.amount_cny,
            provider_order_id=order.provider_order_id,
        )
        return True
    except ValueError as exc:
        logger.warning("WeChat payload validation failed for %s: %s", order_id, exc)
        return False


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
    refund_amount_fen: int | None = None,
) -> bool:
    """Process a payment event idempotently. Returns True if entitlements were updated.

    This function is provider-agnostic. All provider-specific logic (signature
    verification, payload parsing, status mapping) happens in the caller.

    Args:
        signature_valid: Whether the webhook signature was verified by the provider adapter.
            Unverified events are recorded but never settle orders or upgrade plans.
    """
    # --- Idempotency check + record (atomic) ---
    # P1-11b (audit 2026-05-07, D-HIGH-7): atomic INSERT ON CONFLICT replaces
    # SELECT-then-INSERT race window. Two concurrent provider deliveries used
    # to both pass the SELECT, both add a row, and rely on the unique
    # constraint catching the duplicate via IntegrityError on commit. With
    # ON CONFLICT DO NOTHING RETURNING we get a single atomic statement that
    # either inserts (returns the new id) or no-ops (returns None). The unique
    # index on `provider_event_id` is the conflict target.
    now = datetime.now(timezone.utc)
    insert_stmt = (
        pg_insert(PaymentWebhookEvent)
        .values(
            provider=provider,
            provider_event_id=provider_event_id,
            event_type=event_type,
            signature_valid=signature_valid,
            processed=False,
            payload=raw_payload,
            received_at=now,
        )
        # P1-11a (audit 2026-05-07, D-CRITICAL-4 + alembic 017):
        # composite dedup key — provider event IDs are not globally
        # unique across providers (Stripe / Alipay / WeChat Pay can
        # each emit "evt_ABC123" independently). Match the new composite
        # UNIQUE on the table.
        .on_conflict_do_nothing(
            index_elements=["provider", "provider_event_id"]
        )
        .returning(PaymentWebhookEvent.id)
    )
    insert_result = await db.execute(insert_stmt)
    inserted_id = insert_result.scalar_one_or_none()
    if inserted_id is None:
        logger.info("Duplicate webhook event %s, skipping", provider_event_id)
        return False

    # Re-fetch as a managed ORM instance so subsequent code can mutate
    # `event.processed`, `event.error_message`, `event.processed_at`, etc.,
    # and have those changes flushed in the existing 4-commit flow below.
    event = (await db.execute(
        select(PaymentWebhookEvent).where(PaymentWebhookEvent.id == inserted_id)
    )).scalar_one()

    # Find the order. FOR UPDATE 行锁（Codex review 2026-06-13 P1）：webhook 与
    # 对账 sweeper / 用户轮询 refresh 是三个并发入口，query 路径合成的
    # provider_event_id 与 webhook 不同，事件级幂等键挡不住跨入口并发——
    # 必须在订单行上串行化，后到者拿锁后重读到终态被守卫挡掉，防止
    # 双结算/重复发订阅点数。sqlite（测试）方言无 FOR UPDATE，自动忽略。
    result = await db.execute(
        select(PaymentOrder).where(PaymentOrder.id == order_id).with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        event.processed = True
        event.error_message = f"Order {order_id} not found"
        event.processed_at = now
        await db.commit()
        logger.warning("Webhook for unknown order %s", order_id)
        return False

    if provider == "alipay" and raw_payload:
        trade_no = str(raw_payload.get("trade_no", "")).strip()
        if trade_no and order.provider_order_id != trade_no:
            order.provider_order_id = trade_no

    # Terminal-state guard.
    #
    # Most combinations of (current order status, new status) are replays and
    # must short-circuit to preserve webhook idempotency. The single exception
    # is the paid → refunded transition, which a real provider refund callback
    # depends on to keep `billing_invoices.status` truthful. Without allowing
    # that transition here, a later refund webhook would be silently rejected
    # and billing history would lie.
    _is_refund_transition = (
        order.status in ("paid", "partial_refunded") and new_status == "refunded"
    )
    if order.status in ("paid", "partial_refunded", "refunded", "cancelled") and not _is_refund_transition:
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

    is_known_partial_refund = (
        new_status == "refunded"
        and refund_amount_fen is not None
        and refund_amount_fen < order.amount_cny
    )
    settlement_status = "partial_refunded" if is_known_partial_refund else new_status

    # Update order status
    order.status = settlement_status
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
        # Refund truth layer: billing history made truthful (T4 既有行为)。
        await record_invoice_for_order(
            db, order=order, settled_at=now, status=settlement_status
        )
        # R7 退款闭环：回收订阅 / plan 投影 / 本单关联 credits。
        # 部分退款（已知退款金额 < 订单金额）不自动回收，留人工复核——
        # admin /api/admin/billing/unsettled 的 recent_refunds 面板可见；
        # 金额未知（query 路径 / 字段缺失）按全额处理。
        if is_known_partial_refund:
            logger.warning(
                "Partial refund for order %s (refund=%s < order=%s); "
                "entitlements kept — manual review required",
                order_id, refund_amount_fen, order.amount_cny,
            )
        else:
            await _recall_entitlements_for_refund(db, order=order, now=now)

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


async def _recall_entitlements_for_refund(
    db: AsyncSession,
    *,
    order: PaymentOrder,
    now: datetime,
) -> None:
    """退款结算后的权益回收（R7）。与退款结算同事务，由调用方 commit。

    - active 订阅若由本单计划支撑（plan_code 匹配）→ 标 cancelled
    - ``user.plan_code`` 投影若等于本单计划 → 回落 free（写审计日志）；
      用户退款后又买了别的计划时两者都不动，不误伤新计划
    - 本单关联 credits bucket 余额清零（shadow 语义，失败只告警不阻断
      退款结算本身——发票/订单真值优先落库）
    """
    user = (
        await db.execute(select(User).where(User.id == order.user_id))
    ).scalar_one_or_none()
    if user is None:
        logger.warning("Refund recall: user %s not found for order %s",
                       order.user_id, order.id)
        return

    subscription = (
        await db.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.status == "active",
            )
        )
    ).scalar_one_or_none()
    if subscription is not None and subscription.plan_code == order.target_plan_code:
        subscription.status = "cancelled"
        subscription.cancelled_at = now
        subscription.updated_at = now

    if (user.plan_code or "free") == order.target_plan_code:
        old_plan = user.plan_code
        user.plan_code = "free"
        db.add(AdminAuditLog(
            admin_user_id=user.id,
            target_user_id=user.id,
            action="payment_refund_downgrade",
            field_name="plan_code",
            old_value=old_plan,
            new_value="free",
        ))

    try:
        from credits_service import revoke_buckets_for_order
        await revoke_buckets_for_order(
            db, user_id=user.id, related_order_id=order.id
        )
    except Exception:
        logger.warning(
            "Refund credits revoke failed for order %s (non-fatal)",
            order.id, exc_info=True,
        )

    logger.info(
        "Refund settled for order %s: subscription_cancelled=%s plan_downgraded=%s",
        order.id,
        bool(subscription is not None and subscription.status == "cancelled"),
        user.plan_code == "free",
    )
