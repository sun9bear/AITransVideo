"""T10 — POST /gateway/anonymous-preview/claim（匿名预览→登录认领）端点测试.

plan 2026-06-15-anonymous-preview-claim-binding-plan.md §5/§8.

直接调用端点函数（绕过 TestClient），所有外部面（CSRF/auth/admin-gate/限频/
DB）注入 fake，逐门断言安全不变量：CSRF、显式 None→401、cookie-bearer no-op、
统一 200 no-op（防探测，无 409）、单条 RETURNING 原子绑定、session 失败回滚
（无半状态）、双层 HMAC 派生、红线（不改 jobs.user_id / 不触结算 / 不触发 clone）。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
_GATEWAY = str(_REPO / "gateway")
_SRC = str(_REPO / "src")
for _p in (_GATEWAY, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 数据库 stub：setdefault（绝不替换——替换会破坏全套 dependency_overrides 身份，
# 见 memory feedback_test_database_stub_convention）。让本文件可单跑绿。
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
_fake_database.init_db = MagicMock()
sys.modules.setdefault("database", _fake_database)

import anonymous_preview_api as api  # noqa: E402

from sqlalchemy import Column, DateTime, String  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: E402
from sqlalchemy.orm import declarative_base  # noqa: E402

_Base = declarative_base()


class _FakeRecordModel(_Base):
    """带 claim_user_id 列（migration 040）的假 record 模型——使
    ``UPDATE ... SET claim_user_id=... RETURNING preview_id`` 表达式可构造。"""

    __tablename__ = "records_fake_t10"
    preview_id = Column(String, primary_key=True)
    session_id = Column(String)
    status = Column(String)
    claim_user_id = Column(UUID(as_uuid=True))
    audit = Column(JSONB)
    expires_at = Column(DateTime(timezone=True))


class _FakeSessionModel(_Base):
    __tablename__ = "sessions_fake_t10"
    session_id_hash = Column(String, primary_key=True)
    claim_user_id = Column(UUID(as_uuid=True))
    expires_at = Column(DateTime(timezone=True))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _user(uid="user-1"):
    return SimpleNamespace(id=uid)


def _request(cookies=None):
    req = MagicMock()
    req.cookies = {"avt_anon": "rawtok"} if cookies is None else cookies
    req.headers = {}
    return req


def _body(resp):
    return json.loads(resp.body)


def _db_bind(*, bound=(("p1",),), won=("h",)):
    """端点绑定阶段的 db（限频已被 patch 掉，故只有 record + session 两次 execute）。

    record UPDATE → .all() 返回 bound（[]=无 eligible）；
    session UPDATE → .first() 返回 won（None=被他人占/竞争失败）。
    """
    db = MagicMock()
    rec = MagicMock()
    rec.all = MagicMock(return_value=list(bound))
    sess = MagicMock()
    sess.first = MagicMock(return_value=won)
    db.execute = AsyncMock(side_effect=[rec, sess])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture()
def wired(monkeypatch):
    """patch 所有外部面，默认 happy（flag on / CSRF pass / 限频不触发）。"""
    monkeypatch.setattr(api, "AnonymousPreviewRecord", _FakeRecordModel)
    monkeypatch.setattr(api, "AnonymousSession", _FakeSessionModel)
    monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)
    monkeypatch.setattr(api, "_claim_admin_enabled", lambda: True)
    monkeypatch.setattr(api, "_claim_rate_limited", AsyncMock(return_value=False))
    monkeypatch.setattr(
        api.settings, "anonymous_preview_hash_secret", "x" * 32, raising=False
    )
    return monkeypatch


# ---------------------------------------------------------------------------
# 1. happy path + 幂等
# ---------------------------------------------------------------------------


def test_claim_happy_binds_records(wired):
    db = _db_bind(bound=[("p1",), ("p2",)], won=("h",))
    resp = _run(api.anonymous_preview_claim(_request(), db, _user()))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["claimed"] is True
    assert body["count"] == 2
    assert set(body["preview_ids"]) == {"p1", "p2"}
    db.commit.assert_awaited()  # 成功 → commit
    db.rollback.assert_not_awaited()


def test_claim_success_rotates_anonymous_cookie(wired):
    db = _db_bind(bound=[("p1",)], won=("h",))
    resp = _run(api.anonymous_preview_claim(_request(), db, _user()))
    assert resp.status_code == 200

    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert "avt_anon=" in set_cookie
    assert "max-age=0" in set_cookie or "expires=" in set_cookie
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie


def test_claim_idempotent_same_user(wired):
    # 本人重复认领：条件含 (claim_user_id IS NULL OR =本人) → record 仍命中 → claimed:true
    db = _db_bind(bound=[("p1",)], won=("h",))
    resp = _run(api.anonymous_preview_claim(_request(), db, _user()))
    assert resp.status_code == 200
    assert _body(resp)["claimed"] is True


# ---------------------------------------------------------------------------
# 2. 门：CSRF / admin gate / auth / cookie
# ---------------------------------------------------------------------------


def test_claim_csrf_rejected(wired):
    def _raise(_r):
        raise RuntimeError("cross-origin")

    wired.setattr(api, "require_same_origin_state_change", _raise)
    db = _db_bind()
    resp = _run(api.anonymous_preview_claim(_request(), db, _user()))
    assert resp.status_code == 403
    assert _body(resp)["error"] == "csrf_origin_rejected"
    db.execute.assert_not_awaited()  # CSRF 在任何 DB 前


def test_claim_flag_off_is_noop(wired):
    wired.setattr(api, "_claim_admin_enabled", lambda: False)
    db = _db_bind()
    resp = _run(api.anonymous_preview_claim(_request(), db, _user()))
    assert resp.status_code == 200
    assert _body(resp) == {"claimed": False, "count": 0}
    db.execute.assert_not_awaited()  # flag off → inert，无 DB 写


def test_claim_flag_off_noop_even_unauthenticated(wired):
    """flag off → 即便未登录也只是 200 no-op（端点 inert，不 401）。"""
    wired.setattr(api, "_claim_admin_enabled", lambda: False)
    db = _db_bind()
    resp = _run(api.anonymous_preview_claim(_request(), db, None))
    assert resp.status_code == 200
    assert _body(resp)["claimed"] is False


def test_claim_unauthenticated_401_when_flag_on(wired):
    """flag ON + 未登录（user=None）→ 显式 401（plan v2 #2，不依赖 require_auth）。"""
    db = _db_bind()
    resp = _run(api.anonymous_preview_claim(_request(), db, None))
    assert resp.status_code == 401
    db.execute.assert_not_awaited()


