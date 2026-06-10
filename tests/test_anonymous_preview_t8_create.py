"""APF P0 T8b — POST /{preview_id}/create 编排测试（plan AD-7/AD-8）。

直接调用端点函数（绕过 TestClient），所有外部面（session/record/DB/Job API/
probe/admin）注入 fake，逐门断言 fail-closed 矩阵与零结算所有权语义。
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
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

import anonymous_preview_api as api  # noqa: E402
import anonymous_preview_probe as probe_mod  # noqa: E402
from anonymous_session import AnonymousSessionContext  # noqa: E402

# ---------------------------------------------------------------------------
# 可构造 SQLAlchemy 表达式的最小假模型。
# T7 测试文件会向 sys.modules 注入无列的 models stub（导致 Job.col 表达式
# 构造失败）；本文件不依赖导入顺序——fixture 统一换成这些 declarative 假模型，
# 单跑 / 与 T7 并跑行为一致。db.execute 全程 mock，不需要真表。
# ---------------------------------------------------------------------------

from sqlalchemy import Boolean, Column, String  # noqa: E402
from sqlalchemy.orm import declarative_base  # noqa: E402

_Base = declarative_base()


class _FakeJobModel(_Base):
    __tablename__ = "jobs_fake_t8"
    job_id = Column(String, primary_key=True)
    user_id = Column(String)
    status = Column(String)
    is_anonymous_preview = Column(Boolean)
    source_type = Column(String)
    source_ref = Column(String)
    source_content_hash = Column(String)
    title = Column(String)
    speakers = Column(String)
    service_mode = Column(String)
    tts_provider = Column(String)
    requires_review = Column(Boolean)
    voice_clone_enabled = Column(Boolean)
    voice_strategy = Column(String)
    plan_code_snapshot = Column(String)
    role_snapshot = Column(String)


class _FakeUserModel(_Base):
    __tablename__ = "users_fake_t8"
    id = Column(String, primary_key=True)
    email = Column(String)


class _FakeRecordModel(_Base):
    __tablename__ = "records_fake_t8"
    preview_id = Column(String, primary_key=True)
    job_id = Column(String)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


VALID_CONSENT = {"anonymous_consent": {"voice_rights_confirmed": True}}


def _ctx() -> AnonymousSessionContext:
    try:
        return AnonymousSessionContext(session_id_hash="sess-hash")
    except TypeError:
        ctx = object.__new__(AnonymousSessionContext)
        object.__setattr__(ctx, "session_id_hash", "sess-hash")
        return ctx


def _record(tmp_path: Path, **overrides) -> SimpleNamespace:
    teaser = tmp_path / "teaser_x.mp4"
    teaser.write_bytes(b"fake")
    base = dict(
        preview_id="p1",
        job_id=None,
        status="ready_for_mode",
        source_hash="h" * 16,
        mode="free",
        claim_token_placeholder=None,
        audit={"teaser_path": str(teaser)},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _request(body=VALID_CONSENT) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.headers = {}
    return req


def _db(*, in_flight: int = 0, sentinel_id: str | None = "u-sentinel", claim_rows: int = 1):
    db = MagicMock()
    count_result = MagicMock()
    count_result.scalar.return_value = in_flight
    sentinel_result = MagicMock()
    sentinel_result.scalar_one_or_none.return_value = (
        SimpleNamespace(id=sentinel_id) if sentinel_id else None
    )
    claim_result = MagicMock(rowcount=claim_rows)
    db.execute = AsyncMock(side_effect=[count_result, sentinel_result, claim_result])
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """Patch every external seam; return a dict of knobs the tests can tweak."""
    record = _record(tmp_path)
    # 端点在调用期 `from models import Job, User`——把假模型钉进当前的
    # models 模块对象（无论它是 T7 的 stub 还是真模块），保证表达式可构造。
    _models_mod = sys.modules.get("models")
    if _models_mod is None:
        _models_mod = types.ModuleType("models")
        monkeypatch.setitem(sys.modules, "models", _models_mod)
    monkeypatch.setattr(_models_mod, "Job", _FakeJobModel, raising=False)
    monkeypatch.setattr(_models_mod, "User", _FakeUserModel, raising=False)
    monkeypatch.setattr(api, "AnonymousPreviewRecord", _FakeRecordModel)
    monkeypatch.setattr(api, "require_same_origin_state_change", lambda r: None)
    monkeypatch.setattr(api, "require_anonymous_session", AsyncMock(return_value=_ctx()))
    monkeypatch.setattr(
        api, "_get_record_for_session", AsyncMock(side_effect=[record, record])
    )
    monkeypatch.setattr(api, "_get_admin_enabled", lambda: True)
    monkeypatch.setattr(api.settings, "enable_free_tier", True, raising=False)
    monkeypatch.setattr(
        probe_mod,
        "probe_source",
        lambda p: {"ok": True, "duration_seconds": 170.0, "has_audio": True,
                   "container_format": "mp4", "failure_reason": None},
    )
    monkeypatch.setitem(
        sys.modules,
        "admin_settings",
        types.SimpleNamespace(
            load_settings=lambda: SimpleNamespace(anonymous_preview_max_in_flight=2)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "quota",
        types.SimpleNamespace(
            TERMINAL_STATUSES=frozenset({"succeeded", "failed", "cancelled", "purged"})
        ),
    )
    job_api_resp = MagicMock(status_code=202)
    job_api_resp.json.return_value = {"job_id": "job-abc"}
    client = MagicMock()
    client.post = AsyncMock(return_value=job_api_resp)
    monkeypatch.setattr(api, "get_client", lambda: client)
    reset_spy = AsyncMock()
    monkeypatch.setattr(api, "_reset_create_claim", reset_spy)
    return {"record": record, "client": client, "reset": reset_spy, "monkeypatch": monkeypatch}


def _call(db) -> object:
    return _run(api.anonymous_preview_create("p1", _request(), db))


# --- happy path -------------------------------------------------------------


def test_create_happy_path(wired):
    db = _db()
    resp = _call(db)
    assert resp.status_code == 202

    # Job API payload 守卫：白名单字段 + 匿名标记 + 零 clone 字段
    _, kwargs = wired["client"].post.call_args
    payload = kwargs["json"]
    assert payload["anonymous_preview"] is True
    assert payload["service_mode"] == "free"
    assert payload["voice_strategy"] == "preset_mapping"
    assert payload["tts_provider"] == "mimo"
    assert payload["requires_review"] is False
    assert "voice_clone" not in payload and "voiceclone_reference_path" not in payload

    # PG Job 行：sentinel owner + 标记列
    job_row = db.add.call_args[0][0]
    assert job_row.user_id == "u-sentinel"
    assert job_row.is_anonymous_preview is True
    assert job_row.service_mode == "free"

    # record 回写：真实 job_id + claim token 占位 + consent 含权威时间戳
    rec = wired["record"]
    assert rec.job_id == "job-abc"
    assert rec.claim_token_placeholder
    assert rec.audit["anonymous_consent"]["voice_rights_confirmed"] is True
    assert rec.audit["anonymous_consent"]["server_confirmed_at"]
    wired["reset"].assert_not_awaited()


# --- gate matrix ------------------------------------------------------------


def test_create_consent_missing_403(wired):
    db = _db()
    resp = _run(api.anonymous_preview_create("p1", _request(body={}), db))
    assert resp.status_code == 403
    wired["client"].post.assert_not_awaited()


def test_create_consent_coercion_403(wired):
    db = _db()
    resp = _run(
        api.anonymous_preview_create(
            "p1", _request(body={"anonymous_consent": {"voice_rights_confirmed": "true"}}), db
        )
    )
    assert resp.status_code == 403


def test_create_replay_blocked_when_job_exists(wired):
    wired["record"].job_id = "job-old"
    resp = _call(_db())
    assert resp.status_code == 409
    wired["client"].post.assert_not_awaited()


def test_create_requires_ready_status(wired):
    wired["record"].status = "rate_limited"
    resp = _call(_db())
    assert resp.status_code == 409


def test_create_free_tier_env_double_gate(wired):
    wired["monkeypatch"].setattr(api.settings, "enable_free_tier", False, raising=False)
    resp = _call(_db())
    assert resp.status_code == 403
    assert b"free_disabled" in resp.body


def test_create_admin_hot_switch(wired):
    wired["monkeypatch"].setattr(api, "_get_admin_enabled", lambda: False)
    resp = _call(_db())
    assert resp.status_code == 403


def test_create_teaser_missing_409(wired):
    wired["record"].audit = {}
    resp = _call(_db())
    assert resp.status_code == 409
    assert b"teaser_missing" in resp.body


def test_create_unprobeable_teaser_409(wired):
    wired["monkeypatch"].setattr(
        probe_mod,
        "probe_source",
        lambda p: {"ok": False, "duration_seconds": None, "has_audio": False,
                   "container_format": None, "failure_reason": "x"},
    )
    resp = _call(_db())
    assert resp.status_code == 409


def test_create_in_flight_gate_429(wired):
    resp = _call(_db(in_flight=2))
    assert resp.status_code == 429
    wired["client"].post.assert_not_awaited()


def test_create_admin_read_failure_means_zero_capacity(wired):
    wired["monkeypatch"].setitem(
        sys.modules,
        "admin_settings",
        types.SimpleNamespace(load_settings=lambda: (_ for _ in ()).throw(RuntimeError())),
    )
    resp = _call(_db(in_flight=0))
    assert resp.status_code == 429  # fail-closed：容量按 0


def test_create_sentinel_missing_503(wired):
    resp = _call(_db(sentinel_id=None))
    assert resp.status_code == 503
    wired["client"].post.assert_not_awaited()


def test_create_claim_race_409(wired):
    resp = _call(_db(claim_rows=0))
    assert resp.status_code == 409
    wired["client"].post.assert_not_awaited()


def test_create_job_api_failure_resets_claim(wired):
    bad = MagicMock(status_code=500)
    bad.json.return_value = {}
    wired["client"].post = AsyncMock(return_value=bad)
    resp = _call(_db())
    assert resp.status_code == 502
    wired["reset"].assert_awaited_once()


# --- 对抗审核 P1 回归：status 端点不得拿 __creating__ 哨兵去查 Job API -------


def test_status_endpoint_handles_creating_sentinel(wired, monkeypatch):
    rec = wired["record"]
    rec.job_id = "__creating__"
    monkeypatch.setattr(
        api, "_get_record_for_session", AsyncMock(return_value=rec)
    )
    monkeypatch.setattr(api, "_is_record_expired", lambda r: False)
    resp = _run(api.anonymous_preview_status("p1", _request(), MagicMock()))
    assert resp.status_code == 200
    assert b'"stage":"creating"' in resp.body.replace(b" ", b"")
    wired["client"].get.assert_not_called()  # 没有拿哨兵值去打 Job API


def test_pipeline_third_defense_source_guard():
    import pipeline.process as process_module

    src = Path(process_module.__file__).read_text(encoding="utf-8")
    assert "防 clone 第三道防线" in src
    assert "job_voice_strategy = 'preset_mapping'" in src
