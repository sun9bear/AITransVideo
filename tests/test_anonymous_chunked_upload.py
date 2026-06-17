"""匿名档分片上传 A1-A6 测试 — plan 2026-06-11 §9.7 矩阵（r1）。

覆盖：三与门 gate / 匿名身份隔离同形 404 / AD-8 预检 429 / per-IP in-flight
gate（计数 + 字节聚合）/ complete→intake 全链（mock intake，consumed 幂等、
intake 只跑一次）/ intake 失败清理 / 无 cookie 401 / 200MB 上限 413 /
per-session active=1 / consumed 不占活跃额度 / sweeper 匿名 6h TTL 与注册档
24h 并存 / import 守卫。
"""
from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import anonymous_preview_chunked_api as api
import chunked_upload_store as store
from anonymous_session import AnonymousSessionContext
from chunked_upload_store import ChunkedLimits, ChunkedUploadError
from database import get_db

ORIGIN = {"origin": "http://testserver"}
PREFIX = "/gateway/anonymous-preview/chunked"

SESSION_A = AnonymousSessionContext(session_id_hash="a" * 64, raw_token=None, is_new=False)
SESSION_B = AnonymousSessionContext(session_id_hash="b" * 64, raw_token=None, is_new=False)

FAKE_APF = SimpleNamespace(
    anonymous_preview_max_upload_bytes=200 * 1024 * 1024,
    anonymous_preview_cap_per_ip=3,
    anonymous_preview_cap_global_per_day=20,
)

FAKE_INTAKE_PAYLOAD = {
    "preview_id": "pv_test_1",
    "status": "ready_for_mode",
    "status_reason": None,
    "mode": "free",
    "admission_decision": "admitted",
}


def anon_limits(**overrides) -> ChunkedLimits:
    base = dict(
        enabled=True, max_file_mb=200, chunk_mb=64, per_user_active=1,
        per_user_inflight_gb=1, global_inflight_gb=20, daily_per_user_gb=1,
        disk_floor_gb=0, ttl_hours=6, ready_ttl_hours=6,
    )
    base.update(overrides)
    return ChunkedLimits(**base)


async def _fake_get_db():
    yield AsyncMock()


async def _noop_peek(db, request, limits, lane="free"):
    return None


@pytest.fixture()
def uploads_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
    monkeypatch.delenv("AIVIDEOTRANS_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(store, "HARD_MIN_CHUNK_BYTES", 1)
    return tmp_path


def make_client(monkeypatch, *, session=SESSION_A, gates=True, intake=None, lane="free"):
    """构造仅挂匿名分片 router 的 TestClient，全部外部依赖打桩。

    plan 2026-06-12 §A：init 还会经 _resolve_active_lane 锁 lane 进 state，
    测试默认钉成 free（与既有行为等价）；express 用例显式传 lane。
    """
    monkeypatch.setattr(api, "three_gates_open", lambda: gates)
    monkeypatch.setattr(api, "_resolve_active_lane", lambda: lane)
    monkeypatch.setattr(api, "resolve_anonymous_chunked_limits", anon_limits)
    monkeypatch.setattr(api, "resolve_apf_limits", lambda: FAKE_APF)
    monkeypatch.setattr(api, "ad8_peek_precheck", _noop_peek)

    async def _get_or_create(request, response, db):
        return session

    async def _require(request, db):
        return session

    monkeypatch.setattr(api, "get_or_create_anonymous_session", _get_or_create)
    monkeypatch.setattr(api, "require_anonymous_session", _require)

    if intake is not None:
        monkeypatch.setattr(api, "_run_intake_single_commit", intake)

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_db] = _fake_get_db
    return TestClient(app)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def do_init(client, data: bytes, chunk_size=4, name="v.mp4", size=None):
    return client.post(
        f"{PREFIX}/init",
        json={
            "size": size if size is not None else len(data),
            "sha256": sha(data),
            "chunk_size": chunk_size,
            "file_name": name,
        },
        headers=ORIGIN,
    )


