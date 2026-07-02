"""Topup credit-pack purchase lane (CM-01).

docs/plans/2026-07-02-commercialization-sprint-plan.md §2 CM-01.

Design invariants:
- ``PaymentOrder.order_kind == "topup"`` is the discriminator (alembic 044).
  For topup orders ``target_plan_code`` holds the SKU code (``topup_*`` prefix,
  enforced disjoint from plan codes by pricing_schema). Plan-lane queries in
  billing.py filter ``order_kind == "plan"`` so a paid topup order can never
  be promoted into ``user.plan_code`` by the refund-fallback path.
- Order-create snapshots the package credits into ``metadata_json`` so a later
  admin edit of the package cannot change what a paid order grants (mirrors
  the ``paypal_expected_usd_cents`` snapshot, plan 2026-06-26 §17 B2).
- Providers v1: wechatpay (amount-based) + paypal (independent USD list price;
  a package without ``price_usd_cents`` is fail-closed hidden from the PayPal
  rail — never FX-derived). Paddle needs per-SKU dashboard price_ids and is
  deliberately NOT offered in v1. Providers never auto-fallback (CLAUDE.md).
- Settlement (``settle_topup_paid``, called from billing._process_payment_event
  inside the same row-locked transaction) grants a ``topup`` credits bucket
  tied to ``related_order_id``; refunds are recovered by the existing
  unconditional ``revoke_buckets_for_order`` call in
  billing._recall_entitlements_for_refund.
- Default inert: ``TopupConfig.enabled`` stays False; production flip is the
  owner's call via pricing runtime.

This module must NOT import billing (billing imports us for the settle hook).
"""

from __future__ import annotations

import functools
import logging
from datetime import UTC, datetime, timedelta, timezone

import anyio
from auth import get_current_user
from csrf import require_same_origin_state_change
from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Request
from models import CreditsBucket, PaymentOrder, User
from payment_provider_alipay import detect_checkout_surface
from payment_providers import get_provider, is_provider_operational
from plan_catalog import find_topup_package_any, get_topup_config, get_topup_package
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing/topup", tags=["billing"])

# Mirrors billing.ORDER_EXPIRY_MINUTES (not imported: billing imports this
# module for the settle hook, so the dependency must stay one-directional).
_ORDER_EXPIRY_MINUTES = 30

# v1 rails. Paddle charges by dashboard price_id per SKU — no topup price_ids
# exist yet, so offering it would 502 at click time (fail-closed exclusion;
# revisit when the owner publishes Paddle prices for topup SKUs). ``fake``
# stays for dev/test and is already gated by is_provider_operational.
TOPUP_ALLOWED_PROVIDERS = ("wechatpay", "paypal", "fake")

_CREDITS_SNAPSHOT_KEY = "topup_credits_snapshot"
_GRANT_FAILED_KEY = "topup_grant_failed"
_GRANTED_BUCKET_KEY = "topup_granted_bucket_id"


class CreateTopupOrderRequest(BaseModel):
    topup_code: str
    provider: str
    checkout_surface: str | None = None


def is_topup_order(order) -> bool:
    """order_kind discriminator; pre-044 rows without the column are plan orders."""
    return (getattr(order, "order_kind", None) or "plan") == "topup"


def _providers_for_package(pkg) -> list[str]:
    """Operational rails that can actually charge this SKU (fail-closed)."""
    available: list[str] = []
    for code in TOPUP_ALLOWED_PROVIDERS:
        if not is_provider_operational(code):
            continue
        if code == "paypal" and not (
            isinstance(getattr(pkg, "price_usd_cents", None), int) and pkg.price_usd_cents > 0
        ):
            continue
        available.append(code)
    return available


