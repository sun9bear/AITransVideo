"""P3a-2 — smart 克隆 600 点预扣 reserve 服务 money-correctness 测试.

plan 2026-06-14-p3-smart-clone-600-credit-subplan v3 §3。真 async SQLite
(aiosqlite) 测 reserve 状态机 + 信用预扣原子性 + 库容门 + 幂等 + inline-expire。
PG-only 的 FOR UPDATE 阻塞在 sqlite 测不了（逻辑路径相同）。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, MetaData, Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

_gateway = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway not in sys.path:
    sys.path.insert(0, _gateway)
# 注意：本测试**不** stub sys.modules["database"]——服务无 module-level database
# import（实测 import OK）。stub 会 mutate 真实 database 模块、污染其它 gateway
# 测试（cosyvoice_clone_api 等），见 memory feedback_test_database_stub_convention。


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


from models import (  # noqa: E402
    CloneBillingEvent, CreditsBucket, CreditsLedger, SmartCloneReservation, UserVoice,
)
import smart_clone_reservation_service as svc  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_UNKNOWN = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

_users_md = MetaData()
_users_stub = Table("users", _users_md, Column("id", PG_UUID(as_uuid=True), primary_key=True))


async def _make_sessionmaker(*, bucket_remaining: int = 800) -> async_sessionmaker[AsyncSession]:
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
        if bucket_remaining > 0:
            db.add(CreditsBucket(
                id=uuid.uuid4(), user_id=_USER, bucket_type="free",
                granted=bucket_remaining, remaining=bucket_remaining, reserved=0,
            ))
        await db.commit()
    return sm


async def _bucket_available(db) -> int:
    b = (await db.execute(select(CreditsBucket).where(CreditsBucket.user_id == _USER))).scalar_one_or_none()
    return 0 if b is None else (b.remaining - b.reserved)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_reserve_happy_creates_row_and_reserves_credit():
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="job_a", amount_credits=600,
                ttl_minutes=30, library_cap=10,
            )
            assert o.status == "reserved" and not o.idempotent_hit
            # reservation 行落库 status=reserved amount=600
            row = (await db.execute(select(SmartCloneReservation))).scalar_one()
            assert row.status == "reserved" and row.amount_credits == 600
            assert str(row.id) == o.reservation_id
            # 信用真预扣 600（available 800→200）
            assert await _bucket_available(db) == 200
    _run(go())


# ---------------------------------------------------------------------------
# insufficient credits → denied, NO reservation, NO credit moved
# ---------------------------------------------------------------------------


def test_reserve_insufficient_credits_denied_no_charge():
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=100)  # < 600
        async with sm() as db:
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="job_b", amount_credits=600,
                ttl_minutes=30, library_cap=10,
            )
            assert o.status == "denied" and o.deny_reason == "insufficient_credits"
            # 无 reservation 行、信用未动（available 仍 100）
            assert (await db.execute(select(SmartCloneReservation))).scalar_one_or_none() is None
            assert await _bucket_available(db) == 100
    _run(go())


# ---------------------------------------------------------------------------
# library full → denied, NO credit charged
# ---------------------------------------------------------------------------


def test_reserve_library_full_denied_before_charge():
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        now = datetime.now(timezone.utc)
        async with sm() as db:
            # seed 1 个 active reservation（占库容），cap=1 → 新 reserve 被库满拒
            db.add(SmartCloneReservation(
                id=uuid.uuid4(), user_id=_USER, task_id="job_other",
                purpose=svc.PURPOSE, amount_credits=600, status="reserved",
                created_at=now, updated_at=now, expires_at=now + timedelta(minutes=30),
            ))
            await db.commit()
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="job_c", amount_credits=600,
                ttl_minutes=30, library_cap=1,
            )
            assert o.status == "denied" and o.deny_reason == "voice_library_full"
            # 库满在信用预扣之前 → 信用未动（available 仍 800）
            assert await _bucket_available(db) == 800
            # 没为 job_c 建 reservation
            rows = (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.task_id == "job_c"))).scalars().all()
            assert rows == []
    _run(go())


# ---------------------------------------------------------------------------
# user not found → fail-closed
# ---------------------------------------------------------------------------


def test_reserve_unknown_user_fails_closed():
    async def go():
        sm = await _make_sessionmaker()
        async with sm() as db:
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_UNKNOWN, task_id="job_d", amount_credits=600,
                ttl_minutes=30, library_cap=10,
            )
            assert o.status == "user_not_found"
            assert (await db.execute(select(SmartCloneReservation))).scalar_one_or_none() is None
    _run(go())


# ---------------------------------------------------------------------------
# 🔥 idempotency: same task_id → no double reserve / no double charge
# ---------------------------------------------------------------------------


def test_reserve_idempotent_same_task_no_double_charge():
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            o1 = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="job_e", amount_credits=600,
                ttl_minutes=30, library_cap=10,
            )
            o2 = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="job_e", amount_credits=600,
                ttl_minutes=30, library_cap=10,
            )
            assert o1.status == "reserved" and o2.status == "reserved"
            assert o2.idempotent_hit and o2.reservation_id == o1.reservation_id
            # 只一条 reservation；信用只扣一次 600（available 200，不是 -400）
            assert len((await db.execute(select(SmartCloneReservation))).scalars().all()) == 1
            assert await _bucket_available(db) == 200
    _run(go())


# ---------------------------------------------------------------------------
# inline expire stale
# ---------------------------------------------------------------------------


def test_reserve_inline_expires_stale_reserved():
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=2000)
        now = datetime.now(timezone.utc)
        async with sm() as db:
            # 手插一条已过期 reserved（expires_at 在过去）
            stale_id = uuid.uuid4()
            db.add(SmartCloneReservation(
                id=stale_id, user_id=_USER, task_id="job_stale",
                purpose=svc.PURPOSE, amount_credits=600, status="reserved",
                created_at=now - timedelta(hours=2), updated_at=now - timedelta(hours=2),
                expires_at=now - timedelta(hours=1),
            ))
            await db.commit()
            # 新 reserve → inline expire 把 stale 标 expired
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="job_f", amount_credits=600,
                ttl_minutes=30, library_cap=10,
            )
            assert o.status == "reserved"
            stale = (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.id == stale_id))).scalar_one()
            assert stale.status == "expired" and stale.reason_code == "ttl_expired"
    _run(go())


# ---------------------------------------------------------------------------
# reason_code 决定性派生（finalizer 凭 reservation 行 recompute）
# ---------------------------------------------------------------------------


def test_credit_reason_code_deterministic():
    rid = "11111111-1111-1111-1111-111111111111"
    assert svc.credit_reserve_reason_code(rid) == "smart_clone_reserve_" + rid


# ---------------------------------------------------------------------------
# P3b register+bill 单一事务（CodeX #2）
# ---------------------------------------------------------------------------


async def _reserve(db, task_id="job_r"):
    o = await svc.reserve_smart_clone_credit(
        db, user_id=_USER, task_id=task_id, amount_credits=600,
        ttl_minutes=30, library_cap=10,
    )
    assert o.status == "reserved"
    return o.reservation_id


def test_register_bill_happy_atomic_event_and_voice():
    """🔥 billed：同一事务写 chargeable billing event + 入 user_voices。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            rid = await _reserve(db, "job_r")
            out = await svc.register_smart_clone_with_billing(
                db, user_id=_USER, task_id="job_r", reservation_id=rid,
                voice_id="mm_voice_1", label="主说话人", source_job_id="job_r",
            )
            assert out.status == "billed"
            ev = (await db.execute(select(CloneBillingEvent))).scalar_one()
            assert ev.chargeable is True and ev.provider == "minimax" and ev.voice_id == "mm_voice_1"
            uv = (await db.execute(select(UserVoice).where(UserVoice.voice_id == "mm_voice_1"))).scalar_one()
            assert uv.created_from == "smart_preview"
    _run(go())


