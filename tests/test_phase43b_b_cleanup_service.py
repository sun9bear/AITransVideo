"""Phase 4.3b-B — express_voice_cleanup_service 状态机 / claim-lease / run_id 守卫
（真 in-memory aiosqlite + mock worker_delete，0 真实 DashScope）。

覆盖 spec §2.1/§2.2/§2.4/§2.7 + §7：

- select_eligible 只选 cosyvoice + temporary + requires_worker + 到期 + 未软删
  + attempts<MAX + retry_after gate + claim lease gate
- claim 写 claim_until + run_id；已认领行本轮不再选；lease 过期可重认领
- complete_soft_delete：写 expired_at + 清 claim，run_id 守卫（stale run_id no-op）
- release_with_backoff：attempts+1 + last_error + **立刻清 claim** + retry_after；
  达 MAX give-up；run_id 守卫
- cleanup 核心：成功软删 / 失败不写 expired_at（保留可重试）/ dry-run 不调 worker
  不改 DB / 未知 delete 失败全部重试（无 already-gone）/ claim 在 worker 之前
- LEASE 常量 >= delete 最坏窗口下界 + 模块不 import worker client

并发原子性（FOR UPDATE SKIP LOCKED）sqlite 测不了 → 留 B-pg。
"""
from __future__ import annotations

import ast
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


from models import UserVoice  # noqa: E402
import express_voice_cleanup_service as svc  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000e3")


