"""Admin 支付对账可见面 — audit 2026-06-12 P1。

「已付款但未结算」此前只有 WARNING 日志一条出口（且 docker logs 随
recreate 蒸发）。本路由给 admin 一个只读对账面 + 手动触发入口：

- ``GET  /api/admin/billing/unsettled``  滞留订单 + 可疑 webhook 事件
- ``POST /api/admin/billing/reconcile``  立即跑一轮对账（复用 sweeper 的
  ``reconcile_once``，结算仍走 billing 单一入口）

不做任何写操作以外的状态流转；退款/手动结算等动作不在此路由（见 plan
2026-05-22 §2 非目标）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin_auth import _require_admin
from auth import get_current_user
from csrf import require_same_origin_state_change
from database import get_db
from models import PaymentOrder, PaymentWebhookEvent, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/billing", tags=["admin-billing"])

# 终态守卫的 replay 拒绝（"Order already in terminal state: ..."）是 webhook
# 重投递的正常痕迹，不算可疑；过滤掉避免淹没真正的验签失败/孤儿事件。
_BENIGN_ERROR_PREFIX = "Order already in terminal state"


def _serialize_order(order: PaymentOrder) -> dict:
    return {
        "order_id": str(order.id),
        "user_id": str(order.user_id),
        "provider": order.provider,
        "provider_order_id": order.provider_order_id,
        "status": order.status,
        "amount_cny": order.amount_cny,
        "target_plan_code": order.target_plan_code,
        "billing_period": order.billing_period,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
    }


def _serialize_event(event: PaymentWebhookEvent) -> dict:
    return {
        "event_id": str(event.id),
        "provider": event.provider,
        "provider_event_id": event.provider_event_id,
        "event_type": event.event_type,
        "signature_valid": event.signature_valid,
        "processed": event.processed,
        "error_message": event.error_message,
        "received_at": event.received_at.isoformat() if event.received_at else None,
    }


@router.get("/unsettled")
async def list_unsettled(
    limit: int = Query(default=100, ge=1, le=500),
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """滞留订单（created/pending）与可疑 webhook 事件（验签失败 / 带错误）。"""
    _require_admin(user)

    orders_result = await db.execute(
        select(PaymentOrder)
        .where(PaymentOrder.status.in_(("created", "pending")))
        .order_by(PaymentOrder.created_at.desc())
        .limit(limit)
    )
    pending_orders = [_serialize_order(o) for o in orders_result.scalars().all()]

    events_result = await db.execute(
        select(PaymentWebhookEvent)
        .where(
            or_(
                PaymentWebhookEvent.signature_valid.is_(False),
                and_(
                    PaymentWebhookEvent.error_message.is_not(None),
                    not_(
                        PaymentWebhookEvent.error_message.like(
                            f"{_BENIGN_ERROR_PREFIX}%"
                        )
                    ),
                ),
            )
        )
        .order_by(PaymentWebhookEvent.received_at.desc())
        .limit(limit)
    )
    suspect_events = [_serialize_event(e) for e in events_result.scalars().all()]

    # R7：最近退款单。部分退款不会自动回收权益（_process_payment_event 只
    # 告警），这里是人工复核的入口。
    refunds_result = await db.execute(
        select(PaymentOrder)
        .where(PaymentOrder.status.in_(("refunded", "partial_refunded")))
        .order_by(PaymentOrder.updated_at.desc())
        .limit(20)
    )
    recent_refunds = [_serialize_order(o) for o in refunds_result.scalars().all()]

    return {
        "pending_orders": pending_orders,
        "suspect_webhook_events": suspect_events,
        "recent_refunds": recent_refunds,
        "counts": {
            "pending_orders": len(pending_orders),
            "suspect_webhook_events": len(suspect_events),
            "recent_refunds": len(recent_refunds),
        },
    }


@router.post("/reconcile", dependencies=[Depends(require_same_origin_state_change)])
async def trigger_reconcile(
    user: User | None = Depends(get_current_user),
) -> dict:
    """手动触发一轮对账。结算路径与 sweeper 完全一致（billing 单一入口）。"""
    _require_admin(user)

    from billing_reconciliation import reconcile_once

    stats = await reconcile_once()
    logger.info(
        "admin manual reconcile by %s: scanned=%d settled=%d errors=%d",
        user.id if user else "?",
        stats["scanned"],
        stats["settled"],
        stats["errors"],
    )
    return {"ok": True, "stats": stats}


__all__ = ["router"]