def test_register_bill_idempotent_no_double():
    """🔥 幂等：同 reservation 第二次 register+bill → idempotent，不双写。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            rid = await _reserve(db, "job_r2")
            kw = dict(user_id=_USER, task_id="job_r2", reservation_id=rid,
                      voice_id="mm_v2", label="x", source_job_id="job_r2")
            o1 = await svc.register_smart_clone_with_billing(db, **kw)
            o2 = await svc.register_smart_clone_with_billing(db, **kw)
            assert o1.status == "billed" and o2.status == "idempotent"
            assert len((await db.execute(select(CloneBillingEvent))).scalars().all()) == 1
            assert len((await db.execute(select(UserVoice).where(UserVoice.voice_id == "mm_v2"))).scalars().all()) == 1
    _run(go())


def test_register_bill_no_active_reservation_does_not_bill():
    """无有效 active reservation（不存在/非 reserved/不属本 task）→ 不 bill 不入库。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            rid = await _reserve(db, "job_r3")
            # 用错 task_id（reservation 属 job_r3，却声称 job_WRONG）
            out = await svc.register_smart_clone_with_billing(
                db, user_id=_USER, task_id="job_WRONG", reservation_id=rid,
                voice_id="mm_v3", label="x", source_job_id="job_WRONG",
            )
            assert out.status == "no_active_reservation"
            assert (await db.execute(select(CloneBillingEvent))).scalar_one_or_none() is None
            assert (await db.execute(select(UserVoice).where(UserVoice.voice_id == "mm_v3"))).scalar_one_or_none() is None
    _run(go())


def test_register_bill_released_reservation_does_not_bill():
    """reservation 已 released（非 reserved）→ 不 bill（防对已退款的 task 再扣）。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            rid = await _reserve(db, "job_r4")
            row = (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.id == __import__("uuid").UUID(rid)))).scalar_one()
            row.status = "released"
            await db.commit()
            out = await svc.register_smart_clone_with_billing(
                db, user_id=_USER, task_id="job_r4", reservation_id=rid,
                voice_id="mm_v4", label="x", source_job_id="job_r4",
            )
            assert out.status == "no_active_reservation"
            assert (await db.execute(select(CloneBillingEvent))).scalar_one_or_none() is None
    _run(go())