def test_claim_no_avt_anon_cookie_noop(wired):
    db = _db_bind()
    resp = _run(api.anonymous_preview_claim(_request(cookies={}), db, _user()))
    assert resp.status_code == 200
    assert _body(resp) == {"claimed": False, "count": 0}
    db.execute.assert_not_awaited()  # 无 cookie → 读侧 miss，不进 DB


# ---------------------------------------------------------------------------
# 3. 统一 200 no-op（防探测，无 409）+ 半状态回滚
# ---------------------------------------------------------------------------


def test_claim_no_eligible_record_noop(wired):
    """无 eligible record（无该 session record / 全过期 / 全被他人占）→ 200 no-op，
    不 commit。安全由条件 UPDATE 保证（plan v3.1 #3）。"""
    db = _db_bind(bound=[], won=("h",))
    resp = _run(api.anonymous_preview_claim(_request(), db, _user()))
    assert resp.status_code == 200
    assert _body(resp) == {"claimed": False, "count": 0}
    db.rollback.assert_awaited()
    db.commit.assert_not_awaited()  # 无绑定 → 绝不 commit


def test_claim_other_user_owned_is_noop_not_409(wired):
    """已被他人认领的 record/session：条件 UPDATE 命中 0 行 → 统一 200 {claimed:false}，
    **非 409**（防探测）；绝不改写他人绑定（由 WHERE 保证）。"""
    db = _db_bind(bound=[], won=("h",))
    resp = _run(api.anonymous_preview_claim(_request(), db, _user()))
    assert resp.status_code == 200  # 不是 409
    assert _body(resp)["claimed"] is False
    db.commit.assert_not_awaited()


