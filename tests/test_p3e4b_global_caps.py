"""P3e-4b — 智能版预览克隆**全局**反滥用 cap 真生效（daily_global / inflight）.

plan 2026-06-14-p3e2-preview-lane-design.md §8 / p3-smart-clone-600-credit-subplan
§7（P1）。免费 3 分钟预览克隆是 MiniMax 付费资源入口，须有**全局**上限防被刷：

  - ``smart_preview_clone_daily_global_cap``（默认 200）：今日（Asia/Shanghai 自然日）
    跨所有 user 的 smart clone reservation 创建数上限。计**所有状态**（每条已授权
    一次克隆尝试 → fail-closed 抗刷）。
  - ``smart_preview_clone_inflight_cap``（默认 5）：当前全局在飞（status=reserved 且
    未过 TTL）reservation 数上限（供应商并发保护）。

钱-正确性铁律（本切片核心）：
  - cap deny 在**信用预扣 600 之前** → denied 时**绝不扣点**、不建 reservation 行。
  - cap deny **不阻断任务**：caller 据 deny_reason 走预设（entitled）/ 收 402
    （免费 exemption），与 insufficient_credits / voice_library_full 同语义。
  - **幂等命中不被 cap 挡**（已建的 reservation 重试必须仍 reserved）。
  - **软上限**：全局计数不被 users row 锁串行化，并发可轻微 overshoot；硬钱不变量
    （per-user 600 reserve 原子性）不受影响。

真 async SQLite（aiosqlite）测计数 + 状态机 + 钱-原子性；call-site wiring 用源码级
守卫（job_intercept 读两旗 + 透传 reserve），避开 DB stub 污染。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Column, MetaData, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

_REPO = Path(__file__).resolve().parent.parent
_gateway = str(_REPO / "gateway")
if _gateway not in sys.path:
    sys.path.insert(0, _gateway)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


from models import (  # noqa: E402
    CreditsBucket, CreditsLedger, SmartCloneReservation, UserVoice, CloneBillingEvent,
)
import smart_clone_reservation_service as svc  # noqa: E402
from free_service_quota import shanghai_day_start_utc  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

_users_md = MetaData()
_users_stub = Table("users", _users_md, Column("id", PG_UUID(as_uuid=True), primary_key=True))


async def _make_sessionmaker(*, bucket_remaining: int = 5000) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda s: _users_stub.create(s))
        await conn.run_sync(lambda s: UserVoice.__table__.create(s))
        await conn.run_sync(lambda s: SmartCloneReservation.__table__.create(s))
        await conn.run_sync(lambda s: CloneBillingEvent.__table__.create(s))
        await conn.run_sync(lambda s: CreditsBucket.__table__.create(s))
        await conn.run_sync(lambda s: CreditsLedger.__table__.create(s))
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sm() as db:
        await db.execute(_users_stub.insert().values(id=_USER))
        await db.execute(_users_stub.insert().values(id=_OTHER))
        if bucket_remaining > 0:
            db.add(CreditsBucket(
                id=uuid.uuid4(), user_id=_USER, bucket_type="free",
                granted=bucket_remaining, remaining=bucket_remaining, reserved=0,
            ))
        await db.commit()
    return sm


def _resv(
    user_id,
    task_id,
    *,
    status="reserved",
    created_at,
    expires_at,
    purpose=svc.PREVIEW_PURPOSE,
):
    return SmartCloneReservation(
        id=uuid.uuid4(), user_id=user_id, task_id=task_id, purpose=purpose,
        amount_credits=600, status=status, created_at=created_at,
        updated_at=created_at, expires_at=expires_at,
    )


async def _bucket_available(db) -> int:
    b = (await db.execute(select(CreditsBucket).where(CreditsBucket.user_id == _USER))).scalar_one_or_none()
    return 0 if b is None else (b.remaining - b.reserved)


# ---------------------------------------------------------------------------
# shanghai_day_start_utc — 日界（pure）
# ---------------------------------------------------------------------------


def test_shanghai_day_start_utc_known_instant():
    """SH 自然日 00:00 对应 UTC 时刻 = 该 UTC 日界 - 8h。2026-06-15T02:00Z 是
    SH 10:00（6-15），SH 当日 00:00 = 6-15T00:00+08:00 = 2026-06-14T16:00Z。"""
    inst = datetime(2026, 6, 15, 2, 0, tzinfo=timezone.utc)
    start = shanghai_day_start_utc(inst)
    assert start == datetime(2026, 6, 14, 16, 0, tzinfo=timezone.utc)


def test_shanghai_day_start_utc_just_after_local_midnight():
    """SH 00:30（= 前一 UTC 日 16:30Z）→ 日界仍是同一 SH 日 00:00 = 16:00Z。"""
    inst = datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc)  # SH 6-15 00:30
    assert shanghai_day_start_utc(inst) == datetime(2026, 6, 14, 16, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 全局计数器
# ---------------------------------------------------------------------------


def test_count_global_today_across_users_all_statuses():
    """今日全局计数 = 跨 user、跨所有状态、仅 created_at 落在 SH 今日内。"""
    async def go():
        sm = await _make_sessionmaker()
        now = datetime.now(timezone.utc)
        day_start = shanghai_day_start_utc(now)
        before = day_start - timedelta(hours=1)  # 昨天（SH）
        async with sm() as db:
            # 今日：_USER reserved, _OTHER captured, _USER released → 计 3
            db.add(_resv(_USER, "t1", status="reserved", created_at=day_start + timedelta(minutes=1), expires_at=now + timedelta(hours=1)))
            db.add(_resv(_OTHER, "t2", status="captured", created_at=day_start + timedelta(hours=2), expires_at=now + timedelta(hours=1)))
            db.add(_resv(_USER, "t3", status="released", created_at=day_start + timedelta(hours=3), expires_at=now + timedelta(hours=1)))
            # 昨日 reserved → 不计
            db.add(_resv(_OTHER, "t4", status="reserved", created_at=before, expires_at=now + timedelta(hours=1)))
            await db.commit()
            n = await svc.count_global_smart_reservations_today(db, now=now)
            assert n == 3
    _run(go())


def test_count_global_inflight_only_active_nonexpired_across_users():
    """在飞全局计数 = 跨 user、仅 status=reserved 且 expires_at>=now。"""
    async def go():
        sm = await _make_sessionmaker()
        now = datetime.now(timezone.utc)
        async with sm() as db:
            # 在飞：_USER reserved 未过期, _OTHER reserved 未过期 → 计 2
            db.add(_resv(_USER, "i1", status="reserved", created_at=now, expires_at=now + timedelta(hours=1)))
            db.add(_resv(_OTHER, "i2", status="reserved", created_at=now, expires_at=now + timedelta(minutes=5)))
            # 不在飞：reserved 但已过 TTL（卡死行，不应虚占并发名额）
            db.add(_resv(_USER, "i3", status="reserved", created_at=now - timedelta(hours=2), expires_at=now - timedelta(minutes=1)))
            # 不在飞：captured / released
            db.add(_resv(_OTHER, "i4", status="captured", created_at=now, expires_at=now + timedelta(hours=1)))
            db.add(_resv(_USER, "i5", status="released", created_at=now, expires_at=now + timedelta(hours=1)))
            await db.commit()
            n = await svc.count_global_inflight_smart_reservations(db, now=now)
            assert n == 2
    _run(go())


def test_preview_caps_ignore_full_smart_reservations():
    """Preview anti-abuse caps must not consume paid full-Smart clone capacity."""
    async def go():
        sm = await _make_sessionmaker()
        now = datetime.now(timezone.utc)
        day_start = shanghai_day_start_utc(now)
        async with sm() as db:
            db.add(_resv(
                _USER,
                "pv1",
                created_at=day_start + timedelta(minutes=1),
                expires_at=now + timedelta(hours=1),
                purpose=svc.PREVIEW_PURPOSE,
            ))
            db.add(_resv(
                _OTHER,
                "full1",
                created_at=day_start + timedelta(minutes=2),
                expires_at=now + timedelta(hours=1),
                purpose=svc.PURPOSE,
            ))
            await db.commit()
            today = await svc.count_global_smart_reservations_today(db, now=now)
            inflight = await svc.count_global_inflight_smart_reservations(db, now=now)
            assert today == 1
            assert inflight == 1
    _run(go())


# ---------------------------------------------------------------------------
# 🔥 reserve cap gates — 钱-正确性
# ---------------------------------------------------------------------------


def test_reserve_inflight_cap_denies_without_charge():
    """全局在飞达 inflight_cap → denied(inflight_cap_exceeded)，不扣点、不建行。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        now = datetime.now(timezone.utc)
        async with sm() as db:
            # 别的 user 占满 2 个在飞名额
            db.add(_resv(_OTHER, "x1", created_at=now, expires_at=now + timedelta(hours=1)))
            db.add(_resv(_OTHER, "x2", created_at=now, expires_at=now + timedelta(hours=1)))
            await db.commit()
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="new", amount_credits=600,
                ttl_minutes=60, library_cap=100, daily_global_cap=1000, inflight_cap=2,
            )
            assert o.status == "denied" and o.deny_reason == "inflight_cap_exceeded"
            assert await _bucket_available(db) == 800  # 未扣
            assert (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.task_id == "new"))).scalar_one_or_none() is None
    _run(go())


