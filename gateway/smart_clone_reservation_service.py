"""P3a-2 — 智能版预览克隆 600 点预扣 reservation 服务（钱-正确性核心）.

plan 2026-06-14-p3-smart-clone-600-credit-subplan v3。承载 reservation 状态机
（``reserved → captured | released | expired``）的 reserve DB 逻辑：

单 transaction 原子完成：锁 ``users`` row → inline expire stale → 幂等查 →
库容门 → **信用预扣 600（reserve_credits_or_raise）** → INSERT reservation。
信用预扣条目 + reservation 行在**同一 commit**，二者要么都成、要么都回滚。

CodeX 钱-正确性不变量（v3 §3）：
- 锁 ``users`` row 串行化同 user 并发 reserve；user 不存在 fail-closed。
- 持锁后先 inline expire stale（不依赖 sweeper）；**expired/terminal reservation
  的信用结算（release）交独立 finalizer/sweeper 单一入口**（本服务只标记 status，
  不在此 release 信用——单一结算入口防 double settle）。
- 库容门含 active reservations（防并发穿透）。
- ``uq_smart_clone_reservation_active`` partial unique 是幂等第二道防线。

信用 reserve 的 ``reason_code`` 由 reservation_id 决定性派生
（``smart_clone_reserve_{reservation_id}``）——finalizer 凭 reservation 行即可
recompute 出来做 capture/release，无需额外字段。

PG-only 的并发原子性（users FOR UPDATE）在 sqlite 测不了；状态机 / 幂等 /
库容门计数 / inline-expire / insufficient / user-not-found 在 sqlite 单测覆盖。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from credits_service import InsufficientCreditsError, reserve_credits_or_raise
from models import SmartCloneReservation, User, UserVoice


RESERVED = "reserved"
CAPTURED = "captured"
RELEASED = "released"
EXPIRED = "expired"

PURPOSE = "smart_clone_minimax_600"


def credit_reserve_reason_code(reservation_id: object) -> str:
    """信用 reserve/capture/release 共用的 reason_code（决定性派生自 reservation_id）。

    finalizer 读 reservation 行即可 recompute，无需在表里另存字段。
    """
    return f"smart_clone_reserve_{reservation_id}"


@dataclass(frozen=True)
class SmartReserveOutcome:
    """``reserve_smart_clone_credit`` 结果。

    - ``"reserved"`` → 成功（新建或幂等命中），预览可走 MiniMax 克隆。
    - ``"denied"`` → ``deny_reason`` ∈ {voice_library_full, insufficient_credits}，
      预览走 CosyVoice/MiMo 预设音色（不阻断），**前端据 deny_reason 提示用户**。
    - ``"user_not_found"`` → users row 不存在，fail-closed，走预设。
    """

    status: str
    reservation_id: str | None = None
    deny_reason: str | None = None
    idempotent_hit: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _expire_stale_for_user(db: AsyncSession, user_id: object, *, now: datetime) -> int:
    """持锁后 inline expire 该 user 的过期 reserved（标记 status=expired）。

    **不在此 release 信用**——expired reservation 的信用退还由独立 finalizer/
    sweeper 单一入口处理（CodeX：单一结算入口防 double settle）。不 commit。
    """
    result = await db.execute(
        update(SmartCloneReservation)
        .where(
            SmartCloneReservation.user_id == user_id,
            SmartCloneReservation.status == RESERVED,
            SmartCloneReservation.expires_at < now,
        )
        .values(status=EXPIRED, reason_code="ttl_expired", updated_at=now)
    )
    return int(result.rowcount or 0)


async def count_active_smart_reservations(db: AsyncSession, user_id: object) -> int:
    """该 user 当前 active(reserved) smart clone reservation 数（库容门并发分量）。"""
    result = await db.execute(
        select(func.count())
        .select_from(SmartCloneReservation)
        .where(
            SmartCloneReservation.user_id == user_id,
            SmartCloneReservation.status == RESERVED,
        )
    )
    return int(result.scalar() or 0)


async def count_active_library_voices(db: AsyncSession, user_id: object) -> int:
    """跨 provider 个人音色库存量：active(``expired_at IS NULL``) 且**非临时**
    （``is_temporary=False``）的 user_voices 数（plan §6 库容门 = 跨 provider 合计）。
    临时音色有自己的成本闸，不挤占长期库容。"""
    result = await db.execute(
        select(func.count())
        .select_from(UserVoice)
        .where(
            UserVoice.user_id == user_id,
            UserVoice.expired_at.is_(None),
            UserVoice.is_temporary.is_(False),
        )
    )
    return int(result.scalar() or 0)


async def reserve_smart_clone_credit(
    db: AsyncSession,
    *,
    user_id: object,
    task_id: str,
    amount_credits: int,
    ttl_minutes: int,
    library_cap: int,
) -> SmartReserveOutcome:
    """原子预扣一个智能版预览克隆 600 点名额（v3 §3 预览阶段）。

    单 transaction：锁 users row → inline expire stale → 幂等查 → 库容门 →
    信用预扣（reserve_credits_or_raise）→ INSERT reservation → commit。
    信用预扣条目与 reservation 行**同一 commit**（原子）。

    ``library_cap`` / ``ttl_minutes`` / ``amount_credits`` 由 caller（create
    endpoint）从 admin/plan 读后传入；service 保持纯（可测）。
    """
    now = _now()

    # 1. 锁 users row（串行化同 user 并发；unknown user fail-closed）
    user_pk = (
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if user_pk is None:
        return SmartReserveOutcome(status="user_not_found")

    # 2. 持锁后先 inline expire 该 user 的 stale reserved（不依赖 sweeper）
    await _expire_stale_for_user(db, user_id, now=now)

    # 3. 幂等：同 (task_id, purpose) 的 active reservation 直接返回
    existing = (
        await db.execute(
            select(SmartCloneReservation).where(
                SmartCloneReservation.task_id == task_id,
                SmartCloneReservation.purpose == PURPOSE,
                SmartCloneReservation.status == RESERVED,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db.commit()  # 提交 inline expire
        return SmartReserveOutcome(
            status="reserved", reservation_id=str(existing.id), idempotent_hit=True
        )

    # 4. 库容门（含 active reservations 防并发穿透）→ 满则 denied、走预设
    lib_total = (
        await count_active_library_voices(db, user_id)
        + await count_active_smart_reservations(db, user_id)
    )
    if lib_total >= library_cap:
        await db.commit()  # 提交 inline expire（即便 deny）
        return SmartReserveOutcome(status="denied", deny_reason="voice_library_full")

    # 5. 信用预扣 600（不足 → InsufficientCreditsError，走预设）
    reservation_id = uuid.uuid4()
    reason = credit_reserve_reason_code(reservation_id)
    try:
        await reserve_credits_or_raise(
            db,
            user_id=user_id,
            job_id=task_id,
            estimated_credits=int(amount_credits),
            service_mode="smart",
            reason_code=reason,
        )
    except InsufficientCreditsError:
        # 不足：提交 inline expire（reserve_credits_or_raise 在加条目前就抛，
        # 无信用条目残留），返回 denied → 前端提示"点数不足，本次用预设"。
        await db.commit()
        return SmartReserveOutcome(status="denied", deny_reason="insufficient_credits")

    # 6. INSERT reservation（与上面信用 reserve 条目同一 commit → 原子）
    db.add(
        SmartCloneReservation(
            id=reservation_id,
            user_id=user_id,
            task_id=task_id,
            purpose=PURPOSE,
            amount_credits=int(amount_credits),
            status=RESERVED,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(minutes=int(ttl_minutes)),
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        # partial unique 第二道防线：并发同 (task_id,purpose) 竞态。rollback
        # 整事务（连同信用 reserve 条目一起撤销 → 不 double-reserve），重查
        # 已有 active reservation 返回（那条有自己的信用 reserve）。
        await db.rollback()
        existing_after = (
            await db.execute(
                select(SmartCloneReservation).where(
                    SmartCloneReservation.task_id == task_id,
                    SmartCloneReservation.purpose == PURPOSE,
                    SmartCloneReservation.status == RESERVED,
                )
            )
        ).scalar_one_or_none()
        if existing_after is not None:
            return SmartReserveOutcome(
                status="reserved",
                reservation_id=str(existing_after.id),
                idempotent_hit=True,
            )
        raise
    return SmartReserveOutcome(status="reserved", reservation_id=str(reservation_id))


__all__ = [
    "RESERVED", "CAPTURED", "RELEASED", "EXPIRED", "PURPOSE",
    "SmartReserveOutcome",
    "credit_reserve_reason_code",
    "count_active_smart_reservations",
    "count_active_library_voices",
    "reserve_smart_clone_credit",
]
