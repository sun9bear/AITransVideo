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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from credits_service import (
    InsufficientCreditsError,
    get_user_buckets,
    reserve_credits_or_raise,
)
from models import CloneBillingEvent, Job, SmartCloneReservation, User, UserVoice


RESERVED = "reserved"
CAPTURED = "captured"
RELEASED = "released"
EXPIRED = "expired"

PURPOSE = "smart_clone_minimax_600"
PREVIEW_PURPOSE = "smart_preview_clone_minimax_600"
SMART_PREVIEW_CLONE_RESERVE_CREDITS = 600
_REGISTER_FAILED_HANDOFF_REASONS = frozenset({
    "clone_library_register_failed",
})


def credit_reserve_reason_code(reservation_id: object) -> str:
    """信用 reserve/capture/release 共用的 reason_code（决定性派生自 reservation_id）。

    finalizer 读 reservation 行即可 recompute，无需在表里另存字段。
    """
    return f"smart_clone_reserve_{reservation_id}"


def credit_related_job_id(reservation_id: object) -> str:
    """Ledger related id for clone-credit reserve/capture/release rows."""
    return f"smart_clone_{reservation_id}"


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
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def count_active_smart_reservations(
    db: AsyncSession,
    user_id: object,
    *,
    exclude_reservation_id: object | None = None,
) -> int:
    """Count reserved clone slots that have not materialized into billed voices."""
    conditions = [
        SmartCloneReservation.user_id == user_id,
        SmartCloneReservation.status == RESERVED,
    ]
    if exclude_reservation_id is not None:
        conditions.append(SmartCloneReservation.id != exclude_reservation_id)
    result = await db.execute(
        select(func.count())
        .select_from(SmartCloneReservation)
        .outerjoin(
            CloneBillingEvent,
            and_(
                CloneBillingEvent.reservation_id == SmartCloneReservation.id,
                CloneBillingEvent.chargeable.is_(True),
            ),
        )
        .where(*conditions, CloneBillingEvent.id.is_(None))
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


def _available_credits_from_buckets(buckets: list[object]) -> int:
    return sum(
        max(
            0,
            int(getattr(bucket, "remaining", 0) or 0)
            - max(0, int(getattr(bucket, "reserved", 0) or 0)),
        )
        for bucket in buckets
    )


async def count_global_smart_reservations_today(
    db: AsyncSession, *, now: datetime | None = None
) -> int:
    """今日（Asia/Shanghai 自然日）**全局**（跨所有 user）smart clone reservation
    创建数 —— ``smart_preview_clone_daily_global_cap`` 分量（P3e-4b）.

    计**所有状态**（reserved/captured/released/expired）：每条 reservation 已授权过
    一次克隆尝试，释放/过期的也算"今日已发名额"——fail-closed 抗刷（防 create→fail
    循环刷免费预览却不增计数）。幂等重试不建新行 → 不重复计数。
    """
    from free_service_quota import shanghai_day_start_utc

    now = now or _now()
    day_start = shanghai_day_start_utc(now)
    result = await db.execute(
        select(func.count())
        .select_from(SmartCloneReservation)
        .where(
            SmartCloneReservation.created_at >= day_start,
            SmartCloneReservation.purpose == PREVIEW_PURPOSE,
        )
    )
    return int(result.scalar() or 0)


async def count_global_inflight_smart_reservations(
    db: AsyncSession, *, now: datetime | None = None
) -> int:
    """当前**全局**在飞（``status=reserved`` 且 ``expires_at >= now``）smart clone
    reservation 数 —— ``smart_preview_clone_inflight_cap`` 分量（供应商并发保护）.

    过 TTL 的 reserved（卡死非终态、待 sweeper/inline-expire 回收）**不计**——否则
    一堆卡死行会虚占并发名额、把活人挡在外面。
    """
    now = now or _now()
    result = await db.execute(
        select(func.count())
        .select_from(SmartCloneReservation)
        .where(
            SmartCloneReservation.status == RESERVED,
            SmartCloneReservation.expires_at >= now,
            SmartCloneReservation.purpose == PREVIEW_PURPOSE,
        )
    )
    return int(result.scalar() or 0)


# 全局 cap advisory-lock 固定 key（任意常量，在 signed-bigint 范围内）—— 标识
# "smart-clone reserve 全局串行域"，与任何其它 advisory lock key 不冲突。
_SMART_CLONE_GLOBAL_LOCK_KEY = 0x53435243  # "SCRC"


async def _acquire_global_cap_lock(db: AsyncSession) -> None:
    """串行化全局 cap（daily/inflight）的 count→insert 窗口（CodeX P3e-4b HIGH）.

    全局 cap 是 ``count → reserve → insert``，仅靠 users-row ``FOR UPDATE`` 只串行
    **同一 user**；跨 user 并发可各自读到 count<cap 后全部放行 → overshoot ≈ 并发量，
    把反滥用**硬**上限打成软上限。取一个**固定 key 的事务级 advisory lock**（PG）让全局
    reserve 串成单队、count 在锁内一致 → 真硬 cap。lock 随本事务 commit/rollback 自动
    释放（``xact`` 级）。

    锁顺序固定为 users-row 锁 → 本全局锁（本函数内唯一获取点）→ 无 lock-ordering 死锁。
    sqlite/其它无 advisory lock → **no-op**（逻辑路径相同；并发硬上限只在 PG 生效，与本
    服务既有 users-row FOR UPDATE 的 PG-only 语义一致）。预览克隆是低频 funnel，全局
    串行化开销可忽略。
    """
    bind = db.bind
    if bind is None or bind.dialect.name != "postgresql":
        return
    from sqlalchemy import text

    await db.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": _SMART_CLONE_GLOBAL_LOCK_KEY},
    )