def _past(hours: float = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _future(hours: float = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


async def _make_session() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda s: UserVoice.__table__.create(s))
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _mk_voice(**over) -> UserVoice:
    base = dict(
        id=uuid.uuid4(),
        user_id=_USER,
        voice_id=f"cosyvoice-v3.5-flash-{uuid.uuid4().hex[:8]}",
        provider="cosyvoice_voice_clone",
        label="express-clone",
        region_constraint="mainland_only",
        is_temporary=True,
        requires_worker=True,
        target_model="cosyvoice-v3.5-flash",
        temporary_expires_at=_past(),
        expired_at=None,
        cleanup_attempts=0,
        source_job_id="job_x",
    )
    base.update(over)
    return UserVoice(**base)


async def _insert(sm, *voices) -> None:
    async with sm() as db:
        for v in voices:
            db.add(v)
        await db.commit()


async def _get(sm, pk) -> UserVoice | None:
    async with sm() as db:
        return (
            await db.execute(select(UserVoice).where(UserVoice.id == pk))
        ).scalar_one_or_none()


def _mk_worker(calls: list, *, fail: bool = False, exc: BaseException | None = None):
    def _w(voice_id, *, user_id, job_id, reason):
        calls.append(voice_id)
        if fail:
            raise (exc or RuntimeError("delete_voice_failed"))
    return _w


# ---------------------------------------------------------------------------
# select_eligible
# ---------------------------------------------------------------------------


def test_select_eligible_only_expired_temp_cosyvoice():
    async def _t():
        sm = await _make_session()
        good = _mk_voice()
        non_temp = _mk_voice(is_temporary=False)
        non_cosy = _mk_voice(provider="minimax_voice_clone")
        no_worker = _mk_voice(requires_worker=False)
        not_due = _mk_voice(temporary_expires_at=_future())
        already = _mk_voice(expired_at=_past())
        gave_up = _mk_voice(cleanup_attempts=svc.MAX_CLEANUP_ATTEMPTS)
        backoff = _mk_voice(cleanup_retry_after=_future())
        leased = _mk_voice(cleanup_claim_until=_future(), cleanup_run_id="other")
        await _insert(sm, good, non_temp, non_cosy, no_worker, not_due, already, gave_up, backoff, leased)
        async with sm() as db:
            rows = await svc.select_eligible(db, limit=50)
        ids = {r.id for r in rows}
        assert ids == {good.id}, f"只应选 1 个 eligible，实际 {len(ids)}"
    _run(_t())


def test_select_eligible_include_give_up_picks_maxed_row():
    async def _t():
        sm = await _make_session()
        gave_up = _mk_voice(cleanup_attempts=svc.MAX_CLEANUP_ATTEMPTS)
        await _insert(sm, gave_up)
        async with sm() as db:
            without = await svc.select_eligible(db, limit=50, include_give_up=False)
            withg = await svc.select_eligible(db, limit=50, include_give_up=True)
        assert without == []
        assert {r.id for r in withg} == {gave_up.id}
    _run(_t())


# ---------------------------------------------------------------------------
# claim_batch
# ---------------------------------------------------------------------------


def test_claim_writes_lease_and_run_id():
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        async with sm() as db:
            claimed = await svc.claim_batch(db, run_id="run-1", limit=10)
        assert [c.voice_id for c in claimed] == [v.voice_id]
        row = await _get(sm, v.id)
        assert row.cleanup_run_id == "run-1"
        assert row.cleanup_claim_until is not None
    _run(_t())


def test_claimed_row_excluded_from_select():
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        async with sm() as db:
            await svc.claim_batch(db, run_id="run-1", limit=10)
        # 同一 now 下，已认领（claim_until 未来）→ 不再被选
        async with sm() as db:
            rows = await svc.select_eligible(db, limit=10)
        assert rows == []
    _run(_t())


def test_expired_lease_reclaimable():
    async def _t():
        sm = await _make_session()
        v = _mk_voice(cleanup_claim_until=_past(), cleanup_run_id="stale")
        await _insert(sm, v)
        async with sm() as db:
            rows = await svc.select_eligible(db, limit=10)
        assert {r.id for r in rows} == {v.id}, "lease 过期应可重认领"
    _run(_t())


# ---------------------------------------------------------------------------
# complete_soft_delete / release_with_backoff（run_id 守卫）
# ---------------------------------------------------------------------------


def test_complete_soft_delete_sets_expired_clears_claim():
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        async with sm() as db:
            await svc.claim_batch(db, run_id="run-1", limit=10)
        async with sm() as db:
            ok = await svc.complete_soft_delete(db, v.id, run_id="run-1")
        assert ok is True
        row = await _get(sm, v.id)
        assert row.expired_at is not None
        assert row.cleanup_claim_until is None and row.cleanup_run_id is None
    _run(_t())


def test_complete_guarded_by_run_id():
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        async with sm() as db:
            await svc.claim_batch(db, run_id="run-1", limit=10)
        # 用错误 run_id 完成 → no-op，不软删
        async with sm() as db:
            ok = await svc.complete_soft_delete(db, v.id, run_id="WRONG")
        assert ok is False
        row = await _get(sm, v.id)
        assert row.expired_at is None, "stale run_id 不得 clobber"
    _run(_t())


def test_release_with_backoff_clears_claim_and_sets_retry():
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        async with sm() as db:
            await svc.claim_batch(db, run_id="run-1", limit=10)
        async with sm() as db:
            outcome = await svc.release_with_backoff(db, v.id, run_id="run-1", error="delete_voice_failed")
        assert outcome == "failed"
        row = await _get(sm, v.id)
        assert row.cleanup_attempts == 1
        assert row.cleanup_last_error == "delete_voice_failed"
        assert row.cleanup_claim_until is None and row.cleanup_run_id is None, (
            "失败必须立刻清 claim（不留 lease 悬挂）"
        )
        assert row.cleanup_retry_after is not None
        assert row.expired_at is None, "失败绝不写 expired_at"
    _run(_t())


def test_release_give_up_at_max_attempts():
    async def _t():
        sm = await _make_session()
        v = _mk_voice(cleanup_attempts=svc.MAX_CLEANUP_ATTEMPTS - 1)
        await _insert(sm, v)
        async with sm() as db:
            await svc.claim_batch(db, run_id="run-1", limit=10)
        async with sm() as db:
            outcome = await svc.release_with_backoff(db, v.id, run_id="run-1", error="x")
        assert outcome == "gave_up"
        row = await _get(sm, v.id)
        assert row.cleanup_attempts == svc.MAX_CLEANUP_ATTEMPTS
        assert row.cleanup_retry_after is None, "give-up 不设 retry_after"
        # give-up 行默认 select 排除
        async with sm() as db:
            rows = await svc.select_eligible(db, limit=10)
        assert rows == []
    _run(_t())


def test_release_guarded_by_run_id():
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        async with sm() as db:
            await svc.claim_batch(db, run_id="run-1", limit=10)
        async with sm() as db:
            outcome = await svc.release_with_backoff(db, v.id, run_id="WRONG", error="x")
        assert outcome == "noop"
        row = await _get(sm, v.id)
        assert row.cleanup_attempts == 0, "stale run_id 不得改 attempts"
        assert row.cleanup_run_id == "run-1", "原 lease 不被 stale runner 清"
    _run(_t())


# ---------------------------------------------------------------------------
# cleanup_expired_temporary_voices 核心
# ---------------------------------------------------------------------------


def test_cleanup_success_path_soft_deletes():
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        calls: list = []
        report = await svc.cleanup_expired_temporary_voices(
            sm, worker_delete=_mk_worker(calls), dry_run=False, limit=10
        )
        assert report.deleted == 1 and report.failed == 0
        assert calls == [v.voice_id]
        row = await _get(sm, v.id)
        assert row.expired_at is not None
        assert row.cleanup_claim_until is None and row.cleanup_run_id is None
    _run(_t())


def test_cleanup_failure_does_not_set_expired_and_clears_claim():
    """KEY（Codex B）：worker 失败 → 不写 expired_at + 清 claim + backoff。"""
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        calls: list = []
        report = await svc.cleanup_expired_temporary_voices(
            sm, worker_delete=_mk_worker(calls, fail=True), dry_run=False, limit=10
        )
        assert report.failed == 1 and report.deleted == 0
        row = await _get(sm, v.id)
        assert row.expired_at is None, "失败绝不软删"
        assert row.cleanup_attempts == 1
        assert row.cleanup_claim_until is None and row.cleanup_run_id is None, (
            "失败立刻释放 lease"
        )
        assert row.cleanup_retry_after is not None
    _run(_t())


def test_cleanup_unknown_error_retries_not_already_gone_success():
    """spec §2.4 / P1-2：未知 delete 失败一律重试，绝不当成功软删。"""
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)

        class _FakeWorkerError(Exception):
            code = "delete_voice_failed"

        report = await svc.cleanup_expired_temporary_voices(
            sm, worker_delete=_mk_worker([], fail=True, exc=_FakeWorkerError("boom")),
            dry_run=False, limit=10,
        )
        assert report.deleted == 0 and report.failed == 1
        row = await _get(sm, v.id)
        assert row.expired_at is None
        assert row.cleanup_last_error == "delete_voice_failed"
    _run(_t())


