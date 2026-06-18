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

import pytest
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
    CloneBillingEvent, CreditsBucket, CreditsLedger, Job, SmartCloneReservation, UserVoice,
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
_TEST_ENGINES = []


@pytest.fixture(autouse=True)
def _dispose_sqlite_engines():
    yield
    while _TEST_ENGINES:
        _run(_TEST_ENGINES.pop().dispose())


async def _make_sessionmaker(*, bucket_remaining: int = 800) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    _TEST_ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(lambda s: _users_stub.create(s))
        await conn.run_sync(lambda s: Job.__table__.create(s))
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


async def _bucket_remaining_reserved(db) -> tuple[int, int]:
    b = (await db.execute(select(CreditsBucket).where(CreditsBucket.user_id == _USER))).scalar_one_or_none()
    return (0, 0) if b is None else (b.remaining, b.reserved)


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
            ledger = (await db.execute(select(CreditsLedger))).scalar_one()
            assert ledger.related_job_id == f"smart_clone_{row.id}"
            assert ledger.related_job_id != row.task_id
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


def test_reserve_combined_credit_requirement_denied_no_charge():
    """Combined Smart base+clone affordability is checked inside the user lock."""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=700)  # clone ok, base+clone not ok
        async with sm() as db:
            o = await svc.reserve_smart_clone_credit(
                db,
                user_id=_USER,
                task_id="job_combined",
                amount_credits=600,
                ttl_minutes=30,
                library_cap=10,
                required_available_credits=800,
            )
            assert o.status == "denied" and o.deny_reason == "insufficient_credits"
            assert (await db.execute(select(SmartCloneReservation))).scalar_one_or_none() is None
            assert await _bucket_available(db) == 700
    _run(go())


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


async def _reserve(db, task_id="job_r", *, library_cap=10):
    o = await svc.reserve_smart_clone_credit(
        db, user_id=_USER, task_id=task_id, amount_credits=600,
        ttl_minutes=30, library_cap=library_cap,
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


def test_register_bill_preserves_source_metadata():
    """Billed registration must keep reuse/audit metadata from the pipeline."""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        try:
            async with sm() as db:
                rid = await _reserve(db, "job_meta")
                published_at = datetime(2024, 5, 1, tzinfo=timezone.utc)
                out = await svc.register_smart_clone_with_billing(
                    db,
                    user_id=_USER,
                    task_id="job_meta",
                    reservation_id=rid,
                    voice_id="mm_meta",
                    label="Speaker Meta",
                    source_speaker_id="speaker_a",
                    source_job_id="job_meta",
                    source_type="youtube_url",
                    source_ref="https://youtu.be/source",
                    source_content_hash="youtube:source",
                    source_video_title="Source Title",
                    source_speaker_name="Speaker A",
                    source_speaker_name_key="speaker a",
                    source_published_at=published_at,
                    source_content_summary="channel: test",
                    source_content_era="2024",
                    source_content_tags={"channel": "Test", "tags": ["AI"]},
                    clone_sample_seconds=12.5,
                    clone_sample_segment_ids=[1, 2],
                    notes="Smart auto-clone from job job_meta",
                )
                assert out.status == "billed"

                uv = (
                    await db.execute(select(UserVoice).where(UserVoice.voice_id == "mm_meta"))
                ).scalar_one()
                assert uv.source_type == "youtube_url"
                assert uv.source_ref == "https://youtu.be/source"
                assert uv.source_content_hash == "youtube:source"
                assert uv.source_video_title == "Source Title"
                assert uv.source_speaker_name == "Speaker A"
                assert uv.source_speaker_name_key == "speaker a"
                stored_published_at = uv.source_published_at
                if stored_published_at.tzinfo is None:
                    stored_published_at = stored_published_at.replace(tzinfo=timezone.utc)
                assert stored_published_at == published_at
                assert uv.source_content_summary == "channel: test"
                assert uv.source_content_era == "2024"
                assert uv.source_content_tags == {"channel": "Test", "tags": ["AI"]}
                assert uv.clone_sample_seconds == 12.5
                assert uv.clone_sample_segment_ids == [1, 2]
                assert uv.notes == "Smart auto-clone from job job_meta"
        finally:
            await sm.kw["bind"].dispose()

    _run(go())


def test_check_active_reservation_requires_reserved_unbilled_slot():
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=1400)
        try:
            async with sm() as db:
                rid = await _reserve(db, "job_active")
                active = await svc.check_smart_clone_reservation_active(
                    db, user_id=_USER, task_id="job_active", reservation_id=rid,
                )
                assert active.active is True
                assert active.reason == "active"

                await svc.register_smart_clone_with_billing(
                    db, user_id=_USER, task_id="job_active", reservation_id=rid,
                    voice_id="mm_active", label="active", source_job_id="job_active",
                )
                billed = await svc.check_smart_clone_reservation_active(
                    db, user_id=_USER, task_id="job_active", reservation_id=rid,
                )
                assert billed.active is False
                assert billed.reason == "already_billed"
        finally:
            await sm.kw["bind"].dispose()

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


