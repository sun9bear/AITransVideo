"""Phase 4.3a PR2-D — Express reservation TTL sweeper 测试。

覆盖 spec §8 + §10.4：

- ``sweep_once`` 把 stale reserved 翻 ``expired`` + ``released_reason='ttl_expired'``
- 不动 active(未过期 reserved) / consumed / released
- 幂等（重跑跳过已 expired）
- ``sweeper_loop`` 单次 sweep 异常**不崩 loop**（续到下一周期）+ stop_event 干净退出
- AST 守卫：sweeper 模块**不** import mainland worker / sample uploader /
  register-smart client / httpx / requests / boto3（不调任何付费 / 外部 API）
- lifespan wiring 确认存在（静态扫 main.py：create_task + app.state stash +
  try/except fail-safe + shutdown cancel），**不**真起无限后台循环

真 in-memory aiosqlite（与 PR2-B 同 harness）；``sweep_once`` 走注入的
``session_factory``，不碰生产 ``database.async_session``。
"""
from __future__ import annotations

import ast
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Column, MetaData, Table, select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

_GATEWAY_DIR = Path(__file__).resolve().parent.parent / "gateway"
if str(_GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(_GATEWAY_DIR))


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


from models import ExpressCloneReservation, UserVoice  # noqa: E402
import express_reservation_service as svc  # noqa: E402
import express_reservation_sweeper as sweeper  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000d4")

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
    async with sm() as db:
        await db.execute(_users_stub.insert().values(id=_USER))
        await db.commit()
    return sm


def _reserve_kwargs(**overrides):
    base = dict(
        user_id=_USER, job_id="job_d4", speaker_id="speaker_a",
        target_model="cosyvoice-v3.5-flash", ttl_minutes=30,
        daily_cap=99, active_temp_cap=99,
    )
    base.update(overrides)
    return base


async def _insert_stale_reserved(sm, *, speaker_id: str, job_id: str = "job_stale") -> str:
    """插一条已过期的 reserved（expires_at 在过去）。"""
    rid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with sm() as db:
        db.add(ExpressCloneReservation(
            id=rid, user_id=_USER, job_id=job_id, speaker_id=speaker_id,
            status="reserved", target_model="cosyvoice-v3.5-flash",
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        ))
        await db.commit()
    return str(rid)


