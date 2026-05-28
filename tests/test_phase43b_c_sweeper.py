"""Phase 4.3b-C — temporary voice cleanup sweeper + audit + lifespan 守卫
（aiosqlite + mock worker / 注入 audit，0 真实 DashScope）。

覆盖 Codex 4.3b-C 三重点 + spec §4.2/§6：

- **worker 不可用在 claim 之前 fail-fast**（实跑模式 → 整轮 skip，不认领）
- **默认 dry-run**（env 未设 → dry_run=True，不删）
- **audit 覆盖 success / fail / give-up / dry-run**（注入 recording emitter）
- sweeper loop fail-safe（单轮异常不崩 loop）+ lifespan wiring（静态，不真起循环）
- audit emitter 真写 JSONL（tmp 目录）
"""
from __future__ import annotations

import ast
import asyncio
import json
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
import express_voice_cleanup_sweeper as swp  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000e7")


def _past(h: float = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=h)


async def _make_session() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda s: UserVoice.__table__.create(s))
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _mk_voice(**over) -> UserVoice:
    base = dict(
        id=uuid.uuid4(), user_id=_USER,
        voice_id=f"cosyvoice-v3.5-flash-{uuid.uuid4().hex[:8]}",
        provider="cosyvoice_voice_clone", label="express-clone",
        region_constraint="mainland_only", is_temporary=True, requires_worker=True,
        target_model="cosyvoice-v3.5-flash", temporary_expires_at=_past(),
        expired_at=None, cleanup_attempts=0, source_job_id="job_x",
    )
    base.update(over)
    return UserVoice(**base)


async def _insert(sm, *voices):
    async with sm() as db:
        for v in voices:
            db.add(v)
        await db.commit()


async def _get(sm, pk):
    async with sm() as db:
        return (await db.execute(select(UserVoice).where(UserVoice.id == pk))).scalar_one_or_none()


def _recording_audit(records: list):
    def _emit(**fields):
        records.append(dict(fields))
    return _emit


def _mk_worker(calls: list, *, fail=False):
    def _w(voice_id, *, user_id, job_id, reason):
        calls.append(voice_id)
        if fail:
            raise RuntimeError("delete_voice_failed")
    return _w


# ===========================================================================
# audit 覆盖（core 直接调，注入 recording emitter）
# ===========================================================================


def test_audit_emits_cleaned_on_success():
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        records: list = []
        await svc.cleanup_expired_temporary_voices(
            sm, worker_delete=_mk_worker([]), dry_run=False, limit=10,
            audit_emit=_recording_audit(records),
        )
        cleaned = [r for r in records if r["decision"] == "cleaned"]
        assert len(cleaned) == 1 and cleaned[0]["voice_id"] == v.voice_id
    _run(_t())


def test_audit_emits_failed():
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        records: list = []
        await svc.cleanup_expired_temporary_voices(
            sm, worker_delete=_mk_worker([], fail=True), dry_run=False, limit=10,
            audit_emit=_recording_audit(records),
        )
        failed = [r for r in records if r["decision"] == "cleanup_failed"]
        assert len(failed) == 1 and failed[0]["error"] == "RuntimeError"
    _run(_t())


def test_audit_emits_give_up():
    async def _t():
        sm = await _make_session()
        v = _mk_voice(cleanup_attempts=svc.MAX_CLEANUP_ATTEMPTS - 1)
        await _insert(sm, v)
        records: list = []
        await svc.cleanup_expired_temporary_voices(
            sm, worker_delete=_mk_worker([], fail=True), dry_run=False, limit=10,
            audit_emit=_recording_audit(records),
        )
        assert [r["decision"] for r in records] == ["cleanup_give_up"]
    _run(_t())


def test_audit_emits_dry_run():
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        records: list = []
        await svc.cleanup_expired_temporary_voices(
            sm, worker_delete=_mk_worker([]), dry_run=True, limit=10,
            audit_emit=_recording_audit(records),
        )
        assert [r["decision"] for r in records] == ["dry_run"]
        assert records[0]["dry_run"] is True and records[0]["voice_id"] == v.voice_id
    _run(_t())


def test_audit_failure_in_emitter_does_not_break_cleanup():
    """audit emitter 抛异常不破坏清理（_safe_audit 吞）。"""
    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)

        def _boom(**fields):
            raise RuntimeError("audit sink down")

        report = await svc.cleanup_expired_temporary_voices(
            sm, worker_delete=_mk_worker([]), dry_run=False, limit=10, audit_emit=_boom,
        )
        assert report.deleted == 1, "audit 失败不应影响清理结果"
        row = await _get(sm, v.id)
        assert row.expired_at is not None
    _run(_t())


# ===========================================================================
# sweep_once：dry-run 默认 + worker fail-fast before claim + 真删
# ===========================================================================


def test_sweep_once_dry_run_default_no_delete(monkeypatch):
    """env 未设 → dry-run 默认 True：不删、不认领、不需要 worker。"""
    monkeypatch.delenv("AVT_EXPRESS_VOICE_CLEANUP_DRY_RUN", raising=False)
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", "/nonexistent_avt_test_audit_dir")

    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        report = await swp.sweep_once(session_factory=sm)  # dry_run=None → env default True
        assert report is not None and report.dry_run is True
        assert report.selected == [v.voice_id]
        row = await _get(sm, v.id)
        assert row.expired_at is None, "dry-run 不删"
        assert row.cleanup_run_id is None, "dry-run 不认领"
    _run(_t())


