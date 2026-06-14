"""P3c — Smart 克隆 reservation TTL 结算 sweeper 测试（钱-正确性兜底）.

plan v3 §4 / CodeX P3c-审核 P1。covers:
- sweep_once 结算过期未结算 reservation（无 event → release 退还；有 → capture）；
- sweeper_loop 单 tick 异常不崩 loop（续命）；
- AST 守卫：sweeper 模块不 import 付费 / HTTP client（settle 只走内部信用 ledger）。

真 async SQLite(aiosqlite) 测结算路径，同 test_p3a harness。**不** stub
sys.modules["database"]（见 memory feedback_test_database_stub_convention）。
"""
from __future__ import annotations

import ast
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Column, MetaData, Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


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
import smart_clone_reservation_sweeper as sweeper  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
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
        db.add(CreditsBucket(
            id=uuid.uuid4(), user_id=_USER, bucket_type="free",
            granted=bucket_remaining, remaining=bucket_remaining, reserved=0,
        ))
        await db.commit()
    return sm


async def _available(sm) -> int:
    async with sm() as db:
        b = (await db.execute(select(CreditsBucket).where(CreditsBucket.user_id == _USER))).scalar_one()
        return b.remaining - b.reserved


# ---------------------------------------------------------------------------
# sweep_once 功能：release / capture stale reservation
# ---------------------------------------------------------------------------


def test_sweep_once_releases_stale_unbilled():
    """🔥 过期 reserved 无 billing event → sweeper release 600（退还）。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="job_sw1", amount_credits=600,
                ttl_minutes=30, library_cap=10,
            )
            assert o.status == "reserved"
            # 标过期（卡死非终态场景）
            row = (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.id == uuid.UUID(o.reservation_id)))).scalar_one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            await db.commit()
        assert await _available(sm) == 200  # 仍挂 reserved 600
        stats = await sweeper.sweep_once(session_factory=sm)
        assert stats["released"] == 1 and stats["captured"] == 0
        assert await _available(sm) == 800  # 退还
    _run(go())


def test_sweep_once_captures_stale_billed():
    """过期但有 chargeable event（克隆已成、任务卡死）→ sweeper capture 600。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            o = await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="job_sw2", amount_credits=600,
                ttl_minutes=30, library_cap=10,
            )
            await svc.register_smart_clone_with_billing(
                db, user_id=_USER, task_id="job_sw2", reservation_id=o.reservation_id,
                voice_id="mm_sw2", label="x", source_job_id="job_sw2",
            )
            row = (await db.execute(select(SmartCloneReservation).where(
                SmartCloneReservation.id == uuid.UUID(o.reservation_id)))).scalar_one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            await db.commit()
        stats = await sweeper.sweep_once(session_factory=sm)
        assert stats["captured"] == 1 and stats["released"] == 0
        assert await _available(sm) == 200  # 真扣 600
    _run(go())


def test_sweep_once_noop_when_nothing_stale():
    """无过期 reservation → sweeper no-op（不动钱）。"""
    async def go():
        sm = await _make_sessionmaker(bucket_remaining=800)
        async with sm() as db:
            # 未过期 reserved（仍在 TTL 内）不应被 sweeper 选中
            await svc.reserve_smart_clone_credit(
                db, user_id=_USER, task_id="job_sw3", amount_credits=600,
                ttl_minutes=30, library_cap=10,
            )
        stats = await sweeper.sweep_once(session_factory=sm)
        assert stats == {"captured": 0, "released": 0, "settlement_failed": 0, "other": 0}
        assert await _available(sm) == 200  # 仍正常挂 reserved，未被误退
    _run(go())


# ---------------------------------------------------------------------------
# sweeper_loop 续命：单 tick 异常不崩
# ---------------------------------------------------------------------------


def test_sweeper_loop_survives_failing_tick():
    """单次 sweep 异常只 log，loop 续到下一周期；stop_event 干净退出。"""
    calls = {"n": 0}

    async def _boom_then_ok(*, session_factory=None):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB blip")
        return {"captured": 0, "released": 0, "settlement_failed": 0, "other": 0}

    async def go():
        stop = asyncio.Event()
        orig_once = sweeper.sweep_once
        orig_init, orig_interval = sweeper.INITIAL_DELAY_S, sweeper.SWEEP_INTERVAL_S
        sweeper.sweep_once = _boom_then_ok
        sweeper.INITIAL_DELAY_S = 0
        sweeper.SWEEP_INTERVAL_S = 0
        try:
            task = asyncio.create_task(sweeper.sweeper_loop(stop_event=stop))
            # 让它跑几轮（第一轮抛、后续正常）
            for _ in range(5):
                await asyncio.sleep(0)
            stop.set()
            await asyncio.wait_for(task, timeout=2)
        finally:
            sweeper.sweep_once = orig_once
            sweeper.INITIAL_DELAY_S = orig_init
            sweeper.SWEEP_INTERVAL_S = orig_interval
        assert calls["n"] >= 2  # 第一轮抛后仍续命跑了至少一轮
    _run(go())


# ---------------------------------------------------------------------------
# AST 守卫：sweeper 不 import 付费 / HTTP client
# ---------------------------------------------------------------------------


def _imported_modules(src: str) -> set[str]:
    tree = ast.parse(src)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_sweeper_no_paid_or_http_imports():
    """settle 走内部信用 ledger（shadow_capture/release），sweeper 模块**不**
    import 任何付费 / 外部 API client——防未来重构把付费动作塞进 sweeper。"""
    src = (_GATEWAY / "smart_clone_reservation_sweeper.py").read_text(encoding="utf-8")
    mods = _imported_modules(src)
    forbidden = (
        "mainland_worker", "minimax", "dashscope", "httpx", "requests",
        "boto3", "voice_clone", "cosyvoice_clone", "sample_upload",
    )
    for m in mods:
        low = m.lower()
        for bad in forbidden:
            assert bad not in low, (
                f"smart_clone_reservation_sweeper 不应 import {m!r}（命中 {bad!r}）"
                f"——sweeper 只允许内部 DB / 信用结算，绝不调付费 / 外部 API"
            )


def test_sweeper_only_calls_sweep_settle_from_service():
    """sweeper 对 service 的调用面只允许 sweep_settle_stale_reservations
    （不直接调 reserve / register / 单条 settle）。"""
    src = (_GATEWAY / "smart_clone_reservation_sweeper.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    accessed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "_svc":
                accessed.add(node.attr)
    assert accessed <= {"sweep_settle_stale_reservations"}, (
        f"sweeper 只应调 sweep_settle_stale_reservations，实际访问了 {accessed}"
    )


def test_main_starts_smart_clone_sweeper():
    """main.py lifespan 必须 import 并 create_task 启动本 sweeper（CodeX P3c
    审核 P1：sweep_settle_stale_reservations 需有生产调用点，否则兜底名存实亡）。
    包 try/except 同其它 sweeper 的 fail-safe 模式。"""
    src = (_GATEWAY / "main.py").read_text(encoding="utf-8")
    assert "from smart_clone_reservation_sweeper import sweeper_loop" in src
    assert "smart-clone-reservation-sweeper" in src  # create_task name=
    # 接线包在 try/except（启动失败不阻断 gateway）——校验同一块里有 except
    tree = ast.parse(src)
    wired_in_try = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            block = ast.get_source_segment(src, node) or ""
            if "smart_clone_reservation_sweeper" in block:
                wired_in_try = True
                break
    assert wired_in_try, "sweeper 启动必须包 try/except（fail-safe，不阻断 gateway 启动）"