def test_cleanup_dry_run_no_worker_no_db_change():
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        calls: list = []
        report = await svc.cleanup_expired_temporary_voices(
            sm, worker_delete=_mk_worker(calls), dry_run=True, limit=10
        )
        assert report.dry_run is True
        assert report.selected == [v.voice_id]
        assert calls == [], "dry-run 绝不调 worker"
        row = await _get(sm, v.id)
        assert row.expired_at is None
        assert row.cleanup_claim_until is None and row.cleanup_run_id is None, (
            "dry-run 不认领、不改 DB"
        )
    _run(_t())


def test_cleanup_give_up_after_max():
    async def _t():
        sm = await _make_session()
        v = _mk_voice(cleanup_attempts=svc.MAX_CLEANUP_ATTEMPTS - 1)
        await _insert(sm, v)
        report = await svc.cleanup_expired_temporary_voices(
            sm, worker_delete=_mk_worker([], fail=True), dry_run=False, limit=10
        )
        assert report.gave_up == 1 and report.failed == 0
        row = await _get(sm, v.id)
        assert row.cleanup_attempts == svc.MAX_CLEANUP_ATTEMPTS
        assert row.expired_at is None
    _run(_t())


def test_cleanup_claims_before_worker_call():
    """顺序守卫：claim → worker → complete（monkeypatch 记录调用序）。"""
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        order: list[str] = []
        orig_claim = svc.claim_batch
        orig_complete = svc.complete_soft_delete

        async def _claim_rec(*a, **k):
            order.append("claim")
            return await orig_claim(*a, **k)

        async def _complete_rec(*a, **k):
            order.append("complete")
            return await orig_complete(*a, **k)

        def _worker(voice_id, *, user_id, job_id, reason):
            order.append("worker")

        svc.claim_batch = _claim_rec
        svc.complete_soft_delete = _complete_rec
        try:
            await svc.cleanup_expired_temporary_voices(
                sm, worker_delete=_worker, dry_run=False, limit=10
            )
        finally:
            svc.claim_batch = orig_claim
            svc.complete_soft_delete = orig_complete
        assert order == ["claim", "worker", "complete"], f"调用顺序错: {order}"
    _run(_t())