async def reserve_smart_clone_credit(
    db: AsyncSession,
    *,
    user_id: object,
    task_id: str,
    amount_credits: int,
    ttl_minutes: int,
    library_cap: int,
    required_available_credits: int | None = None,
    daily_global_cap: int | None = None,
    inflight_cap: int | None = None,
    purpose: str = PURPOSE,
) -> SmartReserveOutcome:
    """原子预扣一个智能版预览克隆 600 点名额（v3 §3 预览阶段）。

    单 transaction：锁 users row → inline expire stale → 幂等查 → 库容门 →
    信用预扣（reserve_credits_or_raise）→ INSERT reservation → commit。
    信用预扣条目与 reservation 行**同一 commit**（原子）。

    ``library_cap`` / ``ttl_minutes`` / ``amount_credits`` 由 caller（create
    endpoint）从 admin/plan 读后传入；service 保持纯（可测）。
    """
    now = _now()
    reservation_purpose = str(purpose or PURPOSE)

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
                SmartCloneReservation.purpose == reservation_purpose,
                SmartCloneReservation.status == RESERVED,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db.commit()  # 提交 inline expire
        return SmartReserveOutcome(
            status="reserved", reservation_id=str(existing.id), idempotent_hit=True
        )

    # 3.5 全局反滥用 cap（P3e-4b）—— 保护 MiniMax 账号被免费 3min 预览刷爆。两者均在
    #     **信用预扣 600 之前** fail-closed deny（denied 不阻断：caller 据 deny_reason
    #     走预设 / 免费 exemption 收 402，与 insufficient/library_full 同语义）。
    #     **幂等命中（上面已 return）不入此 cap**——已授权的预览重试不被误降级。
    #     **软上限**：全局计数不被 users row 锁串行化（该锁只串行同一 user），并发可
    #     轻微 overshoot；硬钱不变量（per-user 600 reserve 原子性）不受影响。
    #     inflight 先于 daily：瞬时在飞名额是更强的供应商压力信号。
    #     cap is None（旧 caller / flag 关）→ 不设上限（inert，向后兼容）。
    if inflight_cap is not None or daily_global_cap is not None:
        # CodeX P3e-4b HIGH：先取全局 advisory lock 串行化 count→insert，把 soft cap
        # 收紧为 hard cap（PG；sqlite no-op）。仅当确有 cap 时取锁（caps=None 全 inert）。
        await _acquire_global_cap_lock(db)
    if inflight_cap is not None:
        inflight_total = await count_global_inflight_smart_reservations(db, now=now)
        if inflight_total >= int(inflight_cap):
            await db.commit()  # 提交 inline expire（即便 deny）
            return SmartReserveOutcome(status="denied", deny_reason="inflight_cap_exceeded")
    if daily_global_cap is not None:
        daily_total = await count_global_smart_reservations_today(db, now=now)
        if daily_total >= int(daily_global_cap):
            await db.commit()
            return SmartReserveOutcome(status="denied", deny_reason="daily_cap_exceeded")

    # 4. 库容门（含 active reservations 防并发穿透）→ 满则 denied、走预设
    lib_total = (
        await count_active_library_voices(db, user_id)
        + await count_active_smart_reservations(db, user_id)
    )
    if lib_total >= library_cap:
        await db.commit()  # 提交 inline expire（即便 deny）
        return SmartReserveOutcome(status="denied", deny_reason="voice_library_full")

    required_total = int(required_available_credits or amount_credits)
    if required_total > int(amount_credits):
        buckets = await get_user_buckets(db, user_id, for_update=True)
        if _available_credits_from_buckets(buckets) < required_total:
            await db.commit()
            return SmartReserveOutcome(
                status="denied", deny_reason="insufficient_credits"
            )

    # 5. 信用预扣 600（不足 → InsufficientCreditsError，走预设）
    reservation_id = uuid.uuid4()
    reason = credit_reserve_reason_code(reservation_id)
    credit_job_id = credit_related_job_id(reservation_id)
    try:
        await reserve_credits_or_raise(
            db,
            user_id=user_id,
            job_id=credit_job_id,
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
            purpose=reservation_purpose,
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
                    SmartCloneReservation.purpose == reservation_purpose,
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


@dataclass(frozen=True)
class RegisterBillOutcome:
    """``register_smart_clone_with_billing`` 结果。

    - ``"billed"`` → 首次写入 chargeable billing event + 入库（同一事务）。
    - ``"idempotent"`` → 该 reservation 已有 billing event（pipeline 重试）→ no-op。
    - ``"no_active_reservation"`` → reservation 不存在 / 非 reserved / 不属本
      task+user → **不 bill、不入库**（pipeline 本不该在无 reservation 下克隆；
      caller 应视为异常并 best-effort 删 MiniMax voice）。
    """

    status: str
    reservation_id: str | None = None


@dataclass(frozen=True)
class ActiveReservationOutcome:
    active: bool
    reason: str
    reservation_id: str | None = None


async def check_smart_clone_reservation_active(
    db: AsyncSession,
    *,
    user_id: object,
    task_id: str,
    reservation_id: object,
) -> ActiveReservationOutcome:
    try:
        res_pk = (
            reservation_id
            if isinstance(reservation_id, uuid.UUID)
            else uuid.UUID(str(reservation_id))
        )
    except (ValueError, AttributeError, TypeError):
        await db.rollback()
        return ActiveReservationOutcome(False, "invalid_reservation_id")

    res = (
        await db.execute(
            select(SmartCloneReservation).where(SmartCloneReservation.id == res_pk)
        )
    ).scalar_one_or_none()
    if res is None:
        await db.rollback()
        return ActiveReservationOutcome(False, "no_active_reservation")
    if res.status != RESERVED:
        await db.rollback()
        return ActiveReservationOutcome(False, f"status_{res.status}", str(res_pk))
    if str(res.task_id) != str(task_id) or str(res.user_id) != str(user_id):
        await db.rollback()
        return ActiveReservationOutcome(False, "reservation_mismatch", str(res_pk))
    now = _now()
    expires_at = res.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at < now:
        await db.rollback()
        return ActiveReservationOutcome(False, "expired", str(res_pk))
    existing = (
        await db.execute(
            select(CloneBillingEvent.id).where(
                CloneBillingEvent.reservation_id == res_pk
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await db.rollback()
        return ActiveReservationOutcome(False, "already_billed", str(res_pk))
    await db.rollback()
    return ActiveReservationOutcome(True, "active", str(res_pk))


async def register_smart_clone_with_billing(
    db: AsyncSession,
    *,
    user_id: object,
    task_id: str,
    reservation_id: object,
    voice_id: str,
    label: str,
    provider: str = "minimax_voice_clone",
    tts_provider: str | None = "minimax_tts",
    platform: str | None = "minimax_domestic",
    source_speaker_id: str | None = None,
    source_job_id: str | None = None,
    source_type: str | None = None,
    source_ref: str | None = None,
    source_content_hash: str | None = None,
    source_upload_md5: str | None = None,
    source_video_title: str | None = None,
    source_speaker_name: str | None = None,
    source_speaker_name_key: str | None = None,
    source_published_at: datetime | None = None,
    source_content_summary: str | None = None,
    source_content_era: str | None = None,
    source_content_tags: object | None = None,
    clone_sample_seconds: float | None = None,
    clone_sample_segment_ids: object | None = None,
    target_model: str | None = None,
    notes: str | None = None,
    library_cap: int = 30,
) -> RegisterBillOutcome:
    """P3b — MiniMax 克隆成功后**单一事务**写 durable billing event + 入库
    （CodeX 钱-正确性 #2）。

    同一 transaction：① 行锁 reservation（FOR UPDATE）校验 status=reserved 且
    属本 (task_id, user_id) ② 幂等查 chargeable billing event ③ 写 billing
    event(chargeable=true) ④ ``add_user_voice(commit=False)`` 入库 ⑤ 一起 commit。
    任一失败整体回滚。**钱的事实(billing event)与音色入库原子**；信用 capture
    由独立 finalizer 凭本 event 做（本函数不动信用）。

    ``uq_clone_billing_event_reservation`` 唯一约束 = 幂等第二道防线（pipeline
    重试 / 并发双写 → 第二个抛 IntegrityError → 回滚返回 idempotent）。
    """
    from user_voice_service import add_user_voice

    # reservation_id 可能是 str（reserve outcome 返回 str(id)）→ 强制转 UUID，
    # 否则 UUID 列 bind processor 调 str.hex 抛 AttributeError。非法 → 当无 reservation。
    try:
        res_pk = reservation_id if isinstance(reservation_id, uuid.UUID) else uuid.UUID(str(reservation_id))
    except (ValueError, AttributeError, TypeError):
        await db.rollback()
        return RegisterBillOutcome(status="no_active_reservation")

    # 1. 行锁 reservation + 校验（无有效 active reservation → 不 bill 不入库）
    res = (
        await db.execute(
            select(SmartCloneReservation)
            .where(SmartCloneReservation.id == res_pk)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        res is None
        or res.status != RESERVED
        or str(res.task_id) != str(task_id)
        or str(res.user_id) != str(user_id)
    ):
        await db.rollback()
        return RegisterBillOutcome(status="no_active_reservation")

    # 2. 幂等：该 reservation 已有 billing event → no-op（pipeline 重试）
    existing = (
        await db.execute(
            select(CloneBillingEvent).where(
                CloneBillingEvent.reservation_id == res_pk
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing_voice_id = str(existing.voice_id or "")
        await db.rollback()
        if existing_voice_id != str(voice_id or ""):
            return RegisterBillOutcome(
                status="idempotency_conflict", reservation_id=str(res_pk)
            )
        return RegisterBillOutcome(status="idempotent", reservation_id=str(res_pk))

    # 3. Re-lock the same user quota domain before consuming the reserved slot.
    # Another clone path may have filled the library after reserve completed.
    # Count other active smart reservations, but exclude this reservation
    # because it is the slot this callback is trying to consume.
    user_pk = (
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if user_pk is None:
        await db.rollback()
        return RegisterBillOutcome(status="no_active_reservation")
    now = _now()
    await _expire_stale_for_user(db, user_id, now=now)
    expires_at = res.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at < now:
        res.status = EXPIRED
        res.reason_code = "ttl_expired"
        res.updated_at = now
        await db.commit()
        return RegisterBillOutcome(
            status="no_active_reservation", reservation_id=str(res_pk)
        )
    other_reserved = await count_active_smart_reservations(
        db, user_id, exclude_reservation_id=res_pk
    )
    active_library_load = await count_active_library_voices(db, user_id) + other_reserved
    if active_library_load >= int(library_cap):
        await db.rollback()
        return RegisterBillOutcome(
            status="voice_library_full", reservation_id=str(res_pk)
        )

    # 4. 写 durable billing event（chargeable=true）= 唯一权威计费信号
    try:
        db.add(
            CloneBillingEvent(
                id=uuid.uuid4(),
                task_id=task_id,
                reservation_id=res_pk,
                provider="minimax",
                voice_id=voice_id,
                chargeable=True,
            )
        )
    # 4. 入库（commit=False，同一事务）
        # Keep the pending billing event from autoflushing inside the
        # add_user_voice lookup. Its explicit flush/commit still lands in this
        # idempotency handler if a concurrent writer wins the reservation.
        with db.no_autoflush:
            await add_user_voice(
                db,
                user_id=user_id,
                voice_id=voice_id,
                label=label,
                provider=provider,
                tts_provider=tts_provider,
                platform=platform,
                source_speaker_id=source_speaker_id,
                source_job_id=source_job_id,
                source_type=source_type,
                source_ref=source_ref,
                source_content_hash=source_content_hash,
                source_upload_md5=source_upload_md5,
                source_video_title=source_video_title,
                source_speaker_name=source_speaker_name,
                source_speaker_name_key=source_speaker_name_key,
                source_published_at=source_published_at,
                source_content_summary=source_content_summary,
                source_content_era=source_content_era,
                source_content_tags=source_content_tags,
                clone_sample_seconds=clone_sample_seconds,
                clone_sample_segment_ids=clone_sample_segment_ids,
                target_model=target_model,
                created_from="smart_preview",
                notes=notes,
                commit=False,
            )
        # 5. 一起 commit（billing event + user_voice 原子）
        await db.commit()
    except IntegrityError:
        # uq_clone_billing_event_reservation 竞态 → 另一并发写已成 → 幂等
        await db.rollback()
        existing_after = (
            await db.execute(
                select(CloneBillingEvent).where(
                    CloneBillingEvent.reservation_id == res_pk
                )
            )
        ).scalar_one_or_none()
        if existing_after is not None:
            existing_voice_id = str(existing_after.voice_id or "")
            await db.rollback()
            if existing_voice_id == str(voice_id or ""):
                return RegisterBillOutcome(
                    status="idempotent", reservation_id=str(res_pk)
                )
            return RegisterBillOutcome(
                status="idempotency_conflict", reservation_id=str(res_pk)
            )
        await db.rollback()
        return RegisterBillOutcome(
            status="idempotency_conflict", reservation_id=str(res_pk)
        )
    return RegisterBillOutcome(status="billed", reservation_id=str(res_pk))


_CAPTURE_REASON = "smart_clone_capture"
_REGISTER_FAILED_CAPTURE_REASON = "smart_clone_regfail_cap"
_RELEASE_REASON = "smart_clone_release"


@dataclass(frozen=True)
class SettleOutcome:
    """``settle_smart_clone_reservation`` 结果。

    - ``"captured"`` → 有 chargeable billing event → 实扣 600。
    - ``"released"`` → 无 chargeable event（克隆没触发/失败/expired）→ 退还 600。
    - ``"already_settled"`` → reservation 已 captured/released（幂等 no-op）。
    - ``"not_found"`` → reservation 不存在。
    - ``"settlement_failed"`` → 信用结算未落 ledger（strict 验证不过）→ **不**改
      reservation 状态，caller 应重试（防"状态说已结算但信用没动"错账）。
    """

    status: str
    reservation_id: str | None = None


def _is_register_failed_handoff_state(smart_state: object) -> bool:
    if not isinstance(smart_state, Mapping):
        return False
    return str(smart_state.get("reason") or "") in _REGISTER_FAILED_HANDOFF_REASONS


async def _has_register_failed_handoff(
    db: AsyncSession,
    res: SmartCloneReservation,
    *,
    smart_state_override: object | None = None,
) -> bool:
    if smart_state_override is not None:
        return _is_register_failed_handoff_state(smart_state_override)
    result = await db.execute(
        select(Job.smart_state).where(
            Job.job_id == res.task_id,
            Job.user_id == res.user_id,
        )
    )
    return _is_register_failed_handoff_state(result.scalar_one_or_none())


async def settle_smart_clone_reservation(
    db: AsyncSession,
    *,
    reservation_id: object,
    service_mode: str = "smart",
    smart_state_override: object | None = None,
) -> SettleOutcome:
    """P3c — 智能版克隆 reservation 终态结算（finalizer，钱-正确性核心）.

    单 transaction 幂等结算：行锁 reservation → 已结算则 no-op → 有 chargeable
    billing event 则 **capture 600**、否则 **release 600** → **strict 验证信用
    结算 ledger 真写入**（CodeX #3：不把 shadow_capture/release 的 [] 当成功）→
    置 status=captured|released + settled_at → commit。

    ``status IN (reserved, expired)`` 都可结算：expired 也走本函数 release
    （CodeX #1：expired+未结算必退，防 600 永久挂 reserved）。
    """
    from credits_service import _has_existing_settlement, shadow_capture, shadow_release
    from models import CloneBillingEvent

    try:
        res_pk = reservation_id if isinstance(reservation_id, uuid.UUID) else uuid.UUID(str(reservation_id))
    except (ValueError, AttributeError, TypeError):
        return SettleOutcome(status="not_found")

    now = _now()
    res = (
        await db.execute(
            select(SmartCloneReservation)
            .where(SmartCloneReservation.id == res_pk)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if res is None:
        return SettleOutcome(status="not_found")
    if res.status in (CAPTURED, RELEASED):
        await db.rollback()
        return SettleOutcome(status="already_settled", reservation_id=str(res_pk))

    reserve_reason = credit_reserve_reason_code(res.id)
    credit_job_id = credit_related_job_id(res.id)
    # **per-reservation** 结算 reason_code（CodeX 钱-loop 审核）：含 reservation_id，
    # 让 _has_existing_settlement 按本 reservation 精确判定"本次结算真落 ledger"。
    # 否则同 task 多 reservation 时第二个会撞第一个的 capture entry → shadow_capture
    # 以为已结算跳过 → 第二个 600 永久挂 reserved（扣了不退）。
    capture_reason = f"{_CAPTURE_REASON}_{res.id}"
    register_failed_capture_reason = f"{_REGISTER_FAILED_CAPTURE_REASON}_{res.id}"
    release_reason = f"{_RELEASE_REASON}_{res.id}"
    event = (
        await db.execute(
            select(CloneBillingEvent).where(CloneBillingEvent.reservation_id == res_pk)
        )
    ).scalar_one_or_none()
    chargeable = event is not None and bool(event.chargeable)
    register_failed_handoff = (
        not chargeable
        and await _has_register_failed_handoff(
            db, res, smart_state_override=smart_state_override
        )
    )
    amount_credits = int(res.amount_credits or 0)

    if amount_credits <= 0:
        if chargeable or register_failed_handoff:
            res.status = CAPTURED
            res.captured_voice_id = event.voice_id if event is not None else None
            res.reason_code = "captured" if chargeable else "captured_register_failed"
            final = "captured"
        else:
            res.status = RELEASED
            res.reason_code = res.reason_code or "released_no_clone"
            final = "released"
        res.settled_at = now
        res.updated_at = now
        await db.commit()
        return SettleOutcome(status=final, reservation_id=str(res_pk))

    if chargeable or register_failed_handoff:
        reason_code = capture_reason if chargeable else register_failed_capture_reason
        await shadow_capture(
            db, user_id=res.user_id, job_id=credit_job_id,
            actual_credits=amount_credits, service_mode=service_mode,
            reason_code=reason_code, reserve_reason_code=reserve_reason,
        )
        settled_ok = await _has_existing_settlement(
            db, user_id=res.user_id, job_id=credit_job_id,
            reason_code=reason_code, reserve_reason_code=reserve_reason,
        )
        if not settled_ok:
            await db.rollback()
            return SettleOutcome(status="settlement_failed", reservation_id=str(res_pk))
        res.status = CAPTURED
        res.captured_voice_id = event.voice_id if event is not None else None
        res.reason_code = "captured" if chargeable else "captured_register_failed"
        final = "captured"
    else:
        await shadow_release(
            db, user_id=res.user_id, job_id=credit_job_id,
            reason_code=release_reason, reserve_reason_code=reserve_reason,
        )
        settled_ok = await _has_existing_settlement(
            db, user_id=res.user_id, job_id=credit_job_id,
            reason_code=release_reason, reserve_reason_code=reserve_reason,
        )
        if not settled_ok:
            await db.rollback()
            return SettleOutcome(status="settlement_failed", reservation_id=str(res_pk))
        res.status = RELEASED
        res.reason_code = res.reason_code if res.status == EXPIRED else (res.reason_code or "released_no_clone")
        final = "released"

    res.settled_at = now
    res.updated_at = now
    await db.commit()
    return SettleOutcome(status=final, reservation_id=str(res_pk))


async def settle_smart_clone_reservations_for_task(
    db: AsyncSession,
    *,
    task_id: str,
    service_mode: str = "smart",
    smart_state_override: object | None = None,
) -> dict:
    """P3c 终态 finalizer 入口（plan v3 §4）—— 结算某 task 的所有 active
    (reserved/expired) smart clone reservation。

    逐个调 ``settle_smart_clone_reservation``（行锁 + 幂等 + strict 验证 + 自带
    commit/rollback）：有 chargeable billing event → capture，无 → release。
    幂等：无 reservation / 全已结算 → no-op（空计数）。

    ⚠️ **必须用专用 session 调用**：``settle_smart_clone_reservation`` 内部
    commit/rollback，与 ``job_terminal_mirror`` 那种"批量、caller 末尾统一
    commit"的 session 语义冲突——其 already_settled/settlement_failed 分支的
    rollback 会连带丢弃同批其他 job 的 mirror 改动。终态结算入口
    （``job_terminal_mirror``）须另开 ``async_session()`` 传入。TTL sweeper
    （``sweep_settle_stale_reservations``）是非终态卡死的兜底。
    """
    rows = (
        await db.execute(
            select(SmartCloneReservation.id).where(
                SmartCloneReservation.task_id == task_id,
                SmartCloneReservation.status.in_([RESERVED, EXPIRED]),
            )
        )
    ).scalars().all()
    stats = {"captured": 0, "released": 0, "settlement_failed": 0, "other": 0}
    for rid in rows:
        out = await settle_smart_clone_reservation(
            db,
            reservation_id=rid,
            service_mode=service_mode,
            smart_state_override=smart_state_override,
        )
        stats[out.status if out.status in stats else "other"] += 1
    return stats


async def sweep_settle_stale_reservations(db: AsyncSession, *, limit: int = 200) -> dict:
    """P3c TTL sweeper — 结算所有过期未结算 reservation（CodeX #1）.

    扫 ``status=reserved 且 expires_at < now`` + ``status=expired`` 的（这些任务
    可能卡死非终态、job 永不 reach terminal、finalizer 永不被触发）→ 逐个调
    ``settle_smart_clone_reservation``（无 chargeable event → release，退还 600）。
    幂等；返回结算统计。独立于 job terminal mirror（CodeX：单一结算入口 + 兜底）。
    """
    now = _now()
    rows = (
        await db.execute(
            select(SmartCloneReservation.id).where(
                SmartCloneReservation.status.in_([RESERVED, EXPIRED]),
                SmartCloneReservation.expires_at < now,
            ).limit(int(limit))
        )
    ).scalars().all()
    stats = {"captured": 0, "released": 0, "settlement_failed": 0, "other": 0}
    for rid in rows:
        out = await settle_smart_clone_reservation(db, reservation_id=rid)
        stats[out.status if out.status in stats else "other"] += 1
    return stats


__all__ = [
    "RESERVED", "CAPTURED", "RELEASED", "EXPIRED", "PURPOSE", "PREVIEW_PURPOSE",
    "SMART_PREVIEW_CLONE_RESERVE_CREDITS",
    "SmartReserveOutcome",
    "RegisterBillOutcome",
    "SettleOutcome",
    "ActiveReservationOutcome",
    "credit_reserve_reason_code",
    "credit_related_job_id",
    "count_active_smart_reservations",
    "count_active_library_voices",
    "count_global_smart_reservations_today",
    "count_global_inflight_smart_reservations",
    "reserve_smart_clone_credit",
    "check_smart_clone_reservation_active",
    "register_smart_clone_with_billing",
    "settle_smart_clone_reservation",
    "settle_smart_clone_reservations_for_task",
    "sweep_settle_stale_reservations",
]