@router.get("/packages")
async def list_topup_packages(
    user: User | None = Depends(get_current_user),
) -> dict:
    """Purchasable topup packages + per-SKU usable rails.

    ``enabled: false`` → empty list (frontend hides the section); packages are
    pricing facts but the endpoint stays login-gated like /checkout-config.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    topup = get_topup_config()
    if topup.enabled is not True:
        return {"enabled": False, "packages": []}

    packages = [
        {
            "code": pkg.code,
            "credits": pkg.credits,
            "price_cny_fen": pkg.price_cny_fen,
            "price_usd_cents": pkg.price_usd_cents,
            "providers": _providers_for_package(pkg),
        }
        for pkg in sorted(topup.packages, key=lambda p: (p.sort_order, p.code))
        if pkg.active
    ]
    return {"enabled": True, "packages": packages}


@router.post("/orders", dependencies=[Depends(require_same_origin_state_change)])
async def create_topup_order(
    body: CreateTopupOrderRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
    request: Request = None,  # type: ignore[assignment]
) -> dict:
    """Create a payment order for a credit pack, dispatched through the
    provider adapter. Mirrors billing.create_order gate-for-gate; no plan-rank
    gate (topup is purchasable on any plan, including free — bucket_priority
    already routes topup credits to every service mode)."""
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    if get_topup_config().enabled is not True:
        raise HTTPException(status_code=403, detail="点数充值当前未开放")

    pkg = get_topup_package(body.topup_code)
    if pkg is None:
        raise HTTPException(status_code=400, detail=f"无效的点数包: {body.topup_code}")

    try:
        provider = get_provider(body.provider)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"不支持的支付渠道: {body.provider}")
    if body.provider not in TOPUP_ALLOWED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"支付渠道 {body.provider} 暂不支持点数充值")
    if not is_provider_operational(body.provider):
        raise HTTPException(status_code=501, detail=f"支付渠道 {body.provider} 尚未接入")
    if body.provider == "paypal" and not (isinstance(pkg.price_usd_cents, int) and pkg.price_usd_cents > 0):
        # Fail-closed: no USD list price → PayPal cannot charge this SKU.
        raise HTTPException(status_code=400, detail=f"点数包 {pkg.code} 未发布美元价格，暂不支持 PayPal")

    checkout_surface = detect_checkout_surface(
        body.checkout_surface,
        request.headers.get("user-agent") if request is not None else None,
    )

    now = datetime.now(UTC)
    order = PaymentOrder(
        user_id=user.id,
        provider=body.provider,
        order_kind="topup",
        target_plan_code=pkg.code,
        billing_period="one_time",
        amount_cny=pkg.price_cny_fen,
        status="created",
        expires_at=now + timedelta(minutes=_ORDER_EXPIRY_MINUTES),
        metadata_json={
            "checkout_surface": checkout_surface,
            # Settlement grants THIS amount, immune to later package edits
            # (B2-style create-time snapshot).
            _CREDITS_SNAPSHOT_KEY: pkg.credits,
        },
    )
    db.add(order)
    await db.flush()

    account_email = (getattr(user, "email", None) or "").strip() or None
    try:
        checkout = await anyio.to_thread.run_sync(
            functools.partial(
                provider.create_checkout,
                order_id=str(order.id),
                amount_cny=pkg.price_cny_fen,
                target_plan_code=pkg.code,
                billing_period="one_time",
                checkout_surface=checkout_surface,
                customer_email=account_email,
            )
        )
    except Exception as exc:
        await db.rollback()
        logger.error("Provider %s topup checkout failed for order %s: %s", body.provider, order.id, exc)
        raise HTTPException(status_code=502, detail=f"支付渠道创建 checkout 失败: {exc}")

    order.checkout_url = checkout.checkout_url
    if checkout.provider_order_id:
        order.provider_order_id = checkout.provider_order_id
    expected_usd_cents = getattr(checkout, "expected_usd_cents", None)
    if expected_usd_cents is not None:
        metadata = dict(getattr(order, "metadata_json", None) or {})
        metadata["paypal_expected_usd_cents"] = int(expected_usd_cents)
        order.metadata_json = metadata
    order.status = "pending"
    await db.commit()

    logger.info(
        "Topup order %s created via %s for user %s: %s (%d credits) ¥%.2f",
        order.id,
        body.provider,
        user.id,
        pkg.code,
        pkg.credits,
        pkg.price_cny_fen / 100,
    )

    return {
        "order_id": str(order.id),
        "status": order.status,
        "order_kind": "topup",
        "topup_code": pkg.code,
        "credits": pkg.credits,
        "amount_cny": order.amount_cny,
        "provider": order.provider,
        "checkout_surface": checkout_surface,
        "checkout_url": checkout.checkout_url,
        "display_mode": getattr(checkout, "display_mode", "redirect"),
        "qr_code_url": getattr(checkout, "qr_code_url", None),
        "expires_at": order.expires_at.isoformat() if order.expires_at else None,
    }


async def settle_topup_paid(db: AsyncSession, *, order: PaymentOrder) -> bool:
    """Grant the topup credits bucket for a paid topup order.

    Called from billing._process_payment_event inside the row-locked settle
    transaction (caller commits). Idempotency layers: webhook event dedup +
    order terminal-state guard upstream, plus a related_order_id bucket probe
    here (belt-and-braces for replays that slip past a mocked upstream).

    Grant amount resolution: metadata credits snapshot (normal) → package
    lookup ignoring enabled/active (order predates a config edit) → give up
    with a CRITICAL log + ``topup_grant_failed`` metadata marker. Never guess
    an amount; never raise — order/invoice truth must still commit, the marker
    keeps the failure visible for manual re-drive.

    Returns True when credits were granted (entitlements_updated semantics).
    """
    existing = (
        (
            await db.execute(
                select(CreditsBucket).where(
                    CreditsBucket.related_order_id == order.id,
                    CreditsBucket.bucket_type == "topup",
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        logger.info("Topup order %s already granted bucket %s, skipping", order.id, existing.id)
        return False

    code = order.target_plan_code
    metadata = dict(getattr(order, "metadata_json", None) or {})
    credits = metadata.get(_CREDITS_SNAPSHOT_KEY)
    if not isinstance(credits, int) or credits <= 0:
        pkg = find_topup_package_any(code)
        credits = getattr(pkg, "credits", None)
        if not isinstance(credits, int) or credits <= 0:
            logger.critical(
                "TOPUP_GRANT_FAILED order=%s user=%s code=%s: no credits "
                "snapshot and package missing from config — paid order left "
                "ungranted, manual re-drive required",
                order.id,
                order.user_id,
                code,
            )
            metadata[_GRANT_FAILED_KEY] = "no_credit_amount"
            order.metadata_json = metadata
            return False
        logger.warning(
            "Topup order %s missing credits snapshot; granting current config amount %d for %s",
            order.id,
            credits,
            code,
        )

    from credits_service import shadow_grant

    bucket = await shadow_grant(
        db,
        user_id=order.user_id,
        bucket_type="topup",
        amount=credits,
        source_label=code,
        related_order_id=order.id,
        reason_code="topup_purchase",
    )
    if bucket is None:
        logger.critical(
            "TOPUP_GRANT_FAILED order=%s user=%s code=%s credits=%d: "
            "shadow_grant failed — paid order left ungranted, manual "
            "re-drive required",
            order.id,
            order.user_id,
            code,
            credits,
        )
        metadata[_GRANT_FAILED_KEY] = "grant_failed"
        order.metadata_json = metadata
        return False

    metadata.pop(_GRANT_FAILED_KEY, None)
    metadata[_GRANTED_BUCKET_KEY] = str(bucket.id)
    order.metadata_json = metadata
    logger.info("Topup order %s settled: %d credits → bucket %s (user %s)", order.id, credits, bucket.id, order.user_id)
    return True