def test_register_bill_rejects_idempotent_voice_id_mismatch():
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            rid = await _reserve(db, "job_r2_conflict")
            first = await svc.register_smart_clone_with_billing(
                db,
                user_id=_USER,
                task_id="job_r2_conflict",
                reservation_id=rid,
                voice_id="mm_v2_first",
                label="x",
                source_job_id="job_r2_conflict",
            )
            second = await svc.register_smart_clone_with_billing(
                db,
                user_id=_USER,
                task_id="job_r2_conflict",
                reservation_id=rid,
                voice_id="mm_v2_second",
                label="x",
                source_job_id="job_r2_conflict",
            )

            assert first.status == "billed"
            assert second.status == "idempotency_conflict"
            events = (await db.execute(select(CloneBillingEvent))).scalars().all()
            assert len(events) == 1 and events[0].voice_id == "mm_v2_first"
            assert (
                await db.execute(
                    select(UserVoice).where(UserVoice.voice_id == "mm_v2_second")
                )
            ).scalar_one_or_none() is None

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


# ---------------------------------------------------------------------------
# P3c finalizer：capture / release 对账（钱-正确性核心）
# ---------------------------------------------------------------------------


def test_register_bill_rechecks_library_capacity_before_billing():
    """A competing clone can fill the reserved library slot before callback."""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        try:
            async with sm() as db:
                rid = await _reserve(db, "job_r5", library_cap=30)
                for i in range(30):
                    db.add(UserVoice(
                        user_id=_USER,
                        voice_id=f"other_voice_{i}",
                        label="other",
                        provider="minimax_voice_clone",
                        tts_provider="minimax_tts",
                        platform="minimax_domestic",
                        created_from="manual_clone",
                    ))
                await db.commit()

                out = await svc.register_smart_clone_with_billing(
                    db, user_id=_USER, task_id="job_r5", reservation_id=rid,
                    voice_id="mm_v5", label="x", source_job_id="job_r5",
                )

                assert out.status == "voice_library_full"
                assert (await db.execute(select(CloneBillingEvent))).scalar_one_or_none() is None
                assert (
                    await db.execute(select(UserVoice).where(UserVoice.voice_id == "mm_v5"))
                ).scalar_one_or_none() is None
        finally:
            await sm.kw["bind"].dispose()

    _run(go())


def test_billed_reservation_does_not_double_count_next_library_reserve():
    """Registered preview clone capacity is represented by its UserVoice row."""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=2000)
        try:
            async with sm() as db:
                for i in range(29):
                    db.add(UserVoice(
                        user_id=_USER,
                        voice_id=f"existing_voice_{i}",
                        label="existing",
                        provider="minimax_voice_clone",
                        tts_provider="minimax_tts",
                        platform="minimax_domestic",
                        created_from="manual_clone",
                    ))
                await db.commit()

                rid = await _reserve(db, "job_registered", library_cap=31)
                out = await svc.register_smart_clone_with_billing(
                    db, user_id=_USER, task_id="job_registered", reservation_id=rid,
                    voice_id="mm_registered", label="registered",
                    source_job_id="job_registered", library_cap=31,
                )
                assert out.status == "billed"

                next_reserve = await svc.reserve_smart_clone_credit(
                    db, user_id=_USER, task_id="job_next", amount_credits=600,
                    ttl_minutes=30, library_cap=31,
                )

                assert next_reserve.status == "reserved"
                assert next_reserve.reservation_id is not None
        finally:
            await sm.kw["bind"].dispose()

    _run(go())