def upload_all_parts(client, uid: str, data: bytes, chunk_size=4) -> None:
    total = (len(data) + chunk_size - 1) // chunk_size
    for n in range(total):
        piece = data[n * chunk_size:(n + 1) * chunk_size]
        r = client.put(
            f"{PREFIX}/{uid}/part/{n}",
            content=piece,
            headers={**ORIGIN, "x-chunk-sha256": sha(piece)},
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 三与门 gate（任一关 → A1/A2/A3 同形 404）
# ---------------------------------------------------------------------------


def test_three_gates_closed_init_part_complete_404(uploads_env, monkeypatch):
    client = make_client(monkeypatch, gates=False)
    data = b"0123456789"
    assert do_init(client, data).status_code == 404
    r_part = client.put(
        f"{PREFIX}/{'0' * 32}/part/0",
        content=b"x",
        headers={**ORIGIN, "x-chunk-sha256": sha(b"x")},
    )
    assert r_part.status_code == 404
    assert r_part.json() == {"error": "not_found"}
    r_complete = client.post(f"{PREFIX}/{'0' * 32}/complete", headers=ORIGIN)
    assert r_complete.status_code == 404
    assert r_complete.json() == {"error": "not_found"}


def test_limits_env_flag_off_404(uploads_env, monkeypatch):
    client = make_client(monkeypatch)
    import config as gw_config

    monkeypatch.setattr(gw_config.settings, "enable_anonymous_preview", False)
    assert client.get(f"{PREFIX}/limits").status_code == 404


def test_limits_returns_threshold_and_caps(uploads_env, monkeypatch):
    client = make_client(monkeypatch)
    import config as gw_config

    monkeypatch.setattr(gw_config.settings, "enable_anonymous_preview", True)
    r = client.get(f"{PREFIX}/limits")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["threshold_mb"] == 95
    assert body["max_file_mb"] == 200


# ---------------------------------------------------------------------------
# CSRF / 会话
# ---------------------------------------------------------------------------


def test_init_without_origin_403(uploads_env, monkeypatch):
    client = make_client(monkeypatch)
    r = client.post(
        f"{PREFIX}/init",
        json={"size": 4, "sha256": "a" * 64, "chunk_size": 4, "file_name": "x"},
    )
    assert r.status_code == 403
    assert r.json() == {"error": "csrf_origin_rejected"}


def test_no_session_401(uploads_env, monkeypatch):
    client = make_client(monkeypatch)

    async def _reject(request, db):
        return JSONResponse(status_code=401, content={"error": "anonymous_session_required"})

    monkeypatch.setattr(api, "require_anonymous_session", _reject)
    uid = "0" * 32
    assert client.get(f"{PREFIX}/{uid}/status").status_code == 401
    assert client.post(f"{PREFIX}/{uid}/complete", headers=ORIGIN).status_code == 401
    r_part = client.put(
        f"{PREFIX}/{uid}/part/0",
        content=b"x",
        headers={**ORIGIN, "x-chunk-sha256": sha(b"x")},
    )
    assert r_part.status_code == 401


# ---------------------------------------------------------------------------
# AD-8 预检 / 上限
# ---------------------------------------------------------------------------


def test_ad8_peek_reject_blocks_init(uploads_env, monkeypatch):
    client = make_client(monkeypatch)

    async def _reject_peek(db, request, limits, lane="free"):
        return JSONResponse(status_code=429, content={"error": "rate_limited"})

    monkeypatch.setattr(api, "ad8_peek_precheck", _reject_peek)
    r = do_init(client, b"0123456789")
    assert r.status_code == 429
    assert r.json()["error"] == "rate_limited"


def test_over_200mb_413(uploads_env, monkeypatch):
    client = make_client(monkeypatch)
    r = do_init(client, b"xx", size=201 * 1024 * 1024)
    assert r.status_code == 413
    assert r.json()["error"] == "over_limit"


def test_per_session_active_capped_at_1(uploads_env, monkeypatch):
    client = make_client(monkeypatch)
    assert do_init(client, b"data-one-x").status_code == 200
    r2 = do_init(client, b"data-two-y")
    assert r2.status_code == 429
    assert r2.json()["error"] == "too_many_active"


# ---------------------------------------------------------------------------
# per-IP in-flight gate（store 层，跨 session 聚合）
# ---------------------------------------------------------------------------


def test_per_ip_inflight_count_gate(uploads_env):
    limits = anon_limits()
    ip_hash = "iphash-1"
    for i in range(3):
        store.init_upload(
            user_id=f"anon:s{i}",
            declared_size=10,
            declared_sha256=sha(f"d{i}".encode()),
            chunk_size=4,
            file_name="v.mp4",
            limits=limits,
            owner_scope=store.OWNER_SCOPE_ANONYMOUS,
            client_ip_hash=ip_hash,
            per_ip_active=3,
        )
    with pytest.raises(ChunkedUploadError) as exc:
        store.init_upload(
            user_id="anon:s9",
            declared_size=10,
            declared_sha256=sha(b"d9"),
            chunk_size=4,
            file_name="v.mp4",
            limits=limits,
            owner_scope=store.OWNER_SCOPE_ANONYMOUS,
            client_ip_hash=ip_hash,
            per_ip_active=3,
        )
    assert exc.value.status_code == 429
    assert exc.value.code == "rate_limited"
    # 不同 IP 不受影响
    st = store.init_upload(
        user_id="anon:s10",
        declared_size=10,
        declared_sha256=sha(b"d10"),
        chunk_size=4,
        file_name="v.mp4",
        limits=limits,
        owner_scope=store.OWNER_SCOPE_ANONYMOUS,
        client_ip_hash="iphash-other",
        per_ip_active=3,
    )
    assert st["state"] == store.STATE_RECEIVING


def test_per_ip_inflight_bytes_gate(uploads_env):
    """字节聚合：cap×max_file 收缩后（admin 改小 max_file_mb），count 未满
    也要被字节维度拦住。"""
    mb = 1024 * 1024
    ip_hash = "iphash-bytes"
    big = anon_limits(max_file_mb=200, per_user_inflight_gb=1)
    for i in range(2):
        store.init_upload(
            user_id=f"anon:b{i}",
            declared_size=200 * mb,
            declared_sha256=sha(f"b{i}".encode()),
            chunk_size=64 * mb,
            file_name="v.mp4",
            limits=big,
            owner_scope=store.OWNER_SCOPE_ANONYMOUS,
            client_ip_hash=ip_hash,
            per_ip_active=3,
        )
    shrunk = anon_limits(max_file_mb=100, per_user_inflight_gb=1)
    with pytest.raises(ChunkedUploadError) as exc:
        store.init_upload(
            user_id="anon:b9",
            declared_size=100 * mb,
            declared_sha256=sha(b"b9"),
            chunk_size=64 * mb,
            file_name="v.mp4",
            limits=shrunk,
            owner_scope=store.OWNER_SCOPE_ANONYMOUS,
            client_ip_hash=ip_hash,
            per_ip_active=3,
        )
    assert exc.value.status_code == 429
    assert exc.value.code == "rate_limited"


# ---------------------------------------------------------------------------
# 匿名身份隔离
# ---------------------------------------------------------------------------


def test_cross_session_isolation_404(uploads_env, monkeypatch):
    client_a = make_client(monkeypatch, session=SESSION_A)
    data = b"isolation-data"
    r = do_init(client_a, data)
    uid = r.json()["upload_id"]

    client_b = make_client(monkeypatch, session=SESSION_B)
    assert client_b.get(f"{PREFIX}/{uid}/status").status_code == 404
    r_part = client_b.put(
        f"{PREFIX}/{uid}/part/0",
        content=data[:4],
        headers={**ORIGIN, "x-chunk-sha256": sha(data[:4])},
    )
    assert r_part.status_code == 404
    assert client_b.delete(f"{PREFIX}/{uid}", headers=ORIGIN).status_code == 404


# ---------------------------------------------------------------------------
# complete → intake 全链（mock intake）
# ---------------------------------------------------------------------------


def test_complete_consume_idempotent_single_intake(uploads_env, monkeypatch):
    calls = {"n": 0}

    def _fake_intake(**kwargs):
        calls["n"] += 1
        return dict(FAKE_INTAKE_PAYLOAD)

    client = make_client(monkeypatch, intake=_fake_intake)
    data = b"hello anonymous chunked upload!"
    r = do_init(client, data)
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]
    upload_all_parts(client, uid, data)

    rc = client.post(f"{PREFIX}/{uid}/complete", headers=ORIGIN)
    assert rc.status_code == 200, rc.text
    assert rc.json() == FAKE_INTAKE_PAYLOAD
    assert calls["n"] == 1

    # 终文件已 move 到 /upload 同款落点
    from anonymous_preview_upload import _safe_segment

    media_dir = uploads_env / "uploads" / "anonymous" / _safe_segment(SESSION_A.session_id_hash)
    media = list(media_dir.glob(f"{uid[:12]}_*"))
    assert len(media) == 1 and media[0].is_file()

    # state → consumed；status 暴露 preview_id、无 upload_ref
    identity = f"anon:{SESSION_A.session_id_hash}"
    st = store.load_state(identity, uid)
    assert st["state"] == store.STATE_CONSUMED
    assert st["final_path"] is None
    rs = client.get(f"{PREFIX}/{uid}/status")
    assert rs.status_code == 200
    assert rs.json()["state"] == "consumed"
    assert rs.json()["preview_id"] == "pv_test_1"
    assert "upload_ref" not in rs.json()

    # complete 重试：原样返回已存响应，intake 不二跑
    rc2 = client.post(f"{PREFIX}/{uid}/complete", headers=ORIGIN)
    assert rc2.status_code == 200
    assert rc2.json() == FAKE_INTAKE_PAYLOAD
    assert calls["n"] == 1

    # consumed 不占 per-session active=1 额度：新 init 可立即发起
    r_next = do_init(client, b"another-file-data")
    assert r_next.status_code == 200, r_next.text