def test_reserve_daily_cap_denies_without_charge():
    """今日全局达 daily_global_cap → denied(daily_cap_exceeded)，不扣点、不建行。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        now = datetime.now(timezone.utc)
        day_start = shanghai_day_start_utc(now)
        async with sm() as db:
            # 今日已有 2 条（别的 user，含一条 released → 仍计入今日授权）
            db.add(_resv(_OTHER, "d1", status="captured", created_at=day_start + timedelta(minutes=1), expires_at=now + timedelta(hours=1)))
            db.add(_resv(_OTHER, "d2", status="released", created_at=day_start + timedelta(minutes=2), expires_at=now + timedelta(hours=1)))
            await db.commit()
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="new2", amount_credits=600,
                ttl_minutes=60, library_cap=100, daily_global_cap=2, inflight_cap=1000,
            )
            assert o.status == "denied" and o.deny_reason == "daily_cap_exceeded"
            assert await _bucket_available(db) == 800
            assert (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.task_id == "new2"))).scalar_one_or_none() is None
    _run(go())


def test_reserve_inflight_checked_before_daily():
    """两 cap 同时超时 inflight 先报（瞬时供应商压力是更强信号）。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        now = datetime.now(timezone.utc)
        async with sm() as db:
            db.add(_resv(_OTHER, "b1", created_at=now, expires_at=now + timedelta(hours=1)))
            await db.commit()
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="new3", amount_credits=600,
                ttl_minutes=60, library_cap=100, daily_global_cap=1, inflight_cap=1,
            )
            assert o.deny_reason == "inflight_cap_exceeded"
    _run(go())