def test_settle_captures_when_billed():
    """🔥 有 chargeable event → capture 600：remaining 真扣到 200、reserved 清 0。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            rid = await _reserve(db, "job_s1")  # available 800→200, reserved 600
            await svc.register_smart_clone_with_billing(
                db, user_id=_USER, task_id="job_s1", reservation_id=rid,
                voice_id="mm_s1", label="x", source_job_id="job_s1",
            )
            out = await svc.settle_smart_clone_reservation(db, reservation_id=rid)
            assert out.status == "captured"
            rem, resv = await _bucket_remaining_reserved(db)
            assert rem == 200 and resv == 0  # 600 真扣（remaining 800→200），无悬挂 reserved
            row = (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.id == __import__("uuid").UUID(rid)))).scalar_one()
            assert row.status == "captured" and row.captured_voice_id == "mm_s1" and row.settled_at is not None
    _run(go())


def test_settle_releases_when_no_billing_event():
    """🔥 无 chargeable event（克隆没触发）→ release 600：available 复原 800。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            rid = await _reserve(db, "job_s2")  # available 800→200
            out = await svc.settle_smart_clone_reservation(db, reservation_id=rid)
            assert out.status == "released"
            rem, resv = await _bucket_remaining_reserved(db)
            assert rem == 800 and resv == 0  # 600 全退（remaining 仍 800），无悬挂 reserved
            row = (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.id == __import__("uuid").UUID(rid)))).scalar_one()
            assert row.status == "released" and row.settled_at is not None
    _run(go())


def test_settle_captures_reserved_register_failed_handoff():
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            rid = await _reserve(db, "job_s2b")
            db.add(Job(
                job_id="job_s2b",
                user_id=_USER,
                source_type="youtube_url",
                source_ref="https://youtu.be/s2b",
                title="s2b",
                speakers="auto",
                status="failed",
                smart_state={
                    "status": "downgraded_to_studio",
                    "reason": "clone_library_register_failed",
                },
            ))
            await db.commit()

            out = await svc.settle_smart_clone_reservation(db, reservation_id=rid)

            assert out.status == "captured"
            rem, resv = await _bucket_remaining_reserved(db)
            assert rem == 200 and resv == 0
            row = (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.id == __import__("uuid").UUID(rid)))).scalar_one()
            assert row.status == "captured"
            assert row.reason_code == "captured_register_failed"
            assert row.captured_voice_id is None
            assert row.settled_at is not None

    _run(go())


def test_settle_for_task_uses_smart_state_override_before_db_commit():
    """The mirror finalizer must honor merged smart_state before caller commit."""

    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            await _reserve(db, "job_s2c")

            stats = await svc.settle_smart_clone_reservations_for_task(
                db,
                task_id="job_s2c",
                smart_state_override={
                    "status": "downgraded_to_studio",
                    "reason": "clone_library_register_failed",
                },
            )

            assert stats["captured"] == 1
            rem, resv = await _bucket_remaining_reserved(db)
            assert rem == 200 and resv == 0
            row = (await db.execute(select(SmartCloneReservation))).scalar_one()
            assert row.status == "captured"
            assert row.reason_code == "captured_register_failed"
            assert row.settled_at is not None

    _run(go())


def test_settle_idempotent_no_double_capture():
    """🔥 幂等：settle 两次 → 第二次 already_settled，不双扣。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            rid = await _reserve(db, "job_s3")
            await svc.register_smart_clone_with_billing(
                db, user_id=_USER, task_id="job_s3", reservation_id=rid,
                voice_id="mm_s3", label="x", source_job_id="job_s3",
            )
            o1 = await svc.settle_smart_clone_reservation(db, reservation_id=rid)
            o2 = await svc.settle_smart_clone_reservation(db, reservation_id=rid)
            assert o1.status == "captured" and o2.status == "already_settled"
            rem, resv = await _bucket_remaining_reserved(db)
            assert rem == 200 and resv == 0  # 仍只扣一次 600，没二次扣到 -400
    _run(go())


def test_settle_expired_reservation_releases():
    """🔥 CodeX #1：expired 未结算 → finalizer release（不让 600 永久挂 reserved）。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=2000)
        now = datetime.now(timezone.utc)
        async with sm() as db:
            rid = await _reserve(db, "job_s4")  # reserved 600
            # 手动把它标 expired（模拟 TTL 过期 / inline-expire）
            row = (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.id == __import__("uuid").UUID(rid)))).scalar_one()
            row.status = "expired"
            row.expires_at = now - timedelta(hours=1)
            await db.commit()
            out = await svc.settle_smart_clone_reservation(db, reservation_id=rid)
            assert out.status == "released"
            rem, resv = await _bucket_remaining_reserved(db)
            assert rem == 2000 and resv == 0  # expired 的 600 退还、reserved 不悬挂
    _run(go())