def test_intake_failure_rolls_back_and_purges(uploads_env, monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("intake exploded")

    client = make_client(monkeypatch, intake=_boom)
    data = b"failure path data!"
    uid = do_init(client, data).json()["upload_id"]
    upload_all_parts(client, uid, data)

    rc = client.post(f"{PREFIX}/{uid}/complete", headers=ORIGIN)
    assert rc.status_code == 500
    assert rc.json()["error"] == "intake_failed"

    # 媒体 + 上传目录全清；upload 不复存在（同形 404）
    identity = f"anon:{SESSION_A.session_id_hash}"
    assert store.load_state(identity, uid) is None
    from anonymous_preview_upload import _safe_segment

    media_dir = uploads_env / "uploads" / "anonymous" / _safe_segment(SESSION_A.session_id_hash)
    assert not list(media_dir.glob(f"{uid[:12]}_*")) if media_dir.is_dir() else True
    assert client.get(f"{PREFIX}/{uid}/status").status_code == 404

    # active 额度已释放：可重新 init 从头再来
    assert do_init(client, data).status_code == 200


# ---------------------------------------------------------------------------
# sweeper：匿名 6h TTL 与注册档 24h 并存
# ---------------------------------------------------------------------------


def _backdate(identity: str, uid: str, hours: float) -> None:
    sp = store._state_path(identity, uid)
    st = json.loads(sp.read_text(encoding="utf-8"))
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    st["updated_at"] = stamp
    st["created_at"] = stamp
    sp.write_text(json.dumps(st), encoding="utf-8")


def test_sweeper_anonymous_ttl_independent(uploads_env):
    limits = anon_limits(ttl_hours=24, per_user_active=5)
    anon_state = store.init_upload(
        user_id="anon:sweep",
        declared_size=10,
        declared_sha256=sha(b"sw1"),
        chunk_size=4,
        file_name="v.mp4",
        limits=limits,
        owner_scope=store.OWNER_SCOPE_ANONYMOUS,
        client_ip_hash="ip-sw",
        per_ip_active=3,
    )
    reg_state = store.init_upload(
        user_id="user-reg",
        declared_size=10,
        declared_sha256=sha(b"sw2"),
        chunk_size=4,
        file_name="v.mp4",
        limits=limits,
    )
    _backdate("anon:sweep", anon_state["upload_id"], 7)
    _backdate("user-reg", reg_state["upload_id"], 7)

    stats = store.sweep_once(limits, anonymous_ttl_hours=6)
    assert stats["expired_purged"] == 1
    assert store.load_state("anon:sweep", anon_state["upload_id"]) is None
    assert store.load_state("user-reg", reg_state["upload_id"]) is not None


# ---------------------------------------------------------------------------
# import 守卫（F18 同款）
# ---------------------------------------------------------------------------


def test_module_has_no_services_jobs_import():
    src_path = Path(api.__file__)
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            assert not name.startswith("services.jobs"), (
                f"anonymous_preview_chunked_api 不得 import {name}（pydub guard）"
            )


# ---------------------------------------------------------------------------
# plan 2026-06-12 anonymous-express-preview §A（R3 验收）：
# lane 在 init 锁进 state；init 后翻转任意 lane 开关，
# part / complete / status / delete 全部不受影响。
# ---------------------------------------------------------------------------


def test_lane_locked_at_init_flip_switch_transfer_unaffected(uploads_env, monkeypatch):
    """init（express lane）→ 翻关所有开关 → part/complete 照常工作，
    且 complete 的 intake 用 init 时锁定的 state lane（express）。"""
    recorded: dict = {}

    def _fake_intake(**kwargs):
        recorded.update(kwargs)
        return dict(FAKE_INTAKE_PAYLOAD, mode=kwargs.get("mode"))

    client = make_client(monkeypatch, intake=_fake_intake, lane="express")
    data = b"0123456789"
    r = do_init(client, data)
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]

    # state 落盘带 lane=express
    st = store.load_state(f"anon:{SESSION_A.session_id_hash}", uid)
    assert st is not None and st["lane"] == "express"

    # —— 翻转开关：三与门关 + lane resolver 返回 None ——
    monkeypatch.setattr(api, "three_gates_open", lambda: False)
    monkeypatch.setattr(api, "_resolve_active_lane", lambda: None)

    # part 不受影响
    upload_all_parts(client, uid, data)
    # complete 不受影响，intake 拿到 init 锁定的 express
    r2 = client.post(f"{PREFIX}/{uid}/complete", headers=ORIGIN)
    assert r2.status_code == 200, r2.text
    assert recorded.get("mode") == "express"
    assert r2.json()["mode"] == "express"


def test_status_and_delete_unaffected_by_gate_flip(uploads_env, monkeypatch):
    """status / delete 是生命周期端点：开关全关后照常服务。"""
    client = make_client(monkeypatch, lane="free")
    data = b"0123456789"
    r = do_init(client, data)
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]

    monkeypatch.setattr(api, "three_gates_open", lambda: False)
    monkeypatch.setattr(api, "_resolve_active_lane", lambda: None)

    assert client.get(f"{PREFIX}/{uid}/status").status_code == 200
    assert client.delete(f"{PREFIX}/{uid}", headers=ORIGIN).status_code == 200
