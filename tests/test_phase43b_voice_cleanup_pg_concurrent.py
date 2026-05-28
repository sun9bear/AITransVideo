"""Phase 4.3b-B-pg — temporary-voice cleanup claim 并发原子性（真 PostgreSQL）。

spec §2.7：claim 用 ``SELECT … FOR UPDATE SKIP LOCKED`` 原子认领一批，防多
runner（auto sweeper + manual cleanup / 多 gateway 实例）重复选中同一行 →
重复付费 ``delete_voice``。sqlite（aiosqlite）**不支持** SKIP LOCKED（被忽略），
用 sqlite 测并发是假绿。本文件用真 PG 测两件事：

1. 2 个并发 ``claim_batch`` 同一批到期音色 → **无**任何 voice 被两边同时认领
   （claimed 集合不相交）
2. limit < N 时两并发认领**干净切分**：并集 = 全部 eligible，无丢失、无重复

本测试**只认领，不调 worker**（claim_batch 是纯 DB 认领；worker delete 在
事务外，B 已用 mock 覆盖）。本地无 PG 时整文件 skip（``AVT_TEST_PG_DSN`` 未设）；
CI 的 ``backend-pg-integration`` job 起 postgres service 才真跑。
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))

_PG_DSN = os.environ.get("AVT_TEST_PG_DSN", "").strip()

_SKIP_NO_PG = pytest.mark.skipif(
    not _PG_DSN,
    reason="AVT_TEST_PG_DSN unset — PG concurrency test runs only in CI "
    "backend-pg-integration job (sqlite can't test FOR UPDATE SKIP LOCKED)",
)

# 允许 DROP TABLE 的 host 白名单（本地 / CI service container alias）。
_SAFE_PG_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})


class UnsafeTestDsnError(RuntimeError):
    """DSN 指向疑似非测试库 —— 拒绝执行 DROP TABLE。"""


def _assert_safe_test_dsn(dsn: str) -> None:
    """fail-closed：本测试 DROP TABLE user_voices / users。误把 DSN 指到非测试库
    会删核心表。强制：database name 含 'test' + host 在白名单内。任一不满足 →
    raise，**绝不** DROP。"""
    from urllib.parse import urlparse

    parsed = urlparse(dsn)
    host = (parsed.hostname or "").lower()
    db = (parsed.path or "").lstrip("/").lower()
    if "test" not in db:
        raise UnsafeTestDsnError(
            f"refusing to DROP TABLE: DB name {db!r} must contain 'test' "
            f"(AVT_TEST_PG_DSN looks like a non-test database)"
        )
    if host not in _SAFE_PG_HOSTS:
        raise UnsafeTestDsnError(
            f"refusing to DROP TABLE: host {host!r} not in {sorted(_SAFE_PG_HOSTS)} "
            f"(AVT_TEST_PG_DSN must point at a local / CI test database)"
        )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000ca")


async def _setup_engine():
    """建临时表（users stub + user_voices），插 test user。返回 (engine, sm)。"""
    from sqlalchemy import Column, MetaData, Table
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from models import UserVoice

    # fail-closed：DROP TABLE 前强制确认 DSN 指向测试库
    _assert_safe_test_dsn(_PG_DSN)

    engine = create_async_engine(_PG_DSN, future=True)

    md = MetaData()
    users_stub = Table("users", md, Column("id", PG_UUID(as_uuid=True), primary_key=True))

    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE IF EXISTS user_voices CASCADE")
        await conn.exec_driver_sql("DROP TABLE IF EXISTS users CASCADE")
        await conn.run_sync(users_stub.create)
        await conn.run_sync(UserVoice.__table__.create)

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as db:
        await db.execute(users_stub.insert().values(id=_USER))
        await db.commit()
    return engine, sm


async def _teardown(engine):
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE IF EXISTS user_voices CASCADE")
        await conn.exec_driver_sql("DROP TABLE IF EXISTS users CASCADE")
    await engine.dispose()


async def _insert_expired_temp_voices(sm, n: int) -> set[str]:
    """插 n 条到期、可清理的临时 cosyvoice 音色，返回它们的 voice_id 集合。"""
    from models import UserVoice

    now = datetime.now(timezone.utc)
    voice_ids: set[str] = set()
    async with sm() as db:
        for i in range(n):
            vid = f"cosyvoice-v3.5-flash-{uuid.uuid4().hex[:10]}"
            voice_ids.add(vid)
            db.add(UserVoice(
                id=uuid.uuid4(),
                user_id=_USER,
                voice_id=vid,
                provider="cosyvoice_voice_clone",
                label=f"express-clone-{i}",
                region_constraint="mainland_only",
                is_temporary=True,
                requires_worker=True,
                target_model="cosyvoice-v3.5-flash",
                temporary_expires_at=now - timedelta(hours=1),
                expired_at=None,
                cleanup_attempts=0,
            ))
        await db.commit()
    return voice_ids


async def _claim(sm, run_id: str, limit: int):
    import express_voice_cleanup_service as svc
    async with sm() as db:
        claimed = await svc.claim_batch(db, run_id=run_id, limit=limit)
    return {c.voice_id for c in claimed}


@_SKIP_NO_PG
def test_pg_concurrent_claim_no_double_select():
    """2 并发 claim_batch（limit=10, 10 行）→ 任何 voice 不被两边同时认领。"""
    async def _t():
        engine, sm = await _setup_engine()
        try:
            all_ids = await _insert_expired_temp_voices(sm, 10)
            a, b = await asyncio.gather(_claim(sm, "run-A", 10), _claim(sm, "run-B", 10))
            assert a.isdisjoint(b), (
                f"FOR UPDATE SKIP LOCKED 失效：{a & b} 被两个 runner 同时认领（会重复付费删除）"
            )
            assert (a | b) <= all_ids
            # 认领总数不超过总行数（无重复认领）
            assert len(a) + len(b) == len(a | b) <= 10
        finally:
            await _teardown(engine)
    _run(_t())


@_SKIP_NO_PG
def test_pg_concurrent_claim_partitions_all():
    """limit=5, 10 行, 2 并发 → 干净切分：并集 = 全部 10，不相交，无丢失。"""
    async def _t():
        engine, sm = await _setup_engine()
        try:
            all_ids = await _insert_expired_temp_voices(sm, 10)
            a, b = await asyncio.gather(_claim(sm, "run-A", 5), _claim(sm, "run-B", 5))
            assert a.isdisjoint(b), f"重复认领: {a & b}"
            assert (a | b) == all_ids, (
                f"SKIP LOCKED 切分应覆盖全部 eligible，实际 {len(a | b)}/10"
            )
            assert len(a) <= 5 and len(b) <= 5
        finally:
            await _teardown(engine)
    _run(_t())


# ---------------------------------------------------------------------------
# DSN 安全 guard 单元测（always run，不依赖 PG）
# ---------------------------------------------------------------------------


def test_dsn_guard_rejects_non_test_db():
    with pytest.raises(UnsafeTestDsnError):
        _assert_safe_test_dsn("postgresql+asyncpg://avt:pw@localhost:5432/aivideotrans")
    with pytest.raises(UnsafeTestDsnError):
        _assert_safe_test_dsn("postgresql+asyncpg://avt:pw@localhost:5432/production")


def test_dsn_guard_rejects_remote_host():
    with pytest.raises(UnsafeTestDsnError):
        _assert_safe_test_dsn("postgresql+asyncpg://avt:pw@db.prod.example.com:5432/aivideotrans_test")
    with pytest.raises(UnsafeTestDsnError):
        _assert_safe_test_dsn("postgresql+asyncpg://avt:pw@10.0.0.5:5432/test_db")


def test_dsn_guard_accepts_ci_and_local_test_dsn():
    _assert_safe_test_dsn("postgresql+asyncpg://avt:avt_test@localhost:5432/aivideotrans_test")
    _assert_safe_test_dsn("postgresql+asyncpg://avt:avt_test@postgres:5432/aivideotrans_test")
    _assert_safe_test_dsn("postgresql+asyncpg://avt:pw@127.0.0.1:5432/my_test_db")


def test_setup_engine_calls_dsn_guard_before_drop():
    """守卫：_setup_engine 在任何 DROP TABLE 之前调 _assert_safe_test_dsn（静态扫）。"""
    src = Path(__file__).read_text(encoding="utf-8")
    guard_call = src.find("_assert_safe_test_dsn(_PG_DSN)")
    first_drop = src.find("DROP TABLE IF EXISTS")
    assert guard_call != -1, "_setup_engine 必须调 _assert_safe_test_dsn(_PG_DSN)"
    assert first_drop != -1
    assert guard_call < first_drop, "DSN 安全 guard 必须在第一个 DROP TABLE 之前（fail-closed）"