def test_sweep_once_worker_unavailable_failfast_before_claim(monkeypatch):
    """实跑模式 + worker 不可用 → 整轮 skip（return None），**不认领任何行**。"""
    import mainland_voice_worker as mvw
    monkeypatch.setattr(mvw, "is_mainland_voice_worker_config_ready", lambda s: False)

    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        report = await swp.sweep_once(session_factory=sm, dry_run=False)
        assert report is None, "worker 不可用应整轮 skip"
        row = await _get(sm, v.id)
        assert row.cleanup_run_id is None and row.cleanup_claim_until is None, (
            "fail-fast 必须在 claim 之前 —— 不应留 lease"
        )
        assert row.expired_at is None
    _run(_t())


def test_sweep_once_real_delete_when_worker_ready(monkeypatch):
    """实跑 + worker 可用 → 调 client.delete_voice → 软删行 + close client。"""
    import mainland_voice_worker as mvw

    class _FakeClient:
        def __init__(self):
            self.deleted = []
            self.closed = False

        def delete_voice(self, voice_id, req):
            self.deleted.append(voice_id)

        def close(self):
            self.closed = True

    fake = _FakeClient()
    monkeypatch.setattr(mvw, "is_mainland_voice_worker_config_ready", lambda s: True)
    monkeypatch.setattr(mvw, "build_mainland_voice_worker_client", lambda s: fake)

    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        report = await swp.sweep_once(session_factory=sm, dry_run=False)
        assert report is not None and report.deleted == 1
        assert fake.deleted == [v.voice_id]
        assert fake.closed is True, "client 必须 close"
        row = await _get(sm, v.id)
        assert row.expired_at is not None and row.cleanup_run_id is None
    _run(_t())


# ===========================================================================
# loop fail-safe + dry-run default helper
# ===========================================================================


def test_dry_run_default_semantics(monkeypatch):
    monkeypatch.delenv("AVT_EXPRESS_VOICE_CLEANUP_DRY_RUN", raising=False)
    assert swp._dry_run_default() is True
    monkeypatch.setenv("AVT_EXPRESS_VOICE_CLEANUP_DRY_RUN", "false")
    assert swp._dry_run_default() is False
    monkeypatch.setenv("AVT_EXPRESS_VOICE_CLEANUP_DRY_RUN", "FALSE")
    assert swp._dry_run_default() is False
    monkeypatch.setenv("AVT_EXPRESS_VOICE_CLEANUP_DRY_RUN", "true")
    assert swp._dry_run_default() is True


def test_sweeper_loop_survives_exception():
    async def _t():
        stop = asyncio.Event()
        calls = {"n": 0}

        async def _boom(**kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                stop.set()
            raise RuntimeError("tick down")

        orig = swp.sweep_once
        orig_init, orig_interval = swp.INITIAL_DELAY_S, swp.SWEEP_INTERVAL_S
        swp.sweep_once = _boom
        swp.INITIAL_DELAY_S = 0
        swp.SWEEP_INTERVAL_S = 0
        try:
            await asyncio.wait_for(swp.sweeper_loop(stop_event=stop), timeout=5)
        finally:
            swp.sweep_once = orig
            swp.INITIAL_DELAY_S = orig_init
            swp.SWEEP_INTERVAL_S = orig_interval
        assert calls["n"] >= 2, "loop 应在异常后续命"
    _run(_t())


# ===========================================================================
# audit emitter 真写 JSONL
# ===========================================================================


def test_emit_voice_cleanup_audit_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))
    from express_voice_cleanup_audit import _AUDIT_FILENAME, emit_voice_cleanup_audit

    emit_voice_cleanup_audit(decision="cleaned", voice_id="v1", user_id="u1")
    f = tmp_path / _AUDIT_FILENAME
    assert f.exists()
    line = json.loads(f.read_text(encoding="utf-8").strip())
    assert line["kind"] == "express_temp_voice_cleanup"
    assert line["decision"] == "cleaned" and line["voice_id"] == "v1"


# ===========================================================================
# lifespan wiring（静态扫）
# ===========================================================================


def test_lifespan_wires_voice_cleanup_sweeper():
    src = (_GATEWAY / "main.py").read_text(encoding="utf-8")
    assert "from express_voice_cleanup_sweeper import sweeper_loop" in src
    assert "express-voice-cleanup-sweeper" in src
    assert src.count("express_voice_cleanup_sweeper_task") >= 2, (
        "app.state task 应同时用于 stash + shutdown cancel"
    )


def test_lifespan_voice_cleanup_failsafe():
    """启动 import 包在 try/except 内（sweeper 故障不阻塞 gateway 启动）。"""
    src = (_GATEWAY / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def _try_has_import(t: ast.Try) -> bool:
        return any(
            isinstance(n, ast.ImportFrom) and n.module == "express_voice_cleanup_sweeper"
            for n in ast.walk(t)
        )

    assert any(isinstance(n, ast.Try) and _try_has_import(n) for n in ast.walk(tree)), (
        "voice cleanup sweeper 启动必须包在 try/except（fail-safe）"
    )
    assert "Failed to start express_voice_cleanup_sweeper" in src


# ===========================================================================
# 核心 worker 调用走线程（不阻塞事件循环）
# ===========================================================================


def test_core_offloads_worker_to_thread():
    """守卫：core 用 asyncio.to_thread 调 worker_delete（同步 client 不阻塞事件循环）。"""
    src = (_GATEWAY / "express_voice_cleanup_service.py").read_text(encoding="utf-8")
    assert "asyncio.to_thread(" in src and "worker_delete" in src, (
        "worker_delete 必须经 asyncio.to_thread 调用（同步阻塞 client 丢线程）"
    )
