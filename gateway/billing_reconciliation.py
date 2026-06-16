"""支付对账 sweeper — audit 2026-06-12 P1「已付款但未结算」静默故障的服务端兜底。

webhook 是结算事实的主通道，但它被 200 ACK 终结后 provider 不再重发：
fact-gate 拒绝、验签失败、网关重启窗口丢失，结果都是用户真金已扣而订单
永远停在 created/pending，且运营零感知。本模块周期性扫滞留订单，主动调
provider 的 query_order 对账。

设计边界：

- **结算单一入口不变**：每单只调 ``billing._refresh_order_from_provider``
  —— 它内部带 provider fact-gate 校验（金额/plan/period/mchid 等），最终走
  ``_process_payment_event``（幂等、终态守卫）。本模块不自己写任何结算 /
  状态流转逻辑。
- **不跨渠道**：每单只查自己的 provider（plan 2026-05-22 §8.1 渠道隔离，
  禁止支付渠道间 fallback）。
- **fake provider 排除**：fake 仅用于开发/测试且由 fake_pay 同步结算，
  sweeper 介入会改变测试时序。
- **对账窗口**：只查 ``MIN_AGE_S``（默认 10 分钟——结账期内前端轮询的
  refresh=true 已覆盖）到 ``MAX_AGE_S``（默认 72 小时）之间的单；更老的
  滞留单不再每轮打 provider，由 ``/api/admin/billing/unsettled`` 人工可见。
- **fail-safe**：单笔订单异常不中断本轮其余订单；单轮异常不中断 loop
  （与 express_reservation_sweeper 同模式）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select

from database import async_session
from models import PaymentOrder

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_S = int(os.environ.get("AVT_BILLING_RECONCILE_INTERVAL_S", "300"))
SWEEP_BATCH_SIZE = int(os.environ.get("AVT_BILLING_RECONCILE_BATCH_SIZE", "50"))
# 错开其它 sweeper 的首扫（express=240s、r2 等），减少 startup contention。
INITIAL_DELAY_S = int(os.environ.get("AVT_BILLING_RECONCILE_INITIAL_DELAY_S", "210"))
# 单龄下限：结账进行中的订单由前端轮询（GET /orders/{id}?refresh=true）刷新，
# sweeper 不抢——也避免与 _refresh_allowed 节流的窗口互相放大 provider 查询。
MIN_AGE_S = int(os.environ.get("AVT_BILLING_RECONCILE_MIN_AGE_S", "600"))
# 单龄上限：超过此窗口的滞留单停止自动查询（provider 查询接口也有配额），
# 只留 admin 端点可见。
MAX_AGE_S = int(os.environ.get("AVT_BILLING_RECONCILE_MAX_AGE_S", str(72 * 3600)))
# 单单重试间隔（Codex review 2026-06-13 P2）：同一单两次对账之间至少隔这么久，
# 配合按 updated_at 升序轮转，防止最老的一批 pending 单饿死后续订单、
# 72 小时内反复打 provider。
RETRY_INTERVAL_S = int(os.environ.get("AVT_BILLING_RECONCILE_RETRY_INTERVAL_S", "1800"))

# 对账只针对未结算态。terminal 态（paid/refunded/cancelled/expired/failed）
# 由 _process_payment_event 的终态守卫负责，这里连选都不选。
_UNSETTLED_STATUSES = ("created", "pending")


def _candidate_orders_stmt(now: datetime):
    """滞留订单的选单语句：created/pending、非 fake、落在对账窗口内。

    轮转语义：按 ``last_reconciled_at`` 升序（最久未检查的优先；NULL 视为
    从未检查排最前），且距上次检查至少 ``RETRY_INTERVAL_S``——reconcile_once
    在每次尝试后无条件 bump ``last_reconciled_at``，保证失败/无结果的单也会让位。
    """
    newest_allowed = now - timedelta(seconds=MIN_AGE_S)
    oldest_allowed = now - timedelta(seconds=MAX_AGE_S)
    retry_cutoff = now - timedelta(seconds=RETRY_INTERVAL_S)
    return (
        select(PaymentOrder)
        .where(
            or_(
                PaymentOrder.status.in_(_UNSETTLED_STATUSES),
                and_(
                    PaymentOrder.status == "partial_refunded",
                    PaymentOrder.paid_at.is_(None),
                ),
            ),
            PaymentOrder.provider != "fake",
            PaymentOrder.created_at <= newest_allowed,
            PaymentOrder.created_at >= oldest_allowed,
            or_(
                PaymentOrder.last_reconciled_at.is_(None),
                PaymentOrder.last_reconciled_at <= retry_cutoff,
            ),
        )
        .order_by(
            PaymentOrder.last_reconciled_at.asc().nulls_first(),
            PaymentOrder.created_at.asc(),
        )
        .limit(SWEEP_BATCH_SIZE)
    )


async def reconcile_once(
    *,
    session_factory=None,
    refresh_fn=None,
    now: datetime | None = None,
) -> dict:
    """跑一轮对账，返回统计 ``{"scanned", "settled", "errors"}``。

    ``session_factory`` / ``refresh_fn`` / ``now`` 仅供测试注入；生产走默认
    ``database.async_session`` 与 ``billing._refresh_order_from_provider``。
    单笔订单的 provider 查询异常只记 errors 计数，不中断其余订单。
    """
    if refresh_fn is None:
        # 延迟 import：避免模块加载期与 billing 的依赖链耦合（billing 不
        # import 本模块，方向是安全的，但 lifespan try/except 兜底更稳）。
        from billing import _refresh_order_from_provider as refresh_fn

    factory = session_factory or async_session
    stats = {"scanned": 0, "settled": 0, "errors": 0}
    current = now or datetime.now(timezone.utc)

    async with factory() as db:
        result = await db.execute(_candidate_orders_stmt(current))
        orders = list(result.scalars().all())
        for order in orders:
            stats["scanned"] += 1
            status_before = order.status
            attempt_failed = False
            try:
                await refresh_fn(db=db, order=order)
            except Exception:
                attempt_failed = True
                stats["errors"] += 1
                logger.exception(
                    "billing_reconciliation: refresh failed for order %s (provider=%s)",
                    order.id,
                    order.provider,
                )
                await db.rollback()
            finally:
                # 无条件 bump：让本单在 RETRY_INTERVAL_S 内不再被选中——
                # refresh 内部只有 pending 结果会 commit updated_at，
                # 早退（provider 无结果 / NotImplementedError / 异常）不会，
                # 不补这里就会出现「最老一批永远霸占批次」（Codex P2）。
                attempted_at = datetime.now(timezone.utc)
                order.last_reconciled_at = attempted_at
                order.updated_at = attempted_at
            if not attempt_failed and order.status == "paid" and status_before != "paid":
                stats["settled"] += 1
                logger.warning(
                    "billing_reconciliation: order %s settled via reconcile "
                    "(provider=%s, was=%s) — webhook 通道曾丢失该单，建议排查",
                    order.id,
                    order.provider,
                    status_before,
                )
        await db.commit()
    return stats


async def _interruptible_sleep(
    delay_s: float, stop_event: asyncio.Event | None
) -> bool:
    """与 express_reservation_sweeper 同语义：返回 True 表示被 stop 唤醒。"""
    if stop_event is None:
        await asyncio.sleep(delay_s)
        return False
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_s)
        return True
    except asyncio.TimeoutError:
        return False


async def sweeper_loop(*, stop_event: asyncio.Event | None = None) -> None:
    """周期跑 ``reconcile_once``。单轮崩溃只 log，绝不终止 loop。"""
    logger.info(
        "billing_reconciliation sweeper started (interval=%ss, batch=%d, "
        "window=%ss..%ss)",
        SWEEP_INTERVAL_S,
        SWEEP_BATCH_SIZE,
        MIN_AGE_S,
        MAX_AGE_S,
    )
    if await _interruptible_sleep(INITIAL_DELAY_S, stop_event):
        return
    while True:
        try:
            stats = await reconcile_once()
            if stats["scanned"] > 0:
                logger.info(
                    "billing_reconciliation tick: scanned=%d settled=%d errors=%d",
                    stats["scanned"],
                    stats["settled"],
                    stats["errors"],
                )
        except Exception:
            logger.exception(
                "billing_reconciliation tick failed; retrying next interval"
            )
        if await _interruptible_sleep(SWEEP_INTERVAL_S, stop_event):
            return


__all__ = [
    "reconcile_once",
    "sweeper_loop",
    "SWEEP_INTERVAL_S",
    "SWEEP_BATCH_SIZE",
    "INITIAL_DELAY_S",
    "MIN_AGE_S",
    "MAX_AGE_S",
]