def test_reserve_caps_none_unbounded():
    """caps=None（旧 caller / flag 关）→ 不设上限，reserve 正常进行（向后兼容）。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="nb", amount_credits=600,
                ttl_minutes=60, library_cap=100, daily_global_cap=None, inflight_cap=None,
            )
            assert o.status == "reserved"
            assert await _bucket_available(db) == 200
    _run(go())


def test_reserve_idempotent_hit_bypasses_caps():
    """🔥 已有 active reservation 的同 task 重试 → 即使 cap=0 也必须仍 reserved
    （幂等命中不被 cap 挡，否则重试把已授权的预览误降级）。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            o1 = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="idem", amount_credits=600,
                ttl_minutes=60, library_cap=100, daily_global_cap=1000, inflight_cap=1000,
            )
            assert o1.status == "reserved"
            # 现在全局在飞=1、今日=1；用 cap=1（即"已满"）重试同 task
            o2 = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="idem", amount_credits=600,
                ttl_minutes=60, library_cap=100, daily_global_cap=1, inflight_cap=1,
            )
            assert o2.status == "reserved" and o2.idempotent_hit
            assert o2.reservation_id == o1.reservation_id
            assert await _bucket_available(db) == 200  # 仍只扣一次
    _run(go())


def test_reserve_inflight_cap_ignores_stale_reserved():
    """卡死的 reserved（已过 TTL）不计在飞 → 不应虚占名额阻断新 reserve。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        now = datetime.now(timezone.utc)
        async with sm() as db:
            # 别的 user 两条 reserved 但都已过期（卡死）
            db.add(_resv(_OTHER, "s1", created_at=now - timedelta(hours=3), expires_at=now - timedelta(hours=1)))
            db.add(_resv(_OTHER, "s2", created_at=now - timedelta(hours=3), expires_at=now - timedelta(hours=1)))
            await db.commit()
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="fresh", amount_credits=600,
                ttl_minutes=60, library_cap=100, daily_global_cap=1000, inflight_cap=2,
            )
            assert o.status == "reserved"  # 过期 reserved 不占在飞 → 放行
    _run(go())


# ---------------------------------------------------------------------------
# call-site wiring（源码级守卫）
# ---------------------------------------------------------------------------


def _ji_flat() -> str:
    return " ".join((_REPO / "gateway" / "job_intercept.py").read_text(encoding="utf-8").split())


def test_job_intercept_reads_both_caps_from_admin():
    flat = _ji_flat()
    assert "smart_preview_clone_daily_global_cap" in flat
    assert "smart_preview_clone_inflight_cap" in flat


def test_job_intercept_passes_both_caps_to_reserve():
    """reserve 调用必须透传 daily_global_cap= 与 inflight_cap=（否则 service 端
    上限恒 None = inert，cap 形同虚设）。"""
    flat = _ji_flat()
    # 定位 _reserve_smart_clone( 调用窗口
    a = flat.find("_reserve_smart_clone(")
    assert a != -1, "未找到 _reserve_smart_clone( 调用"
    window = flat[a:a + 600]
    assert "daily_global_cap=" in window
    assert "inflight_cap=" in window
    assert "purpose=" in window
    assert "PREVIEW_PURPOSE" in window


# ---------------------------------------------------------------------------
# 全局 advisory lock（CodeX P3e-4b HIGH 硬化：soft→hard cap）
# ---------------------------------------------------------------------------


def test_acquire_global_cap_lock_noop_on_sqlite():
    """非 PG 方言 → _acquire_global_cap_lock 是 no-op（不抛错）；PG 才真取
    pg_advisory_xact_lock 串行化全局 count→insert。"""
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            await svc._acquire_global_cap_lock(db)  # sqlite → 直接 return，不报错
            # 锁路径已接入：caps 非 None 的 reserve 会先调它，仍正常 reserve
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="lk", amount_credits=600,
                ttl_minutes=60, library_cap=100, daily_global_cap=200, inflight_cap=5,
            )
            assert o.status == "reserved"
    _run(go())


def test_reserve_acquires_global_lock_only_when_caps_enforced():
    """源码守卫：仅当 inflight_cap/daily_global_cap 非 None 时取全局锁（caps=None
    inert，不引入无谓全局串行化）；reserve 必须真调用 _acquire_global_cap_lock。"""
    src = (_REPO / "gateway" / "smart_clone_reservation_service.py").read_text(encoding="utf-8")
    flat = " ".join(src.split())
    assert "pg_advisory_xact_lock" in flat
    a = flat.find("inflight_cap is not None or daily_global_cap is not None")
    assert a != -1, "未找到 caps-enforced 守卫"
    assert "_acquire_global_cap_lock(db)" in flat[a:a + 300]


def test_migration_038_chains_and_indexes_created_at():
    """038 链在 037 之后，且为 smart_clone_reservations.created_at 建索引（服务全局
    daily cap count，避免反滥用闸成 DB 热点）。"""
    mig = (_REPO / "gateway" / "alembic" / "versions" / "038_smart_clone_created_at_index.py").read_text(encoding="utf-8")
    assert 'down_revision: Union[str, None] = "037_smart_clone_reservations"' in mig
    assert "idx_smart_clone_reservation_created_at" in mig
    assert '"smart_clone_reservations"' in mig and '["created_at"]' in mig
