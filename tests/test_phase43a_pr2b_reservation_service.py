"""Phase 4.3a PR2-B — express_reservation_service sqlite 行为测试。

覆盖 reservation service 状态机 / 幂等 / 计数 / inline-expire / unknown-user
（spec §4 + §5）。**并发原子性**（users row FOR UPDATE 阻塞）sqlite 测不了，
留 §10.7 真 PG（PR2-C-pg）。

真 in-memory aiosqlite：建 users（minimal）+ user_voices + express_clone_reservations
三表（reserve 要 SELECT users + count user_voices）。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


from models import ExpressCloneReservation, UserVoice  # noqa: E402
import express_reservation_service as svc  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_UNKNOWN_USER = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

# minimal users stub 表（reserve 要 SELECT users.id FOR UPDATE）。
# 关键：id 列用 PG_UUID(as_uuid=True)（与 models.User.id 同类型），这样
# insert / query 经同一 bind processor，sqlite 下 UUID 绑定格式一致——
# 否则 raw CHAR(36) + str(uuid) 与 select(User.id) 的 PG_UUID 绑定格式
# 不匹配，已知 user 会被误判 user_not_found。
_users_md = MetaData()
_users_stub = Table(
    "users",
    _users_md,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
)


async def _make_session() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda s: _users_stub.create(s))
        await conn.run_sync(lambda s: UserVoice.__table__.create(s))
        await conn.run_sync(lambda s: ExpressCloneReservation.__table__.create(s))
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # 插已知 user（经 PG_UUID processor，与 reserve 的 select(User.id) 一致）
    async with sm() as db:
        await db.execute(_users_stub.insert().values(id=_USER))
        await db.commit()
    return sm


def _reserve_kwargs(**overrides):
    base = dict(
        user_id=_USER, job_id="job_b2", speaker_id="speaker_a",
        target_model="cosyvoice-v3.5-flash", ttl_minutes=30,
        daily_cap=5, active_temp_cap=3,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# reserve: 基本 + 幂等 + unknown user + cap
# ---------------------------------------------------------------------------


def test_reserve_basic_success():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs())
        assert out.status == "reserved"
        assert out.reservation_id is not None
        assert out.expires_at is not None
        assert out.idempotent_hit is False
    _run(_t())


def test_reserve_idempotent_same_key():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            out1 = await svc.reserve(db, **_reserve_kwargs())
        async with sm() as db:
            out2 = await svc.reserve(db, **_reserve_kwargs())
        assert out1.reservation_id == out2.reservation_id, "同 key 应幂等返回同 reservation"
        assert out2.idempotent_hit is True
    _run(_t())


def test_reserve_unknown_user_no_insert():
    """spec §4.1：unknown user → user_not_found fail-closed + 不插 reservation。"""
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs(user_id=_UNKNOWN_USER))
        assert out.status == "user_not_found"
        assert out.reservation_id is None
        # 表里无该 user 的 reservation
        async with sm() as db:
            cnt = await svc.count_active_reservations(db, _UNKNOWN_USER)
        assert cnt == 0, "unknown user 不应插入任何 reservation"
    _run(_t())


def test_reserve_daily_cap_exceeded():
    async def _t():
        sm = await _make_session()
        # daily_cap=2；先占 2 个不同 speaker（绕过幂等）
        async with sm() as db:
            await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_a", daily_cap=2))
        async with sm() as db:
            await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_b", daily_cap=2))
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_c", daily_cap=2))
        assert out.status == "denied"
        assert out.deny_reason == "daily_cap_exceeded"
    _run(_t())


def test_reserve_active_temp_cap_exceeded():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_a", active_temp_cap=1, daily_cap=99))
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_b", active_temp_cap=1, daily_cap=99))
        assert out.status == "denied"
        assert out.deny_reason == "active_temp_cap_exceeded"
    _run(_t())


# ---------------------------------------------------------------------------
# inline expire stale（spec §4.1 step 2）
# ---------------------------------------------------------------------------


async def _insert_stale_reserved(sm, *, speaker_id: str, user_id=_USER) -> str:
    """直接插一条已过期的 reserved（绕过 reserve 的 ttl）。"""
    rid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with sm() as db:
        db.add(ExpressCloneReservation(
            id=rid, user_id=user_id, job_id="job_stale", speaker_id=speaker_id,
            status="reserved", target_model="cosyvoice-v3.5-flash",
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),  # 已过期
        ))
        await db.commit()
    return str(rid)


def test_reserve_inline_expires_stale_before_count():
    """cap 已被 stale 占满，reserve 应先 expire stale 腾名额，新 reserve 成功
    （不依赖 sweeper）。"""
    async def _t():
        sm = await _make_session()
        # 插 1 条 stale reserved（占 active_temp_cap=1）
        await _insert_stale_reserved(sm, speaker_id="speaker_old")
        # 新 reserve（cap=1）：若不 inline expire，会 active_temp_cap_exceeded
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_new", active_temp_cap=1, daily_cap=99))
        assert out.status == "reserved", (
            "stale 应被 inline expire 腾出名额，新 reserve 成功（不依赖 sweeper）"
        )
    _run(_t())


def test_reserve_stale_same_key_not_idempotent_reused():
    """同 (user,job,speaker) 的 stale reserved → reserve 不幂等返回旧 stale，
    而是 expire 旧的 + 新建（新 reservation_id ≠ 旧）。"""
    async def _t():
        sm = await _make_session()
        # stale 用 job_stale + speaker_x
        old_rid = uuid.uuid4()
        now = datetime.now(timezone.utc)
        async with sm() as db:
            db.add(ExpressCloneReservation(
                id=old_rid, user_id=_USER, job_id="job_x", speaker_id="speaker_x",
                status="reserved", target_model="cosyvoice-v3.5-flash",
                created_at=now - timedelta(hours=2), updated_at=now - timedelta(hours=2),
                expires_at=now - timedelta(hours=1),
            ))
            await db.commit()
        # reserve 同 key
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs(job_id="job_x", speaker_id="speaker_x"))
        assert out.status == "reserved"
        assert out.idempotent_hit is False, "stale 不应被幂等命中"
        assert out.reservation_id != str(old_rid), "应新建一条，不复用 stale"
    _run(_t())


# ---------------------------------------------------------------------------
# consume / release 状态机幂等
# ---------------------------------------------------------------------------


def test_consume_reserved_to_consumed():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs())
        async with sm() as db:
            t = await svc.consume(db, reservation_id=out.reservation_id, voice_id="cosyvoice-v3.5-flash-x")
        assert t.ok is True and t.status == "consumed"
    _run(_t())


def test_consume_idempotent_same_voice():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs())
        async with sm() as db:
            await svc.consume(db, reservation_id=out.reservation_id, voice_id="v1")
        async with sm() as db:
            t = await svc.consume(db, reservation_id=out.reservation_id, voice_id="v1")
        assert t.ok is True and t.status == "consumed", "同 voice_id 二次 consume 幂等"
    _run(_t())


def test_consume_different_voice_conflict():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs())
        async with sm() as db:
            await svc.consume(db, reservation_id=out.reservation_id, voice_id="v1")
        async with sm() as db:
            t = await svc.consume(db, reservation_id=out.reservation_id, voice_id="v2")
        assert t.ok is False and t.conflict_reason == "already_consumed_different_voice"
    _run(_t())


def test_release_reserved_to_released():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs())
        async with sm() as db:
            t = await svc.release(db, reservation_id=out.reservation_id, reason="worker_failed")
        assert t.ok is True and t.status == "released"
    _run(_t())


def test_release_idempotent():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs())
        async with sm() as db:
            await svc.release(db, reservation_id=out.reservation_id, reason="x")
        async with sm() as db:
            t = await svc.release(db, reservation_id=out.reservation_id, reason="x")
        assert t.ok is True, "已 released 二次 release 幂等 ok"
    _run(_t())


def test_release_already_consumed_conflict():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs())
        async with sm() as db:
            await svc.consume(db, reservation_id=out.reservation_id, voice_id="v1")
        async with sm() as db:
            t = await svc.release(db, reservation_id=out.reservation_id, reason="x")
        assert t.ok is False and t.conflict_reason == "reservation_already_consumed"
    _run(_t())


def test_consume_after_release_conflict():
    """release 后 consume → reservation_not_reservable（TTL/release 已回收）。"""
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            out = await svc.reserve(db, **_reserve_kwargs())
        async with sm() as db:
            await svc.release(db, reservation_id=out.reservation_id, reason="x")
        async with sm() as db:
            t = await svc.consume(db, reservation_id=out.reservation_id, voice_id="v1")
        assert t.ok is False and t.conflict_reason == "reservation_not_reservable"
    _run(_t())


def test_consume_missing_reservation():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            t = await svc.consume(db, reservation_id=str(uuid.uuid4()), voice_id="v1")
        assert t.ok is False and t.conflict_reason == "reservation_not_found"
    _run(_t())


# ---------------------------------------------------------------------------
# count（含 active reservations）+ consume 转 user_voices 不双算
# ---------------------------------------------------------------------------


def test_count_active_reservations_only_reserved():
    async def _t():
        sm = await _make_session()
        async with sm() as db:
            r1 = await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_a"))
        async with sm() as db:
            await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_b"))
        async with sm() as db:
            n = await svc.count_active_reservations(db, _USER)
        assert n == 2
        # consume 一个 → active 减到 1
        async with sm() as db:
            await svc.consume(db, reservation_id=r1.reservation_id, voice_id="v1")
        async with sm() as db:
            n2 = await svc.count_active_reservations(db, _USER)
        assert n2 == 1, "consumed 不再计入 active reservations（避免与 user_voices 双算）"
    _run(_t())


# ---------------------------------------------------------------------------
# sweeper expire_stale_reservations
# ---------------------------------------------------------------------------


def test_sweeper_expires_stale_across_users():
    async def _t():
        sm = await _make_session()
        await _insert_stale_reserved(sm, speaker_id="speaker_s1")
        await _insert_stale_reserved(sm, speaker_id="speaker_s2")
        async with sm() as db:
            n = await svc.expire_stale_reservations(db)
        assert n == 2
        # 再扫一次幂等：无 stale 可处理
        async with sm() as db:
            n2 = await svc.expire_stale_reservations(db)
        assert n2 == 0, "sweeper 幂等：已 expired 不重复处理"
    _run(_t())


def test_sweeper_does_not_touch_consumed_or_active():
    async def _t():
        sm = await _make_session()
        # 1 个 active（未过期）+ 1 个 consumed + 1 个 stale
        async with sm() as db:
            active = await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_active"))
        async with sm() as db:
            consumed = await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_consumed"))
        async with sm() as db:
            await svc.consume(db, reservation_id=consumed.reservation_id, voice_id="v1")
        await _insert_stale_reserved(sm, speaker_id="speaker_stale")
        async with sm() as db:
            n = await svc.expire_stale_reservations(db)
        assert n == 1, "只 expire stale，不动 active / consumed"
        # active 仍 reserved
        async with sm() as db:
            cnt = await svc.count_active_reservations(db, _USER)
        assert cnt == 1, "active（未过期）reservation 不被 sweeper 动"
    _run(_t())


# ---------------------------------------------------------------------------
# service 不静默吞异常（Codex B 边界）
# ---------------------------------------------------------------------------


def test_service_does_not_swallow_exceptions_ast():
    """守卫：consume / release 不出现裸 ``except: pass`` 静默吞异常
    （release 失败 audit 在 pipeline wrapper，service 抛真实异常）。"""
    import ast
    src = (Path(_gateway_dir) / "express_reservation_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            # 不允许 except 体只有 pass（静默吞）
            body = node.body
            only_pass = len(body) == 1 and isinstance(body[0], ast.Pass)
            assert not only_pass, (
                "express_reservation_service 不应有静默吞异常的 except: pass"
            )
