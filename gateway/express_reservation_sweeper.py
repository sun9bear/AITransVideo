"""Phase 4.3a PR2-D — Express auto-clone reservation TTL sweeper.

spec §8：gateway lifespan 后台任务，周期扫 ``status='reserved' AND
expires_at < now()`` 的过期 reservation，标 ``expired``
（``released_reason='ttl_expired'``）。回收"预占未消费"的 cap 名额——
典型场景：reserve 后进程崩溃 / 该 user 再也没发起新 reserve，inline
expire（reserve 内 §4.1 step 2 的实时主防线）够不到，靠这个后台兜底。

**边界（spec §8 + §1.2 NG6；Codex PR2-D 授权）**：

- **只动 reservation 表**：纯 DB 状态流转，只调一个函数
  ``express_reservation_service.expire_stale_reservations``。
- **不调任何付费 / 外部 API**：不 import mainland worker / sample uploader
  / register-smart client / httpx / requests / boto3。AST 守卫锁死
  （``tests/test_phase43a_pr2d_reservation_sweeper.py``）。
- **不接 pipeline**：与 Express auto-clone 编排（PR2-E/F）完全解耦。
- **幂等**：选行条件 ``status='reserved'``，重跑跳过已 expired 行。
- **单批 cap**：默认 200 行防长事务。
- **fail-safe**：单次 sweep 异常只 log，绝不让 sweeper 故障影响 gateway
  启动或请求处理（lifespan wiring 包 try/except；loop 内 try/except）。
- **≠ 音色 ``temporary_expires_at`` sweeper**（Phase 4.3b）：那个删 DashScope
  临时音色（付费 ``delete_voice``）；本 sweeper 只翻 DB 状态（免费）。

默认保守（spec §8 建议 5 分钟周期 + 单批 200 行）；env 可调但默认安全。
不引入 admin_settings 字段（避免再加 full-body save 守卫面），常量 +
env override 已足够（与 ``r2_artifact_sweeper`` 同模式）。
"""
from __future__ import annotations

import asyncio
import logging
import os

# 仅 import 纯 DB 依赖：database 是 side-effect-free proxy（init_db 前不触库），
# express_reservation_service 是纯 DB 状态机。**绝不** import worker / uploader
# / httpx / requests / boto3（AST 守卫锁死）。
from database import async_session
import express_reservation_service as _reservation_service

logger = logging.getLogger(__name__)

# 保守默认：5 分钟周期、单批 200 行（spec §8）。env 可调，默认安全。
SWEEP_INTERVAL_S = int(
    os.environ.get("AVT_EXPRESS_RESERVATION_SWEEP_INTERVAL_S", "300")
)
SWEEP_BATCH_SIZE = int(
    os.environ.get("AVT_EXPRESS_RESERVATION_SWEEP_BATCH_SIZE", "200")
)
# 启动后首次扫描的延迟，错开其它 sweeper（pack/project/r2）减少 startup
# contention。默认 240s（< 第一个 SWEEP_INTERVAL，仍在第一个周期内首扫）。
INITIAL_DELAY_S = int(
    os.environ.get("AVT_EXPRESS_RESERVATION_SWEEP_INITIAL_DELAY_S", "240")
)


async def sweep_once(*, session_factory=None) -> int:
    """跑一轮 expire：返回本轮 expire 的行数。

    只调 ``expire_stale_reservations``（纯 DB，内部自带单批 cap + commit）。
    DB 异常**向上抛**——由 ``sweeper_loop`` 的 try/except 兜（这里不吞，方便
    单测直接断言异常传播 + loop 续命）。

    ``session_factory`` 仅供测试注入 in-memory sqlite sessionmaker；生产
    走默认 ``database.async_session``（proxy，init_db 后解析真实 sessionmaker）。
    """
    factory = session_factory or async_session
    async with factory() as db:
        return await _reservation_service.expire_stale_reservations(
            db, limit=SWEEP_BATCH_SIZE
        )


async def _interruptible_sleep(
    delay_s: float, stop_event: asyncio.Event | None
) -> bool:
    """睡 ``delay_s`` 秒，期间 ``stop_event`` 被 set 则提前醒。

    返回 ``True`` 表示是被 stop 唤醒（caller 应退出 loop），``False`` 表示
    睡满了（继续）。``stop_event=None``（生产 lifespan 模式）退化为普通
    ``asyncio.sleep``——shutdown 靠 ``task.cancel()`` 打断。``stop_event``
    主要给测试用，避免真起无限循环。
    """
    if stop_event is None:
        await asyncio.sleep(delay_s)
        return False
    # 先查 is_set()：asyncio.wait_for(coro, timeout=0) 不会调度被包裹的
    # Event.wait() 协程，已 set 的 event 会被漏判（误返 TimeoutError）。
    # 这个 fast-path 同时让 delay_s=0（测试场景）能立刻响应 stop。
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_s)
        return True  # delay 内被 set
    except asyncio.TimeoutError:
        return False  # 睡满，继续


async def sweeper_loop(*, stop_event: asyncio.Event | None = None) -> None:
    """周期跑 ``sweep_once``。单次崩溃只 log + 续到下一周期，**绝不**让
    sweeper 故障终止 loop / 影响 gateway。

    生产：lifespan ``create_task(sweeper_loop())``（``stop_event=None``），
    shutdown 时 ``task.cancel()`` 打断 ``asyncio.sleep`` 干净退出。
    测试：传 ``stop_event`` + 把 ``INITIAL_DELAY_S`` / ``SWEEP_INTERVAL_S``
    调 0，跑几轮后 set event 退出，验证 loop 续命 + 不崩。
    """
    logger.info(
        "express_reservation_sweeper started (interval=%ss, batch=%d)",
        SWEEP_INTERVAL_S,
        SWEEP_BATCH_SIZE,
    )
    # 启动延迟（可被 stop 打断）
    if await _interruptible_sleep(INITIAL_DELAY_S, stop_event):
        return
    while True:
        try:
            expired = await sweep_once()
            if expired > 0:
                logger.info(
                    "express_reservation_sweeper expired %d stale reservation(s)",
                    expired,
                )
        except Exception:
            # 永不崩 loop：瞬时 DB 抖动不该终结整个 sweeper 生命周期。
            logger.exception(
                "express_reservation_sweeper tick failed; retrying next interval"
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
