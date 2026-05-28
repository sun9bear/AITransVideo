"""Phase 4.3a PR2-B — Express auto-clone atomic reservation service.

承载 spec §4 reservation 状态机的 DB 逻辑（reserve / consume / release +
budget count + inline expire）。endpoint 层（PR2-C）薄封装这些函数。

核心不变量（spec §2 / §4.1）：

- **reserve 锁 users row**：``SELECT id FROM users WHERE id=:u FOR UPDATE``
  串行化同一 user 的并发 reserve（PG）；users 不存在 → fail-closed
  ``user_not_found``，不建 reservation。
- **持锁后先 inline expire stale**：不依赖 TTL sweeper —— 即使 sweeper
  挂了，过期 reservation 也不能占 cap。
- **幂等**：同 (user,job,speaker) 的 active(reserved) reservation 直接返回
  （partial unique index 第二道防线）。
- **cap count 含 active reservations**（spec §5）：daily / active_temp 计数
  = user_voices（PR1 counter）+ active reserved reservations，防并发穿透。
- **consume / release 状态机幂等**；service 自身**不静默吞异常**（release
  失败的 audit 由 pipeline safe wrapper 处理，service 抛真实异常）。

PG-only 的并发原子性（users row FOR UPDATE 阻塞语义）在 sqlite 测不了，
留 §10.7 真 PG 测试；本 service 的状态机 / 幂等 / 计数 / inline-expire /
unknown-user 在 sqlite 单测覆盖。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import ExpressCloneReservation, User
# PR1 counters（user_voices 侧）—— spec §5 cap = voices + reservations
from user_voice_service import (
    count_active_temporary_voices,
    count_express_auto_clones_today,
)


RESERVED = "reserved"
CONSUMED = "consumed"
RELEASED = "released"
EXPIRED = "expired"


@dataclass(frozen=True)
class ReserveOutcome:
    """``reserve`` 结果。``status`` 决定 caller 行为：

    - ``"reserved"`` → 成功（新建或幂等命中），可进付费路径
    - ``"denied"`` → cap 超限（``deny_reason``），回预设音色
    - ``"user_not_found"`` → users row 不存在，fail-closed，回预设音色
    """

    status: str
    reservation_id: str | None = None
    expires_at: datetime | None = None
    deny_reason: str | None = None
    idempotent_hit: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _expire_stale_for_user(db: AsyncSession, user_id: object, *, now: datetime) -> int:
    """持锁后 inline expire 该 user 的过期 reserved（spec §4.1 step 2）。

    返回 expire 的行数。**不** commit（由调用方 reserve 的 transaction 统一
    commit）。sweeper（§8）也复用同逻辑但跨所有 user。
    """
    result = await db.execute(
        update(ExpressCloneReservation)
        .where(
            ExpressCloneReservation.user_id == user_id,
            ExpressCloneReservation.status == RESERVED,
            ExpressCloneReservation.expires_at < now,
        )
        .values(status=EXPIRED, released_reason="ttl_expired", updated_at=now)
    )
    return int(result.rowcount or 0)


async def count_active_reservations_today(db: AsyncSession, user_id: object, *, now: datetime | None = None) -> int:
    """今天（UTC 自然日）该 user 的 active(reserved) reservation 数（spec §5 daily）。"""
    now = now or _now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.count())
        .select_from(ExpressCloneReservation)
        .where(
            ExpressCloneReservation.user_id == user_id,
            ExpressCloneReservation.status == RESERVED,
            ExpressCloneReservation.created_at >= today_start,
        )
    )
    return int(result.scalar() or 0)


async def count_active_reservations(db: AsyncSession, user_id: object) -> int:
    """该 user 当前 active(reserved) reservation 数（spec §5 active_temp）。"""
    result = await db.execute(
        select(func.count())
        .select_from(ExpressCloneReservation)
        .where(
            ExpressCloneReservation.user_id == user_id,
            ExpressCloneReservation.status == RESERVED,
        )
    )
    return int(result.scalar() or 0)


async def reserve(
    db: AsyncSession,
    *,
    user_id: object,
    job_id: str,
    speaker_id: str,
    target_model: str,
    ttl_minutes: int,
    daily_cap: int,
    active_temp_cap: int,
) -> ReserveOutcome:
    """原子预占一个 Express auto-clone 名额（spec §4.1）。

    单 transaction：锁 users row → inline expire stale → 幂等查 → cap
    检查（含 active reservations）→ INSERT。**PG-only** 的并发串行化靠
    users row FOR UPDATE；sqlite 无阻塞语义但逻辑路径相同（C-pg 测真并发）。

    ``daily_cap`` / ``active_temp_cap`` 由 caller（endpoint）从 admin_settings
    读后传入；service 不读 admin（保持纯，可测）。
    """
    now = _now()

    # 1. 锁 users row（串行化同 user 并发；unknown user fail-closed）
    user_pk = (
        await db.execute(
            select(User.id).where(User.id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if user_pk is None:
        # 不建 reservation；caller skip + audit user_not_found
        return ReserveOutcome(status="user_not_found")

    # 2. 持锁后先 inline expire 该 user 的 stale reserved（不依赖 sweeper）
    await _expire_stale_for_user(db, user_id, now=now)

    # 3. 幂等：查 active reservation（stale 已 expire，不会幂等命中过期行）
    existing = (
        await db.execute(
            select(ExpressCloneReservation).where(
                ExpressCloneReservation.user_id == user_id,
                ExpressCloneReservation.job_id == job_id,
                ExpressCloneReservation.speaker_id == speaker_id,
                ExpressCloneReservation.status == RESERVED,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db.commit()  # 提交 inline expire 的结果
        return ReserveOutcome(
            status="reserved",
            reservation_id=str(existing.id),
            expires_at=existing.expires_at,
            idempotent_hit=True,
        )

    # 4. cap 检查（spec §5：含 active reservations，防并发穿透）
    daily_total = (
        await count_express_auto_clones_today(db, user_id)
        + await count_active_reservations_today(db, user_id, now=now)
    )
    if daily_total >= daily_cap:
        await db.commit()  # 提交 inline expire（即便 deny 也要落 expire）
        return ReserveOutcome(status="denied", deny_reason="daily_cap_exceeded")

    active_temp_total = (
        await count_active_temporary_voices(db, user_id)
        + await count_active_reservations(db, user_id)
    )
    if active_temp_total >= active_temp_cap:
        await db.commit()
        return ReserveOutcome(status="denied", deny_reason="active_temp_cap_exceeded")

    # 5. INSERT reservation
    reservation = ExpressCloneReservation(
        id=uuid.uuid4(),
        user_id=user_id,
        job_id=job_id,
        speaker_id=speaker_id,
        status=RESERVED,
        target_model=target_model,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(minutes=int(ttl_minutes)),
    )
    db.add(reservation)
    await db.commit()
    return ReserveOutcome(
        status="reserved",
        reservation_id=str(reservation.id),
        expires_at=reservation.expires_at,
    )


@dataclass(frozen=True)
class TransitionOutcome:
    """consume / release 结果。``ok`` 表示状态机转换成功（或幂等已达目标态）。"""

    ok: bool
    status: str  # 转换后的 status（或冲突时的当前 status）
    conflict_reason: str | None = None


async def _fetch(db: AsyncSession, reservation_id: str) -> ExpressCloneReservation | None:
    try:
        rid = uuid.UUID(str(reservation_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return (
        await db.execute(
            select(ExpressCloneReservation).where(ExpressCloneReservation.id == rid)
        )
    ).scalar_one_or_none()


async def consume(
    db: AsyncSession, *, reservation_id: str, voice_id: str
) -> TransitionOutcome:
    """reserved → consumed（spec §4.2）。register-smart 成功后调。

    幂等：已 consumed 且 voice_id 相同 → ok。状态非 reserved（已
    released/expired）→ 冲突 ``reservation_not_reservable``（TTL 已回收，
    caller 需重新 reserve 或放弃）。

    **不静默吞异常**：reservation 不存在 → 返 conflict（不 raise，让 caller
    决策），但 DB 异常照常向上抛（service 不掩盖真实错误）。
    """
    row = await _fetch(db, reservation_id)
    if row is None:
        return TransitionOutcome(ok=False, status="missing", conflict_reason="reservation_not_found")
    if row.status == CONSUMED:
        # 幂等：同 voice_id 视为成功；不同 voice_id 视为冲突
        if (row.consumed_voice_id or "") == voice_id:
            return TransitionOutcome(ok=True, status=CONSUMED)
        return TransitionOutcome(ok=False, status=CONSUMED, conflict_reason="already_consumed_different_voice")
    if row.status != RESERVED:
        return TransitionOutcome(ok=False, status=row.status, conflict_reason="reservation_not_reservable")
    row.status = CONSUMED
    row.consumed_voice_id = voice_id
    row.updated_at = _now()
    await db.commit()
    return TransitionOutcome(ok=True, status=CONSUMED)


async def release(
    db: AsyncSession, *, reservation_id: str, reason: str
) -> TransitionOutcome:
    """reserved → released（spec §4.3）。clone 失败 / register 失败 / 主动放弃。

    幂等：已 released / expired → ok（idempotent release）。已 consumed →
    冲突 ``reservation_already_consumed``（不能 release 已消费的）。

    **不静默吞异常**：reservation 不存在 → conflict；DB 异常向上抛。
    release 自身失败的 audit 由 pipeline safe wrapper（spec §6.2
    ``_safe_release``）处理，service 层只负责状态转换 + 抛真实异常。
    """
    row = await _fetch(db, reservation_id)
    if row is None:
        return TransitionOutcome(ok=False, status="missing", conflict_reason="reservation_not_found")
    if row.status in (RELEASED, EXPIRED):
        return TransitionOutcome(ok=True, status=row.status)  # 幂等
    if row.status == CONSUMED:
        return TransitionOutcome(ok=False, status=CONSUMED, conflict_reason="reservation_already_consumed")
    row.status = RELEASED
    row.released_reason = (reason or "")[:64]
    row.updated_at = _now()
    await db.commit()
    return TransitionOutcome(ok=True, status=RELEASED)


async def expire_stale_reservations(db: AsyncSession, *, limit: int = 200) -> int:
    """TTL sweeper（spec §8）：跨所有 user expire 过期 reserved。

    PR2-D 的 lifespan sweeper 调本函数。reserve 内的 per-user inline expire
    是它的实时补充。返回 expire 的行数。
    """
    now = _now()
    # 先选出过期 reserved 的 id（单批 cap，防长事务）
    stale_ids = (
        await db.execute(
            select(ExpressCloneReservation.id)
            .where(
                ExpressCloneReservation.status == RESERVED,
                ExpressCloneReservation.expires_at < now,
            )
            .limit(limit)
        )
    ).scalars().all()
    if not stale_ids:
        return 0
    result = await db.execute(
        update(ExpressCloneReservation)
        .where(ExpressCloneReservation.id.in_(stale_ids))
        .values(status=EXPIRED, released_reason="ttl_expired", updated_at=now)
    )
    await db.commit()
    return int(result.rowcount or 0)


__all__ = [
    "ReserveOutcome",
    "TransitionOutcome",
    "reserve",
    "consume",
    "release",
    "count_active_reservations",
    "count_active_reservations_today",
    "expire_stale_reservations",
    "RESERVED",
    "CONSUMED",
    "RELEASED",
    "EXPIRED",
]