async def _fetch_row(sm, rid: str) -> ExpressCloneReservation | None:
    async with sm() as db:
        return (
            await db.execute(
                select(ExpressCloneReservation).where(
                    ExpressCloneReservation.id == uuid.UUID(str(rid))
                )
            )
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# sweep_once 行为
# ---------------------------------------------------------------------------


def test_sweep_once_expires_stale_sets_ttl_expired():
    """stale reserved → expired + released_reason='ttl_expired'（spec §8）。"""
    async def _t():
        sm = await _make_session()
        rid = await _insert_stale_reserved(sm, speaker_id="speaker_s1")
        n = await sweeper.sweep_once(session_factory=sm)
        assert n == 1, "应 expire 1 条 stale reserved"
        row = await _fetch_row(sm, rid)
        assert row is not None
        assert row.status == svc.EXPIRED
        assert row.released_reason == "ttl_expired", (
            "TTL 回收必须写 released_reason='ttl_expired'（审计可追溯）"
        )
    _run(_t())


def test_sweep_once_does_not_touch_active_consumed_released():
    """只动 stale；active(未过期) / consumed / released 不动（spec §10.4）。"""
    async def _t():
        sm = await _make_session()
        # active（未过期 reserved）
        async with sm() as db:
            active = await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_active"))
        # consumed
        async with sm() as db:
            consumed = await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_consumed"))
        async with sm() as db:
            await svc.consume(db, reservation_id=consumed.reservation_id, voice_id="v_keep")
        # released
        async with sm() as db:
            released = await svc.reserve(db, **_reserve_kwargs(speaker_id="speaker_released"))
        async with sm() as db:
            await svc.release(db, reservation_id=released.reservation_id, reason="worker_failed")
        # stale
        await _insert_stale_reserved(sm, speaker_id="speaker_stale")

        n = await sweeper.sweep_once(session_factory=sm)
        assert n == 1, "只 expire stale，不动 active / consumed / released"

        active_row = await _fetch_row(sm, active.reservation_id)
        assert active_row.status == svc.RESERVED, "未过期 active 不被 sweeper 动"
        assert active_row.released_reason is None

        consumed_row = await _fetch_row(sm, consumed.reservation_id)
        assert consumed_row.status == svc.CONSUMED
        assert consumed_row.consumed_voice_id == "v_keep"

        released_row = await _fetch_row(sm, released.reservation_id)
        assert released_row.status == svc.RELEASED
        assert released_row.released_reason == "worker_failed", (
            "已有 released_reason 不被 ttl_expired 覆盖"
        )
    _run(_t())


def test_sweep_once_idempotent():
    """重跑跳过已 expired 行（选行条件 status='reserved'）。"""
    async def _t():
        sm = await _make_session()
        await _insert_stale_reserved(sm, speaker_id="speaker_s1")
        await _insert_stale_reserved(sm, speaker_id="speaker_s2")
        n1 = await sweeper.sweep_once(session_factory=sm)
        assert n1 == 2
        n2 = await sweeper.sweep_once(session_factory=sm)
        assert n2 == 0, "已 expired 不重复处理（幂等）"
    _run(_t())


def test_sweep_once_respects_batch_size():
    """单批 cap：SWEEP_BATCH_SIZE 限制单轮处理行数（防长事务）。"""
    async def _t():
        sm = await _make_session()
        for i in range(5):
            await _insert_stale_reserved(sm, speaker_id=f"speaker_{i}", job_id=f"job_{i}")
        original = sweeper.SWEEP_BATCH_SIZE
        sweeper.SWEEP_BATCH_SIZE = 3
        try:
            n1 = await sweeper.sweep_once(session_factory=sm)
            assert n1 == 3, "单批应 cap 在 SWEEP_BATCH_SIZE=3"
            n2 = await sweeper.sweep_once(session_factory=sm)
            assert n2 == 2, "剩余 2 条下一轮处理"
        finally:
            sweeper.SWEEP_BATCH_SIZE = original
    _run(_t())


# ---------------------------------------------------------------------------
# sweeper_loop fail-safe + 干净退出（不真起无限循环）
# ---------------------------------------------------------------------------


def test_sweeper_loop_survives_sweep_exception():
    """单次 sweep 异常**不崩 loop**：续到下一周期，set stop 后干净退出。"""
    async def _t():
        stop = asyncio.Event()
        calls = {"n": 0}

        async def _boom(**kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                stop.set()  # 第二轮后退出，证明跨过了第一次异常
            raise RuntimeError("simulated DB hiccup")

        orig_sweep = sweeper.sweep_once
        orig_init = sweeper.INITIAL_DELAY_S
        orig_interval = sweeper.SWEEP_INTERVAL_S
        sweeper.sweep_once = _boom
        sweeper.INITIAL_DELAY_S = 0
        sweeper.SWEEP_INTERVAL_S = 0
        try:
            # 不抛异常即证明 loop 吞掉了 sweep 异常并续命
            await asyncio.wait_for(sweeper.sweeper_loop(stop_event=stop), timeout=5)
        finally:
            sweeper.sweep_once = orig_sweep
            sweeper.INITIAL_DELAY_S = orig_init
            sweeper.SWEEP_INTERVAL_S = orig_interval
        assert calls["n"] >= 2, "loop 应在第一次 sweep 异常后继续跑（不崩）"
    _run(_t())


def test_sweeper_loop_stops_before_first_sweep_when_event_preset():
    """stop_event 预先 set → 初始延迟立即被打断，loop 不跑任何 sweep。"""
    async def _t():
        stop = asyncio.Event()
        stop.set()  # 启动前已 set
        calls = {"n": 0}

        async def _count(**kwargs):
            calls["n"] += 1
            return 0

        orig_sweep = sweeper.sweep_once
        orig_init = sweeper.INITIAL_DELAY_S
        sweeper.sweep_once = _count
        sweeper.INITIAL_DELAY_S = 60  # 若 stop 不打断会卡 60s
        try:
            await asyncio.wait_for(sweeper.sweeper_loop(stop_event=stop), timeout=5)
        finally:
            sweeper.sweep_once = orig_sweep
            sweeper.INITIAL_DELAY_S = orig_init
        assert calls["n"] == 0, "预先 set stop → 初始延迟立即中断，不跑 sweep"
    _run(_t())


def test_sweeper_loop_runs_sweep_then_stops():
    """正常路径：跑 1 轮 sweep 后 stop → 干净退出。"""
    async def _t():
        stop = asyncio.Event()
        calls = {"n": 0}

        async def _once(**kwargs):
            calls["n"] += 1
            stop.set()
            return 0

        orig_sweep = sweeper.sweep_once
        orig_init = sweeper.INITIAL_DELAY_S
        orig_interval = sweeper.SWEEP_INTERVAL_S
        sweeper.sweep_once = _once
        sweeper.INITIAL_DELAY_S = 0
        sweeper.SWEEP_INTERVAL_S = 0
        try:
            await asyncio.wait_for(sweeper.sweeper_loop(stop_event=stop), timeout=5)
        finally:
            sweeper.sweep_once = orig_sweep
            sweeper.INITIAL_DELAY_S = orig_init
            sweeper.SWEEP_INTERVAL_S = orig_interval
        assert calls["n"] == 1, "应跑恰好 1 轮 sweep 后 stop 退出"
    _run(_t())


# ---------------------------------------------------------------------------
# AST 守卫：sweeper 不 import 付费 / 外部 API（Codex PR2-D 边界）
# ---------------------------------------------------------------------------


def _collect_imported_modules(src: str) -> set[str]:
    """收集源码里所有 import 的模块名（含 import / from-import / 函数内 lazy）。"""
    tree = ast.parse(src)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
    return modules


def test_sweeper_module_no_paid_or_http_imports():
    """spec §8 / Codex PR2-D：sweeper **只动 DB**，不 import 任何付费 / 外部
    API client。锁死 mainland worker / sample uploader / register-smart /
    httpx / requests / boto3 —— 防未来重构把付费动作塞进 sweeper。"""
    src = (_GATEWAY_DIR / "express_reservation_sweeper.py").read_text(encoding="utf-8")
    modules = _collect_imported_modules(src)
    forbidden_substrings = (
        "mainland_worker",   # 付费 worker clone
        "httpx",             # HTTP client
        "requests",          # HTTP client
        "boto3",             # OSS / R2 上传
        "cosyvoice_clone",   # express-sample-upload endpoint 模块
        "sample_extractor",  # 样本抽取
        "sample_upload",     # 样本上传
        "register_smart",    # register-smart client
        "voice_clone",       # 任何声音克隆路径
    )
    for mod_name in modules:
        low = mod_name.lower()
        for bad in forbidden_substrings:
            assert bad not in low, (
                f"express_reservation_sweeper 不应 import {mod_name!r}"
                f"（命中禁用片段 {bad!r}）—— sweeper 只允许纯 DB 状态流转，"
                f"绝不调付费 / 外部 API"
            )


def test_sweeper_only_calls_expire_stale_from_reservation_service():
    """sweeper 对 reservation service 的调用面只允许 expire_stale_reservations
    （不允许调 reserve / consume / release 等会改语义的函数）。"""
    src = (_GATEWAY_DIR / "express_reservation_sweeper.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 收集对 _reservation_service.<attr> 的属性访问
    accessed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "_reservation_service":
                accessed.add(node.attr)
    assert accessed <= {"expire_stale_reservations"}, (
        f"sweeper 只应调 expire_stale_reservations，实际访问了 {accessed}"
    )


def test_sweeper_module_imports_are_stdlib_and_db_only():
    """正向白名单：sweeper 顶层依赖只有 stdlib + database + reservation service。"""
    src = (_GATEWAY_DIR / "express_reservation_sweeper.py").read_text(encoding="utf-8")
    modules = _collect_imported_modules(src)
    allowed = {
        "__future__", "asyncio", "logging", "os",
        "database", "express_reservation_service",
    }
    extra = modules - allowed
    assert not extra, f"sweeper 引入了预期外的依赖：{extra}"


# ---------------------------------------------------------------------------
# lifespan wiring（静态扫，不真起循环）
# ---------------------------------------------------------------------------


def test_lifespan_wires_reservation_sweeper():
    """spec §8：gateway lifespan 挂 reservation sweeper。静态确认：
    import + create_task + app.state stash + shutdown cancel 都在。"""
    src = (_GATEWAY_DIR / "main.py").read_text(encoding="utf-8")
    assert "from express_reservation_sweeper import sweeper_loop" in src, (
        "lifespan 必须 import express_reservation_sweeper.sweeper_loop"
    )
    assert "express-reservation-sweeper" in src, "create_task 应命名 task"
    # stash + shutdown cancel 都引用同一 app.state 属性（出现 ≥ 2 次）
    assert src.count("express_reservation_sweeper_task") >= 2, (
        "app.state.express_reservation_sweeper_task 应同时用于 stash 和 shutdown cancel"
    )


def test_lifespan_sweeper_start_is_failsafe():
    """fail-safe：sweeper 启动包在 try/except 里，失败只 log 不阻塞 gateway。
    AST 确认 import 语句位于某个 Try 节点的 body 内。"""
    src = (_GATEWAY_DIR / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def _try_contains_sweeper_import(try_node: ast.Try) -> bool:
        for child in ast.walk(try_node):
            if isinstance(child, ast.ImportFrom) and child.module == "express_reservation_sweeper":
                return True
        return False

    wrapped = any(
        isinstance(node, ast.Try) and _try_contains_sweeper_import(node)
        for node in ast.walk(tree)
    )
    assert wrapped, (
        "express_reservation_sweeper 的启动 import/create_task 必须包在 "
        "try/except 内（fail-safe：sweeper 故障不得阻塞 gateway 启动）"
    )
    # except 分支记 log（不静默吞）
    assert "Failed to start express_reservation_sweeper" in src, (
        "sweeper 启动失败必须 log（不静默）"
    )