# ---------------------------------------------------------------------------
# 常量守卫 + 模块隔离
# ---------------------------------------------------------------------------


def test_lease_exceeds_real_delete_worst_case():
    """spec §2.7 / Codex 4.3b-B-fix P2：LEASE + floor 必须 >= 真实 client
    delete_voice 最坏重试窗口——**从真实 client 常量重算**，client retry/timeout
    变大而 LEASE/floor 没跟上 → red（不再依赖一个偏低的硬编码 floor）。"""
    from services.mainland_worker.client import (
        DEFAULT_TIMEOUT_SECONDS,
        MAX_NETWORK_RETRIES,
        RETRY_BACKOFF_SECONDS,
    )

    t = DEFAULT_TIMEOUT_SECONDS
    per_attempt = (t.connect or 0) + (t.read or 0) + (t.write or 0)
    backoff_between = sum(RETRY_BACKOFF_SECONDS[: max(0, MAX_NETWORK_RETRIES - 1)])
    worst_case = MAX_NETWORK_RETRIES * per_attempt + backoff_between
    assert svc.CLEANUP_CLAIM_LEASE_SECONDS >= worst_case, (
        f"LEASE {svc.CLEANUP_CLAIM_LEASE_SECONDS} < delete 最坏窗口 {worst_case}（双删风险）"
    )
    assert svc.DELETE_VOICE_WORST_CASE_FLOOR_SECONDS >= worst_case, (
        f"floor {svc.DELETE_VOICE_WORST_CASE_FLOOR_SECONDS} < 真实最坏窗口 {worst_case}；"
        f"client retry/timeout 变大了，请上调 floor + LEASE"
    )
    assert svc.CLEANUP_CLAIM_LEASE_SECONDS == 600


def test_release_noop_on_already_expired_row():
    """Codex 4.3b-B-fix P3：行已软删（manual / 竞态）后，失败 release 不再
    bump attempts/error（release 也加 expired_at IS NULL 守卫，与 complete 一致）。"""
    async def _t():
        sm = await _make_session()
        v = _mk_voice(expired_at=_past())  # 已软删
        await _insert(sm, v)
        # 模拟竞态：claim 后该行被 manual 软删，留着 claim/run_id
        async with sm() as db:
            row = (
                await db.execute(select(UserVoice).where(UserVoice.id == v.id))
            ).scalar_one()
            row.cleanup_claim_until = _future()
            row.cleanup_run_id = "run-1"
            await db.commit()
        async with sm() as db:
            outcome = await svc.release_with_backoff(db, v.id, run_id="run-1", error="x")
        assert outcome == "noop", "已软删行不应被失败路径更新"
        row = await _get(sm, v.id)
        assert row.cleanup_attempts == 0, "expired 行 attempts 不被 bump"
        assert row.cleanup_last_error is None


def test_service_does_not_import_worker_client():
    """worker 注入式：core 模块不 import mainland worker / httpx / requests。"""
    src = (_GATEWAY / "express_voice_cleanup_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    for m in mods:
        low = m.lower()
        for bad in ("mainland_worker", "httpx", "requests", "cosyvoice_clone"):
            assert bad not in low, f"core 不应 import {m!r}（worker 走注入）"