def test_claim_session_lock_loser_rolls_back_no_half_state(wired):
    """record 绑定成功但 session 认领锁失败（被他人占的竞态）→ 整事务 rollback
    （含 record 绑定）→ 绝无 session-claimed-但-record-未绑半状态（plan v3.1 #1）。"""
    db = _db_bind(bound=[("p1",)], won=None)
    resp = _run(api.anonymous_preview_claim(_request(), db, _user()))
    assert resp.status_code == 200
    assert _body(resp)["claimed"] is False
    db.rollback.assert_awaited()
    db.commit.assert_not_awaited()  # session 锁失败 → 整体回滚，不 commit


def test_claim_db_write_failure_503(wired):
    """DB 写异常 → 503 retryable（非 200，避免用户误以为已绑定，plan §5.3.7）。"""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("pg down"))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    resp = _run(api.anonymous_preview_claim(_request(), db, _user()))
    assert resp.status_code == 503
    db.rollback.assert_awaited()


# ---------------------------------------------------------------------------
# 4. 双层 HMAC 派生（CodeX #1）：record 用 sess:+hash，session 用裸 hash
# ---------------------------------------------------------------------------


def test_claim_record_lookup_uses_sess_prefixed_key(wired):
    """证明 record 绑定用 hash_scope_key("sess:"+session_id_hash) 派生，
    **非**裸 avt_anon hash（错用裸 hash 恒 0 行，2026-06-11 冒烟事故）。"""
    calls = []

    def _spy(value, *, secret):
        calls.append(value)
        return "K:" + value

    wired.setattr(api, "hash_scope_key", _spy)
    db = _db_bind(bound=[("p1",)], won=("h",))
    resp = _run(api.anonymous_preview_claim(_request(), db, _user()))
    assert resp.status_code == 200
    # 第一次 = 裸 avt_anon；第二次 = "sess:"+第一次结果（record 侧派生）
    assert calls[0] == "rawtok"
    assert calls[1] == "sess:K:rawtok"
    assert calls[1].startswith("sess:")


# ---------------------------------------------------------------------------
# 5. 限频 helper（fail-open）
# ---------------------------------------------------------------------------


def test_rate_limited_empty_ip_fails_open(monkeypatch):
    monkeypatch.setattr(api, "extract_client_ip", lambda r: "")
    db = MagicMock()
    db.execute = AsyncMock()
    assert _run(api._claim_rate_limited(db, _request())) is False
    db.execute.assert_not_awaited()  # 空 IP → 跳过限频


def test_rate_limited_store_error_fails_open(monkeypatch):
    monkeypatch.setattr(api, "extract_client_ip", lambda r: "1.2.3.4")
    monkeypatch.setattr(
        api.settings, "anonymous_preview_hash_secret", "x" * 32, raising=False
    )
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("counter down"))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    assert _run(api._claim_rate_limited(db, _request())) is False  # fail-open
    db.rollback.assert_awaited()


def test_rate_limited_at_cap_returns_true(monkeypatch):
    monkeypatch.setattr(api, "extract_client_ip", lambda r: "1.2.3.4")
    monkeypatch.setattr(
        api.settings, "anonymous_preview_hash_secret", "x" * 32, raising=False
    )
    db = MagicMock()
    rate = MagicMock()
    rate.fetchone = MagicMock(return_value=None)  # WHERE count<cap 不满足 → 无行
    db.execute = AsyncMock(return_value=rate)
    db.commit = AsyncMock()
    assert _run(api._claim_rate_limited(db, _request())) is True


