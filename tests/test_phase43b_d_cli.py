"""Phase 4.3b-D — manual cleanup CLI（默认 dry-run / reset / include-give-up 安全）。

覆盖 Codex 4.3b-D：

- arg parser 默认 dry-run（不传 --execute = 只报告）+ 各 flag
- ``reset_cleanup_state`` 重置 give-up/backoff 行；不动非 stuck / 非 eligible
- run_cleanup_cli：dry-run 默认不 mutate；``--reset-attempts`` 在 dry-run 下被忽略；
  ``--execute --reset-attempts`` 复活 give-up 行并清理；``--include-give-up`` 透传
- CLI **不持** browser session / X-Internal-Key（server-side script）
- CLI audit 复用 emit_voice_cleanup_audit（同一套字段）—— 经 sweep_once

aiosqlite + mock worker，0 真实 DashScope。
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
import cleanup_temp_voices_cli as cli  # noqa: E402


def _run(coro):
    # asyncio.run shuts down the default executor cleanly (joins asyncio.to_thread
    # worker threads before closing the loop) → no "Event loop is closed" thread
    # warning from the cleanup core's to_thread(worker_delete).
    return asyncio.run(coro)


_USER = uuid.UUID("00000000-0000-0000-0000-0000000000ee")


def _past(h: float = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=h)


async def _make_session():
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


def _args(**over):
    base = dict(execute=False, limit=50, include_give_up=False, reset_attempts=False)
    base.update(over)
    return type("Args", (), base)()


# ---------------------------------------------------------------------------
# arg parser
# ---------------------------------------------------------------------------


def test_arg_parser_defaults_dry_run():
    a = cli.build_arg_parser().parse_args([])
    assert a.execute is False, "默认必须是 dry-run（不传 --execute）"
    assert a.limit == 50
    assert a.include_give_up is False
    assert a.reset_attempts is False


def test_arg_parser_flags():
    a = cli.build_arg_parser().parse_args(
        ["--execute", "--limit", "10", "--include-give-up", "--reset-attempts"]
    )
    assert a.execute is True and a.limit == 10
    assert a.include_give_up is True and a.reset_attempts is True


# ---------------------------------------------------------------------------
# reset_cleanup_state
# ---------------------------------------------------------------------------


def test_reset_cleanup_state_revives_stuck_rows():
    async def _t():
        sm = await _make_session()
        gave_up = _mk_voice(cleanup_attempts=svc.MAX_CLEANUP_ATTEMPTS,
                            cleanup_last_error="boom", cleanup_retry_after=_past())
        backoff = _mk_voice(cleanup_attempts=2, cleanup_retry_after=_past())
        fresh = _mk_voice(cleanup_attempts=0)        # 未 stuck，不应动
        non_temp = _mk_voice(is_temporary=False, cleanup_attempts=3)  # 非 eligible
        await _insert(sm, gave_up, backoff, fresh, non_temp)
        async with sm() as db:
            n = await svc.reset_cleanup_state(db, limit=50)
        assert n == 2, "只重置 stuck（attempts>0）的 eligible 行"
        g = await _get(sm, gave_up.id)
        assert g.cleanup_attempts == 0 and g.cleanup_last_error is None
        assert g.cleanup_retry_after is None
        nt = await _get(sm, non_temp.id)
        assert nt.cleanup_attempts == 3, "非 eligible 行不被重置"
    _run(_t())


# ---------------------------------------------------------------------------
# run_cleanup_cli
# ---------------------------------------------------------------------------


def test_run_cli_dry_run_default_no_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))

    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        report, reset_count = await cli.run_cleanup_cli(_args(), session_factory=sm)
        assert report is not None and report.dry_run is True
        assert report.selected == [v.voice_id] and reset_count == 0
        row = await _get(sm, v.id)
        assert row.expired_at is None and row.cleanup_run_id is None
    _run(_t())


def test_run_cli_reset_attempts_ignored_in_dry_run(tmp_path, monkeypatch):
    """--reset-attempts 不带 --execute → reset 被忽略（dry-run 不 mutate）。"""
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))

    async def _t():
        sm = await _make_session()
        v = _mk_voice(cleanup_attempts=svc.MAX_CLEANUP_ATTEMPTS, cleanup_last_error="x")
        await _insert(sm, v)
        report, reset_count = await cli.run_cleanup_cli(
            _args(reset_attempts=True, execute=False), session_factory=sm
        )
        assert reset_count == 0, "dry-run 下 --reset-attempts 不 mutate"
        row = await _get(sm, v.id)
        assert row.cleanup_attempts == svc.MAX_CLEANUP_ATTEMPTS, "attempts 未被重置"
    _run(_t())


def _patch_worker_ready(monkeypatch):
    import mainland_voice_worker as mvw

    class _FakeResp:
        worker_request_id = "wr-cli"

    class _FakeClient:
        def __init__(self):
            self.deleted = []
            self.closed = False

        def delete_voice(self, voice_id, req):
            self.deleted.append(voice_id)
            return _FakeResp()

        def close(self):
            self.closed = True

    fake = _FakeClient()
    monkeypatch.setattr(mvw, "is_mainland_voice_worker_config_ready", lambda s: True)
    monkeypatch.setattr(mvw, "build_mainland_voice_worker_client", lambda s: fake)
    return fake


def test_run_cli_execute_reset_then_cleanup(tmp_path, monkeypatch):
    """--execute --reset-attempts：复活 give-up 行 → 清理删除。"""
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))
    fake = _patch_worker_ready(monkeypatch)

    async def _t():
        sm = await _make_session()
        v = _mk_voice(cleanup_attempts=svc.MAX_CLEANUP_ATTEMPTS, cleanup_last_error="x")
        await _insert(sm, v)
        report, reset_count = await cli.run_cleanup_cli(
            _args(execute=True, reset_attempts=True), session_factory=sm
        )
        assert reset_count == 1, "give-up 行被重置"
        assert report is not None and report.deleted == 1
        assert fake.deleted == [v.voice_id]
        row = await _get(sm, v.id)
        assert row.expired_at is not None, "复活后被清理删除"
    _run(_t())


def test_run_cli_include_give_up_without_reset_processes_maxed_row(tmp_path, monkeypatch):
    """--execute --include-give-up（不 reset）：give-up 行也被纳入处理（再试一次）。"""
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))
    fake = _patch_worker_ready(monkeypatch)

    async def _t():
        sm = await _make_session()
        v = _mk_voice(cleanup_attempts=svc.MAX_CLEANUP_ATTEMPTS)
        await _insert(sm, v)
        report, reset_count = await cli.run_cleanup_cli(
            _args(execute=True, include_give_up=True), session_factory=sm
        )
        assert reset_count == 0
        assert report is not None and report.deleted == 1
        assert fake.deleted == [v.voice_id]
    _run(_t())


def test_run_cli_execute_skips_when_worker_unavailable(monkeypatch):
    """--execute 但 worker 未配置 → sweep_once fail-fast → report None，不删。"""
    import mainland_voice_worker as mvw
    monkeypatch.setattr(mvw, "is_mainland_voice_worker_config_ready", lambda s: False)

    async def _t():
        sm = await _make_session()
        v = _mk_voice()
        await _insert(sm, v)
        report, reset_count = await cli.run_cleanup_cli(_args(execute=True), session_factory=sm)
        assert report is None, "worker 不可用 → skip"
        row = await _get(sm, v.id)
        assert row.expired_at is None and row.cleanup_run_id is None
    _run(_t())


# ---------------------------------------------------------------------------
# sweep_once include_give_up passthrough（dry-run，无需 worker）
# ---------------------------------------------------------------------------


def test_sweep_once_include_give_up_passthrough(tmp_path, monkeypatch):
    monkeypatch.setenv("AIVIDEOTRANS_RUNTIME_LOGS_DIR", str(tmp_path))
    import express_voice_cleanup_sweeper as swp

    async def _t():
        sm = await _make_session()
        v = _mk_voice(cleanup_attempts=svc.MAX_CLEANUP_ATTEMPTS)
        await _insert(sm, v)
        # 默认（include_give_up=False）→ dry-run 选不到 give-up 行
        r1 = await swp.sweep_once(session_factory=sm, dry_run=True, include_give_up=False)
        assert r1.selected == []
        # include_give_up=True → 选到
        r2 = await swp.sweep_once(session_factory=sm, dry_run=True, include_give_up=True)
        assert r2.selected == [v.voice_id]
    _run(_t())


# ---------------------------------------------------------------------------
# CLI 不持 internal-key / browser（server-side script）
# ---------------------------------------------------------------------------


def test_cli_no_internal_key_or_browser():
    """CLI 直接走 DB + worker 配置，不持 X-Internal-Key / browser session / HTTP 客户端。"""
    src = (_GATEWAY / "cleanup_temp_voices_cli.py").read_text(encoding="utf-8")
    low = src.lower()
    for bad in ("x-internal-key", "internal_api_key", "cookie", "session_token", "requests.post"):
        assert bad not in low, f"CLI 不应出现 {bad!r}（不是 HTTP/browser 客户端）"


def test_cli_imports_are_db_and_cleanup_only():
    """CLI 顶层只 import stdlib（argparse/asyncio/logging）；DB/sweeper/service 延迟 import。"""
    tree = ast.parse((_GATEWAY / "cleanup_temp_voices_cli.py").read_text(encoding="utf-8"))
    top_level = set()
    for node in tree.body:  # 只看模块顶层
        if isinstance(node, ast.Import):
            for a in node.names:
                top_level.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
    assert top_level <= {"__future__", "argparse", "asyncio", "logging"}, (
        f"CLI 顶层 import 应只 stdlib，实际 {top_level}"
    )
