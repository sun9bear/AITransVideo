"""Phase 4.3a PR2-C-pg — reservation 并发原子性（真 PostgreSQL）。

spec §10.7：reservation 的核心是 users row ``FOR UPDATE`` 串行化并发 reserve。
sqlite（aiosqlite）**不支持** FOR UPDATE 阻塞语义，用 sqlite 测并发是假绿。
本文件用真 PG 测两件事：

1. cap=1 + N 并发**不同 speaker**（绕过幂等，测真 cap 竞态）→ 恰好 1 个
   reserved，其余 active_temp_cap_exceeded
2. N 并发**同 (user,job,speaker)** → 全部返回**同一** reservation_id（幂等 +
   partial unique 在并发下不重复建）

本地无 PG 时整个文件 skip（``AVT_TEST_PG_DSN`` 未设）；CI 的
``backend-pg-integration`` job 起 postgres service 才真跑。

**不接 pipeline，不启 sweeper**（Codex C-pg 边界）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))

_PG_DSN = os.environ.get("AVT_TEST_PG_DSN", "").strip()

pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="AVT_TEST_PG_DSN unset — PG concurrency test runs only in CI "
    "backend-pg-integration job (sqlite can't test FOR UPDATE blocking)",
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000c9")


async def _setup_engine():
    """建临时表（users stub + user_voices + express_clone_reservations），
    插 test user。返回 (engine, session_maker)。测完调 _teardown。"""
    from sqlalchemy import Column, MetaData, Table
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from models import ExpressCloneReservation, UserVoice

    engine = create_async_engine(_PG_DSN, future=True)

    # users stub（只 id；真 users 表列多，PG 测试自建最小表）
    md = MetaData()
    users_stub = Table("users", md, Column("id", PG_UUID(as_uuid=True), primary_key=True))

    async with engine.begin() as conn:
        # 幂等清理上轮残留
        await conn.exec_driver_sql("DROP TABLE IF EXISTS express_clone_reservations CASCADE")
        await conn.exec_driver_sql("DROP TABLE IF EXISTS user_voices CASCADE")
        await conn.exec_driver_sql("DROP TABLE IF EXISTS users CASCADE")
        await conn.run_sync(users_stub.create)
        await conn.run_sync(UserVoice.__table__.create)
        await conn.run_sync(ExpressCloneReservation.__table__.create)

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as db:
        await db.execute(users_stub.insert().values(id=_USER))
        await db.commit()
    return engine, sm


async def _teardown(engine):
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE IF EXISTS express_clone_reservations CASCADE")
        await conn.exec_driver_sql("DROP TABLE IF EXISTS user_voices CASCADE")
        await conn.exec_driver_sql("DROP TABLE IF EXISTS users CASCADE")
    await engine.dispose()


def test_pg_concurrent_reserve_cap_one_only_one_wins():
    """cap=1 + 10 并发不同 speaker → 恰好 1 reserved，9 active_temp_cap_exceeded。"""
    async def _t():
        import express_reservation_service as svc
        engine, sm = await _setup_engine()
        try:
            async def _one(i):
                async with sm() as db:
                    return await svc.reserve(
                        db, user_id=_USER, job_id="job_pg",
                        speaker_id=f"speaker_{i}",  # 不同 speaker，绕过幂等
                        target_model="cosyvoice-v3.5-flash",
                        ttl_minutes=30, daily_cap=99, active_temp_cap=1,
                    )
            outcomes = await asyncio.gather(*[_one(i) for i in range(10)])
            reserved = [o for o in outcomes if o.status == "reserved"]
            denied = [o for o in outcomes if o.status == "denied"]
            assert len(reserved) == 1, (
                f"cap=1 并发应恰好 1 个 reserved，实际 {len(reserved)}（并发未串行化？）"
            )
            assert len(denied) == 9
            assert all(o.deny_reason == "active_temp_cap_exceeded" for o in denied)
        finally:
            await _teardown(engine)
    _run(_t())


def test_pg_concurrent_reserve_same_key_idempotent():
    """10 并发同 (user,job,speaker) → 全部同一 reservation_id，表里只 1 active row。"""
    async def _t():
        import express_reservation_service as svc
        engine, sm = await _setup_engine()
        try:
            async def _one():
                async with sm() as db:
                    return await svc.reserve(
                        db, user_id=_USER, job_id="job_same", speaker_id="speaker_a",
                        target_model="cosyvoice-v3.5-flash",
                        ttl_minutes=30, daily_cap=99, active_temp_cap=99,
                    )
            outcomes = await asyncio.gather(*[_one() for _ in range(10)])
            assert all(o.status == "reserved" for o in outcomes), "同 key 并发应全 reserved（幂等）"
            ids = {o.reservation_id for o in outcomes}
            assert len(ids) == 1, (
                f"同 key 并发应返回同一 reservation_id，实际 {len(ids)} 个不同 id"
            )
            # 表里只 1 active row
            async with sm() as db:
                n = await svc.count_active_reservations(db, _USER)
            assert n == 1, f"同 key 并发应只建 1 active row，实际 {n}"
        finally:
            await _teardown(engine)
    _run(_t())