def test_rate_limited_under_cap_returns_false(monkeypatch):
    monkeypatch.setattr(api, "extract_client_ip", lambda r: "1.2.3.4")
    monkeypatch.setattr(
        api.settings, "anonymous_preview_hash_secret", "x" * 32, raising=False
    )
    db = MagicMock()
    rate = MagicMock()
    rate.fetchone = MagicMock(return_value=(3,))
    db.execute = AsyncMock(return_value=rate)
    db.commit = AsyncMock()
    assert _run(api._claim_rate_limited(db, _request())) is False


def test_claim_rate_limited_returns_429(wired):
    wired.setattr(api, "_claim_rate_limited", AsyncMock(return_value=True))
    db = _db_bind()
    resp = _run(api.anonymous_preview_claim(_request(), db, _user()))
    assert resp.status_code == 429
    db.execute.assert_not_awaited()  # 限频拦截在绑定前


# ---------------------------------------------------------------------------
# 6. admin gate 严格 is True（fail-closed）
# ---------------------------------------------------------------------------


def test_admin_gate_strict_is_true(monkeypatch):
    """raw-JSON 字符串 "true" / 数字 1 不得开启（strict is True，防误开）。"""
    for val in ("true", "1", 1, "false", None, 0):
        monkeypatch.setitem(
            sys.modules,
            "admin_settings",
            SimpleNamespace(
                load_settings=lambda v=val: SimpleNamespace(
                    anonymous_preview_claim_enabled=v
                )
            ),
        )
        assert api._claim_admin_enabled() is False

    monkeypatch.setitem(
        sys.modules,
        "admin_settings",
        SimpleNamespace(
            load_settings=lambda: SimpleNamespace(
                anonymous_preview_claim_enabled=True
            )
        ),
    )
    assert api._claim_admin_enabled() is True


def test_admin_gate_fail_closed_on_error(monkeypatch):
    def _boom():
        raise RuntimeError("disk")

    monkeypatch.setitem(
        sys.modules, "admin_settings", SimpleNamespace(load_settings=_boom)
    )
    assert api._claim_admin_enabled() is False


# ---------------------------------------------------------------------------
# 7. 红线守卫（plan §5.4）：不改 jobs.user_id / 不触结算 / 不触发 clone
#    —— 扫 AST 标识符（Name/Attribute/import 模块），**不**含 docstring/注释
#    （docstring 是 ast.Constant，不进标识符集），避免 prose 提及红线名误报。
# ---------------------------------------------------------------------------


def _claim_identifiers() -> tuple[set[str], set[str]]:
    """返回 (name+attr+module 标识符全集, 其小写形态集) —— 仅取 handler 三函数
    的可执行 AST，排除文档串/注释。"""
    ids: set[str] = set()
    for fn in (
        api.anonymous_preview_claim,
        api._claim_rate_limited,
        api._claim_admin_enabled,
    ):
        tree = ast.parse(inspect.getsource(fn))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                ids.add(node.id)
            elif isinstance(node, ast.Attribute):
                ids.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                ids.add(node.module or "")
                ids.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                ids.update(a.name for a in node.names)
    return ids, {i.lower() for i in ids}


_IDS, _IDS_LOWER = _claim_identifiers()


def test_claim_handler_never_references_job_model():
    """红线：claim 绝不触 Job 模型（Model A 不迁移 jobs 所有权，不改 jobs.user_id）。"""
    assert "Job" not in _IDS, "claim 红线：不得引用 Job 模型"
    assert "jobs" not in _IDS, "claim 红线：不得引用 jobs"


def test_claim_handler_never_settles_or_mirrors():
    for banned in ("settle", "mirror_job", "shadow_capture", "shadow_release"):
        hits = [i for i in _IDS_LOWER if banned in i]
        assert not hits, f"claim 红线：不得触结算（命中标识符 {hits}）"


def test_claim_handler_never_triggers_clone():
    for banned in (
        "tts_generator",
        "build_smart_clone",
        "synthesize_voiceclone",
        "maybe_run_express_auto_clone",
        "minimax",
        "cosyvoice",
    ):
        hits = [i for i in _IDS_LOWER if banned in i]
        assert not hits, f"claim 红线：不得触发 clone（命中标识符 {hits}）"