def test_sweep_settles_stale_expired():
    """TTL sweeper 兜底：扫 reserved+过期 / expired → 逐个 settle(release)。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=2000)
        now = datetime.now(timezone.utc)
        async with sm() as db:
            rid = await _reserve(db, "job_s5")
            # 标成 reserved 但已过期（卡死非终态场景）
            row = (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.id == __import__("uuid").UUID(rid)))).scalar_one()
            row.expires_at = now - timedelta(hours=1)
            await db.commit()
            stats = await svc.sweep_settle_stale_reservations(db)
            assert stats["released"] == 1
            rem, resv = await _bucket_remaining_reserved(db)
            assert rem == 2000 and resv == 0  # 退还
    _run(go())


def test_settle_two_sequential_reservations_same_task_no_held_credit():
    """🔥 CodeX 钱-loop 审核回归：同 task 顺序两个 reservation（第一个 captured
    后第二个才能建），第二个必须 capture **自己的** 600——不能撞第一个的 capture
    entry 导致 shadow_capture 跳过、第二个 600 永久挂 reserved（per-reservation
    reason_code 修复）。两次真扣 → remaining 2000→800、reserved 0。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=2000)
        async with sm() as db:
            # 第一个 reservation：reserve → bill → capture
            rid1 = await _reserve(db, "job_multi")
            await svc.register_smart_clone_with_billing(
                db, user_id=_USER, task_id="job_multi", reservation_id=rid1,
                voice_id="mm_m1", label="x", source_job_id="job_multi",
            )
            o1 = await svc.settle_smart_clone_reservation(db, reservation_id=rid1)
            assert o1.status == "captured"
            # 第二个 reservation（同 task，第一个已 captured → partial unique 放行）
            rid2 = await _reserve(db, "job_multi")
            assert rid2 != rid1
            await svc.register_smart_clone_with_billing(
                db, user_id=_USER, task_id="job_multi", reservation_id=rid2,
                voice_id="mm_m2", label="x", source_job_id="job_multi",
            )
            o2 = await svc.settle_smart_clone_reservation(db, reservation_id=rid2)
            assert o2.status == "captured"  # 不是 already_settled / settlement_failed
            rem, resv = await _bucket_remaining_reserved(db)
            # 两个 600 都真扣：2000 - 600 - 600 = 800，无悬挂 reserved
            assert rem == 800 and resv == 0
    _run(go())


# ---------------------------------------------------------------------------
# P3c by-task finalizer 入口（job_terminal_mirror 单一入口，plan v3 §4）
# ---------------------------------------------------------------------------


def test_settle_for_task_releases_unbilled():
    """🔥 by-task：reserved 无 chargeable event（克隆没触发）→ release 600。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            await _reserve(db, "job_t1")  # available 800→200
            stats = await svc.settle_smart_clone_reservations_for_task(db, task_id="job_t1")
            assert stats["released"] == 1 and stats["captured"] == 0
            rem, resv = await _bucket_remaining_reserved(db)
            assert rem == 800 and resv == 0  # 全退、无悬挂
    _run(go())


def test_settle_for_task_captures_billed():
    """🔥 by-task：有 chargeable event → capture 600。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            rid = await _reserve(db, "job_t2")
            await svc.register_smart_clone_with_billing(
                db, user_id=_USER, task_id="job_t2", reservation_id=rid,
                voice_id="mm_t2", label="x", source_job_id="job_t2",
            )
            stats = await svc.settle_smart_clone_reservations_for_task(db, task_id="job_t2")
            assert stats["captured"] == 1 and stats["released"] == 0
            rem, resv = await _bucket_remaining_reserved(db)
            assert rem == 200 and resv == 0  # 真扣 600
    _run(go())


def test_settle_for_task_no_reservation_is_noop():
    """by-task：该 task 无 reservation → no-op（空计数），不报错、不动钱。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            stats = await svc.settle_smart_clone_reservations_for_task(db, task_id="job_none")
            assert stats == {"captured": 0, "released": 0, "settlement_failed": 0, "other": 0}
            assert await _bucket_available(db) == 800
    _run(go())


def test_settle_for_task_idempotent_already_settled():
    """🔥 by-task 幂等：mirror level-triggered 反复对同一终态 job 调用——
    第二次该 task 已无 active reservation（已 released）→ no-op，不双退/双扣。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            await _reserve(db, "job_t3")
            s1 = await svc.settle_smart_clone_reservations_for_task(db, task_id="job_t3")
            s2 = await svc.settle_smart_clone_reservations_for_task(db, task_id="job_t3")
            assert s1["released"] == 1
            # 第二次已无 active(reserved/expired) reservation → 不再选中 → 空
            assert s2 == {"captured": 0, "released": 0, "settlement_failed": 0, "other": 0}
            rem, resv = await _bucket_remaining_reserved(db)
            assert rem == 800 and resv == 0  # 仍只退一次
    _run(go())
