"""P3c — Smart 预览克隆 reservation TTL 结算 sweeper（钱-正确性兜底）.

plan 2026-06-14-p3-smart-clone-600-credit-subplan v3 §4/§P3c。gateway lifespan
后台任务，周期扫 ``status='reserved' AND expires_at < now()`` + ``status='expired'``
的未结算 reservation，逐个调 ``settle_smart_clone_reservation``：有 chargeable
``clone_billing_event`` → capture 600，无 → **release 600**（退还）。

**为什么需要它（CodeX P3c 审核 P1）**：``job_terminal_mirror`` 的 finalizer 是
timely 主路径，但它 marker-gated 且依赖 job reach terminal。覆盖不到的漏退场景：
- reservation 建了但 ``smart_state`` marker 没传到（create/pipeline 漏 stamp）；
- job 卡死非终态（worker 没起 / 崩溃），永不触发 terminal mirror；
- finalizer 因瞬时异常 / 连接池超时本轮跳过。
这些都会让 600 永久挂在 ``reserved`` / ``expired``。本 sweeper 是兜底，保证
"任意未结算 reservation 最终都会 capture 或 release"。

**边界**：
- **只动 reservation + credits ledger（内部记账，非付费 API）**：调
  ``sweep_settle_stale_reservations`` → ``settle_smart_clone_reservation`` →
  ``shadow_capture`` / ``shadow_release``（内部信用桶流转，**不** import worker /
  uploader / httpx / requests / boto3 / MiniMax client）。settle 是用户知情付费
  路径的**记账结算**，capture 实扣的是 reserve 时已冻结的额度、release 是退还，
  二者都不触发任何外部付费调用——符合 CLAUDE.md「付费 API 不能自动调用」。
- **幂等**：``settle_smart_clone_reservation`` 行锁 + 只结算 reserved/expired +
  per-reservation reason_code，重跑跳过已 captured/released 行。
- **单批 cap**：默认 200 行防长事务。每个 reservation 自带 commit（一行失败
  不回滚整批）。
- **fail-safe**：单次 sweep 异常只 log，绝不让 sweeper 故障影响 gateway 启动 /
  请求处理（lifespan wiring 包 try/except；loop 内 try/except）。

结构镜像 ``express_reservation_sweeper``（同 5 分钟周期 + 单批 200 + 错峰启动延迟 +
可中断 sleep 便于测试）。
"""
from __future__ import annotations

import asyncio
import logging
import os

# 仅 import 纯 DB 依赖：database 是 side-effect-free proxy，
# smart_clone_reservation_service 是纯 DB 状态机 + 内部信用结算。**绝不** import
# worker / uploader / httpx / requests / boto3 / MiniMax client（AST 守卫锁死）。
from database import async_session
import smart_clone_reservation_service as _svc

logger = logging.getLogger(__name__)

# 保守默认：5 分钟周期、单批 200 行。env 可调，默认安全。
SWEEP_INTERVAL_S = int(
    os.environ.get("AVT_SMART_CLONE_RESERVATION_SWEEP_INTERVAL_S", "300")
)
SWEEP_BATCH_SIZE = int(
    os.environ.get("AVT_SMART_CLONE_RESERVATION_SWEEP_BATCH_SIZE", "200")
)
# 启动后首次扫描延迟，错开其它 sweeper（pack/project/r2/express）减少 startup
# contention。默认 300s。
INITIAL_DELAY_S = int(
    os.environ.get("AVT_SMART_CLONE_RESERVATION_SWEEP_INITIAL_DELAY_S", "300")
)


async def sweep_once(*, session_factory=None) -> dict:
    """跑一轮结算：返回 ``sweep_settle_stale_reservations`` 的 stats dict
    （``{"captured": n, "released": n, "settlement_failed": n, "other": n}``）。

    只调 ``sweep_settle_stale_reservations``（内部自带单批 cap + per-reservation
    commit）。DB 异常**向上抛**——由 ``sweeper_loop`` 的 try/except 兜（这里不吞，
    方便单测断言异常传播 + loop 续命）。

    ``session_factory`` 仅供测试注入 in-memory sqlite sessionmaker；生产走默认
    ``database.async_session``（proxy，init_db 后解析真实 sessionmaker）。
    """
    factory = session_factory or async_session
    async with factory() as db:
        return await _svc.sweep_settle_stale_reservations(db, limit=SWEEP_BATCH_SIZE)


async def _interruptible_sleep(
    delay_s: float, stop_event: asyncio.Event | None
) -> bool:
    """睡 ``delay_s`` 秒，期间 ``stop_event`` 被 set 则提前醒。

    返回 ``True`` 表示被 stop 唤醒（caller 应退出 loop），``False`` 表示睡满。
    ``stop_event=None``（生产 lifespan）退化为普通 ``asyncio.sleep``——shutdown
    靠 ``task.cancel()`` 打断。``stop_event`` 主要给测试用，避免真起无限循环。
    """
    if stop_event is None:
        await asyncio.sleep(delay_s)
        return False
    # 先查 is_set()：wait_for(coro, timeout=0) 不调度被包裹协程，已 set 的
    # event 会被漏判。fast-path 同时让 delay_s=0（测试）能立刻响应 stop。
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_s)
        return True
    except asyncio.TimeoutError:
        return False


async def sweeper_loop(*, stop_event: asyncio.Event | None = None) -> None:
    """周期跑 ``sweep_once``。单次崩溃只 log + 续到下一周期，**绝不**让 sweeper
    故障终止 loop / 影响 gateway。

    生产：lifespan ``create_task(sweeper_loop())``（``stop_event=None``），
    shutdown 时 ``task.cancel()`` 打断 ``asyncio.sleep`` 干净退出。
    测试：传 ``stop_event`` + 把延迟 / 周期调 0，跑几轮后 set event 退出。
    """
    logger.info(
        "smart_clone_reservation_sweeper started (interval=%ss, batch=%d)",
        SWEEP_INTERVAL_S,
        SWEEP_BATCH_SIZE,
    )
    if await _interruptible_sleep(INITIAL_DELAY_S, stop_event):
        return
    while True:
        try:
            stats = await sweep_once()
            settled = stats.get("captured", 0) + stats.get("released", 0)
            if settled > 0 or stats.get("settlement_failed", 0):
                logger.info(
                    "smart_clone_reservation_sweeper settled stale reservations: %s",
                    stats,
                )
        except Exception:
            # 永不崩 loop：瞬时 DB 抖动不该终结整个 sweeper 生命周期。
            logger.exception(
                "smart_clone_reservation_sweeper tick failed; retrying next interval"
            )
        if await _interruptible_sleep(SWEEP_INTERVAL_S, stop_event):
            return


__all__ = [
    "sweep_once",
    "sweeper_loop",
    "SWEEP_INTERVAL_S",
    "SWEEP_BATCH_SIZE",
    "INITIAL_DELAY_S",
]