def test_gateway_file_imports_no_clone_settle_mirror():
    """AST 扫 anonymous_preview_api.py 的 import：不得拉入 clone/settle/mirror 模块。"""
    src = (Path(_GATEWAY) / "anonymous_preview_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = (
        "voice_clone",
        "user_voice",
        "minimax",
        "cosyvoice",
        "tts_generator",
        "settle",
        "mirror_job",
        "smart_clone",
    )
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
        for m in mods:
            low = m.lower()
            for b in banned:
                assert b not in low, f"anonymous_preview_api 不应 import {m!r}（含 {b!r}）"


# ---------------------------------------------------------------------------
# 8. 真实 SQL 覆盖（对抗复审 HIGH 确认）：mock execute 不验真 SQL → 补两层
#    ① 捕获 handler **真实** statement 对象编译到 Postgres dialect（验构造正确）；
#    ② 真实 SQLite 引擎执行同构条件 WHERE（验越权/status/expiry 过滤真语义）。
# ---------------------------------------------------------------------------


def _capture_claim_sql():
    """用**真实** ORM 模型（不 patch models）跑 handler，捕获它实际构造的
    record/session UPDATE statement，编译到 Postgres dialect 返回 SQL 文本。

    这测的是 handler **真正发出的** statement（无重复构造、无 fake 模型漂移），
    捕获顺序 = [record UPDATE, session UPDATE]（限频已 patch 掉）。"""
    import anonymous_preview_api as _api
    from sqlalchemy.dialects import postgresql

    captured = []
    rec = MagicMock()
    rec.all = MagicMock(return_value=[("p1",)])
    sess = MagicMock()
    sess.first = MagicMock(return_value=("h",))
    results = iter([rec, sess])

    async def _exec(stmt, *a, **k):
        captured.append(stmt)
        return next(results)

    db = MagicMock()
    db.execute = _exec
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    import pytest as _pt

    mp = _pt.MonkeyPatch()
    try:
        # 只 patch CSRF / admin / 限频，**保留真实 AnonymousPreviewRecord /
        # AnonymousSession 模型**（验真表名 + 真列类型 JSONB/UUID 的渲染）。
        mp.setattr(_api, "require_same_origin_state_change", lambda r: None)
        mp.setattr(_api, "_claim_admin_enabled", lambda: True)
        mp.setattr(_api, "_claim_rate_limited", AsyncMock(return_value=False))
        mp.setattr(
            _api.settings, "anonymous_preview_hash_secret", "x" * 32, raising=False
        )
        _run(_api.anonymous_preview_claim(_request(), db, _user("11111111-1111-1111-1111-111111111111")))
    finally:
        mp.undo()

    return [
        str(s.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for s in captured
    ]


def test_record_update_sql_renders_pg_constructs():
    """真实 record UPDATE 编译到 PG：jsonb_set/coalesce/greatest/条件 WHERE/
    status 过滤/RETURNING 全部正确渲染（v3.1 #2 防 NULL + 延长 + 防越权）。"""
    sqls = _capture_claim_sql()
    rec_sql = sqls[0].lower()
    assert "update anonymous_preview_records" in rec_sql
    assert "jsonb_set(coalesce(" in rec_sql, "audit 必须 jsonb_set(COALESCE(...))（防 NULL）"
    assert "'{}'::jsonb" in sqls[0], "COALESCE 兜底空 jsonb"
    assert "to_jsonb(" in rec_sql, "claimed_at 经 to_jsonb"
    assert "greatest(anonymous_preview_records.expires_at" in rec_sql, "expires_at 延长用 GREATEST"
    assert "claim_user_id is null or" in rec_sql, "条件 WHERE 防越权/幂等"
    assert "status = 'ready_for_mode'" in rec_sql, "只绑 ready 契约状态"
    assert "expires_at > " in rec_sql, "不绑已过期"
    assert "returning anonymous_preview_records.preview_id" in rec_sql


def test_session_update_sql_renders_pg_constructs():
    """真实 session UPDATE 编译到 PG：裸 session_id_hash PK + 条件 + GREATEST + RETURNING。"""
    sqls = _capture_claim_sql()
    sess_sql = sqls[1].lower()
    assert "update anonymous_sessions" in sess_sql
    assert "session_id_hash =" in sess_sql, "session 用裸 hash PK（非 sess: 派生）"
    assert "claim_user_id is null or" in sess_sql
    assert "anonymous_sessions.expires_at > " in sess_sql, "不认领已过期 session"
    assert "greatest(anonymous_sessions.expires_at" in sess_sql
    assert "returning anonymous_sessions.session_id_hash" in sess_sql


def test_rate_limit_upsert_sql_matches_proven_contract(monkeypatch):
    """限频 upsert 用与 try_acquire 同款实证 SQL：ON CONFLICT(scope,scope_key,mode,
    usage_date) + WHERE count<:cap + RETURNING count，落 anonymous_preview_daily_usage。"""
    monkeypatch.setattr(api, "extract_client_ip", lambda r: "1.2.3.4")
    monkeypatch.setattr(
        api.settings, "anonymous_preview_hash_secret", "x" * 32, raising=False
    )
    captured = {}

    async def _exec(stmt, params=None, *a, **k):
        captured["sql"] = str(stmt)
        captured["params"] = params
        row = MagicMock()
        row.fetchone = MagicMock(return_value=(1,))
        return row

    db = MagicMock()
    db.execute = _exec
    db.commit = AsyncMock()
    _run(api._claim_rate_limited(db, _request()))
    sql = captured["sql"].lower()
    assert "insert into anonymous_preview_daily_usage" in sql
    assert "on conflict (scope, scope_key, mode, usage_date)" in sql
    assert "where anonymous_preview_daily_usage.count < :cap" in sql
    assert "returning count" in sql
    # 参数：mode='claim' 与 intake 计数器隔离；cap = 模块常量
    assert captured["params"]["mode"] == api._CLAIM_RATE_MODE == "claim"
    assert captured["params"]["cap"] == api._CLAIM_RATE_CAP_PER_IP


def test_where_semantics_on_real_sqlite_engine():
    """在**真实 SQLite 引擎**执行与 handler 同构的条件 WHERE UPDATE，证明越权保护/
    status 过滤/expiry 过滤/session 隔离的**真实语义**（非 mock 断言）。SQLite 不支持
    jsonb_set/greatest，故 SET 只设 claim_user_id（安全保证全在 WHERE，与 SET 无关）；
    用 SELECT 后置态验证（不依赖 SQLite RETURNING）。"""
    from datetime import datetime, timedelta
    from sqlalchemy import (
        Column,
        DateTime,
        MetaData,
        String,
        Table,
        create_engine,
        insert,
        or_,
        select,
        update,
    )

    eng = create_engine("sqlite://")
    md = MetaData()
    t = Table(
        "apr_real",
        md,
        Column("preview_id", String, primary_key=True),
        Column("session_id", String),
        Column("status", String),
        Column("claim_user_id", String),
        Column("expires_at", DateTime),
    )
    md.create_all(eng)

    now = datetime(2026, 6, 15, 12, 0, 0)
    future = now + timedelta(hours=12)
    past = now - timedelta(hours=1)
    READY = "ready_for_mode"
    A, B = "user-A", "user-B"
    rows = [
        ("p_unclaimed", "K", READY, None, future),       # 应绑给 A
        ("p_mine", "K", READY, A, future),               # 本人 → 幂等绑
        ("p_other", "K", READY, B, future),              # 他人占 → 不绑（owner 不变）
        ("p_block", "K", "rejected", None, future),      # block 状态 → 不绑
        ("p_expired", "K", READY, None, past),           # 过期 → 不绑
        ("p_othersession", "X", READY, None, future),    # 别 session → 不绑
    ]
    with eng.begin() as c:
        for pid, sid, st, owner, exp in rows:
            c.execute(
                insert(t).values(
                    preview_id=pid, session_id=sid, status=st,
                    claim_user_id=owner, expires_at=exp,
                )
            )

    # 与 handler 同构的条件 WHERE（SET 只设 claim_user_id；安全在 WHERE）。
    stmt = (
        update(t)
        .where(
            t.c.session_id == "K",
            t.c.expires_at > now,
            t.c.status == READY,
            or_(t.c.claim_user_id.is_(None), t.c.claim_user_id == A),
        )
        .values(claim_user_id=A)
    )
    with eng.begin() as c:
        c.execute(stmt)

    with eng.connect() as c:
        def owner(pid):
            return c.execute(
                select(t.c.claim_user_id).where(t.c.preview_id == pid)
            ).scalar()

        # 只绑可认领 + 本人；越权/block/过期/别 session 全不动
        assert owner("p_unclaimed") == A      # 未认领 → 绑给 A
        assert owner("p_mine") == A           # 本人 → 幂等
        assert owner("p_other") == B          # ★ 越权保护：他人 owner 绝不被改写
        assert owner("p_block") is None       # block 状态未绑
        assert owner("p_expired") is None     # 过期未绑
        assert owner("p_othersession") is None  # 别 session 未绑


# ---------------------------------------------------------------------------
# 9. 前端认领重试保留守卫（CodeX P2）：可重试失败不得清 hint（永久丢失）。
#    项目无 JS test runner → Python 静态扫描（沿用 admin sync guard 约定）。
# ---------------------------------------------------------------------------

_CLAIM_TS = _REPO / "frontend-next" / "src" / "lib" / "api" / "claim.ts"
_APP_SHELL_TSX = _REPO / "frontend-next" / "src" / "components" / "app-shell.tsx"


def test_claim_ts_keeps_hint_on_retryable_failure():
    """claim.ts 必须：① 非 2xx/异常路径返回 settled:false（可重试）；② 仅在 settled
    时清 hint，**绝不**用无条件 finally 清——否则 503/429/网络失败后用户永久丢失认领。"""
    assert _CLAIM_TS.exists(), f"claim.ts 不存在: {_CLAIM_TS}"
    src = _CLAIM_TS.read_text(encoding="utf-8")

    # ① 可重试标志存在（非 ok / catch 返回 settled:false）
    assert re.search(r"settled:\s*false", src), (
        "claimAnonymousPreview 非 2xx/异常路径必须返回 settled:false（可重试）"
    )
    # ② 清 hint 受 settled 守护
    assert re.search(r"if\s*\(\s*settled\s*\)", src), (
        "maybeClaimAnonPreviewAfterLogin 必须 `if (settled)` 守护 clearAnonClaimHint"
    )
    assert "clearAnonClaimHint()" in src
    # ③ 绝不无条件 finally 清 hint（CodeX P2 的反模式）
    assert not re.search(r"finally\s*\{\s*clearAnonClaimHint", src), (
        "禁止无条件 finally { clearAnonClaimHint }——可重试失败会被清成永久丢失"
    )


def test_claim_ts_bounds_post_auth_claim_fetch():
    assert _CLAIM_TS.exists(), f"claim.ts 不存在: {_CLAIM_TS}"
    src = _CLAIM_TS.read_text(encoding="utf-8")

    assert "CLAIM_REQUEST_TIMEOUT_MS" in src
    assert "AbortController" in src
    assert "signal: controller.signal" in src
    assert "window.setTimeout" in src
    assert "window.clearTimeout" in src


def test_app_shell_retries_pending_claim_after_auth_redirect():
    assert _APP_SHELL_TSX.exists(), f"app-shell.tsx 不存在: {_APP_SHELL_TSX}"
    src = _APP_SHELL_TSX.read_text(encoding="utf-8")

    assert "maybeClaimAnonPreviewAfterLogin" in src
    assert re.search(r"useEffect\(\(\)\s*=>\s*\{[\s\S]*user[\s\S]*maybeClaimAnonPreviewAfterLogin", src)
