"""分片上传 R1-R6 路由测试 — plan 2026-06-11 §3.1/§3.6 + §5 安全/流式行。"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import chunked_upload_api as api
import chunked_upload_store as store
from auth import require_auth
from chunked_upload_store import ChunkedLimits

USER_A = SimpleNamespace(id="user-a")
USER_B = SimpleNamespace(id="user-b")

# CSRF guard 在 dev 语义下信任 Host 头——同源请求带这个 Origin 即放行。
ORIGIN = {"origin": "http://testserver"}


def enabled_limits(**overrides) -> ChunkedLimits:
    base = dict(
        enabled=True, max_file_mb=2048, chunk_mb=64, per_user_active=5,
        per_user_inflight_gb=4, global_inflight_gb=20, daily_per_user_gb=8,
        disk_floor_gb=0, ttl_hours=24, ready_ttl_hours=6,
    )
    base.update(overrides)
    return ChunkedLimits(**base)


@pytest.fixture()
def uploads_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIVIDEOTRANS_PROJECTS_DIR", str(tmp_path))
    monkeypatch.delenv("AIVIDEOTRANS_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(store, "HARD_MIN_CHUNK_BYTES", 1)
    return tmp_path


@pytest.fixture()
def client_a(uploads_env, monkeypatch):
    monkeypatch.setattr(api, "resolve_chunked_limits", enabled_limits)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: USER_A
    return TestClient(app)


@pytest.fixture()
def client_b(uploads_env, monkeypatch):
    monkeypatch.setattr(api, "resolve_chunked_limits", enabled_limits)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: USER_B
    return TestClient(app)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def do_init(client, data: bytes, chunk_size=4, name="v.mp4"):
    return client.post(
        "/gateway/uploads/chunked/init",
        json={"size": len(data), "sha256": sha(data), "chunk_size": chunk_size, "file_name": name},
        headers=ORIGIN,
    )


def put_part(client, upload_id, n, piece: bytes, hash_override=None):
    return client.put(
        f"/gateway/uploads/chunked/{upload_id}/part/{n}",
        content=piece,
        headers={**ORIGIN, "x-chunk-sha256": hash_override or sha(piece)},
    )


def upload_all(client, data: bytes, chunk_size=4) -> str:
    r = do_init(client, data, chunk_size)
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]
    total = r.json()["total_parts"]
    for n in range(total):
        piece = data[n * chunk_size:(n + 1) * chunk_size]
        rp = put_part(client, uid, n, piece)
        assert rp.status_code == 200, rp.text
    rc = client.post(f"/gateway/uploads/chunked/{uid}/complete", headers=ORIGIN)
    assert rc.status_code == 200, rc.text
    return uid


# ---------------------------------------------------------------------------
# 全链路 happy path
# ---------------------------------------------------------------------------


def test_full_flow_init_parts_complete_status(client_a):
    data = b"hello chunked upload!"
    uid = upload_all(client_a, data, chunk_size=5)
    r = client_a.get(f"/gateway/uploads/chunked/{uid}/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "ready"
    assert body["upload_ref"] == f"chunked:{uid}"
    # complete 幂等
    rc = client_a.post(f"/gateway/uploads/chunked/{uid}/complete", headers=ORIGIN)
    assert rc.status_code == 200
    assert rc.json()["upload_ref"] == f"chunked:{uid}"
    # 返回的是 opaque ref，不是文件路径
    assert "/" not in rc.json()["upload_ref"].split(":", 1)[1]


def test_init_resume_returns_bitmap(client_a):
    data = b"0123456789"
    r1 = do_init(client_a, data)
    uid = r1.json()["upload_id"]
    put_part(client_a, uid, 0, data[:4])
    r2 = do_init(client_a, data)
    assert r2.json()["upload_id"] == uid
    assert r2.json()["resumed"] is True
    assert r2.json()["received_parts"] == [0]


# ---------------------------------------------------------------------------
# CSRF（全部写方法缺 Origin → 403）
# ---------------------------------------------------------------------------


def test_state_changing_methods_reject_missing_origin(client_a):
    data = b"0123456789"
    r = do_init(client_a, data)
    uid = r.json()["upload_id"]
    no_origin_post = client_a.post(
        "/gateway/uploads/chunked/init",
        json={"size": 4, "sha256": "a" * 64, "chunk_size": 4, "file_name": "x"},
    )
    assert no_origin_post.status_code == 403
    no_origin_put = client_a.put(
        f"/gateway/uploads/chunked/{uid}/part/0",
        content=b"0123",
        headers={"x-chunk-sha256": sha(b"0123")},
    )
    assert no_origin_put.status_code == 403
    no_origin_complete = client_a.post(f"/gateway/uploads/chunked/{uid}/complete")
    assert no_origin_complete.status_code == 403
    no_origin_delete = client_a.delete(f"/gateway/uploads/chunked/{uid}")
    assert no_origin_delete.status_code == 403
    # GET 不要求 Origin
    assert client_a.get(f"/gateway/uploads/chunked/{uid}/status").status_code == 200


# ---------------------------------------------------------------------------
# 同形 404（跨用户 / 不存在 / 非法 id 响应体逐字节一致）
# ---------------------------------------------------------------------------


def test_cross_user_and_missing_404_same_shape(client_a, client_b):
    data = b"0123456789"
    uid = do_init(client_a, data).json()["upload_id"]
    cross_user = client_b.get(f"/gateway/uploads/chunked/{uid}/status")
    missing = client_b.get(f"/gateway/uploads/chunked/{'e' * 32}/status")
    malformed = client_b.get("/gateway/uploads/chunked/EVIL../status")
    assert cross_user.status_code == missing.status_code == malformed.status_code == 404
    assert cross_user.content == missing.content == malformed.content
    # 写方法同样同形
    cross_put = put_part(client_b, uid, 0, data[:4])
    missing_put = put_part(client_b, "e" * 32, 0, data[:4])
    assert cross_put.status_code == missing_put.status_code == 404
    assert cross_put.content == missing_put.content


def test_part_index_out_of_range_is_not_found(client_a):
    data = b"0123456789"
    uid = do_init(client_a, data).json()["upload_id"]
    r = put_part(client_a, uid, 99, data[:4])
    assert r.status_code == 404
    r_neg = client_a.put(
        f"/gateway/uploads/chunked/{uid}/part/-1",
        content=data[:4],
        headers={**ORIGIN, "x-chunk-sha256": sha(data[:4])},
    )
    assert r_neg.status_code == 404


# ---------------------------------------------------------------------------
# 流式 / 完整性（§3.6 + r3 per-part 哈希）
# ---------------------------------------------------------------------------


def test_part_hash_mismatch_rejected_not_committed(client_a):
    data = b"0123456789"
    uid = do_init(client_a, data).json()["upload_id"]
    r = put_part(client_a, uid, 0, data[:4], hash_override="0" * 64)
    assert r.status_code == 422
    assert r.json()["error"] == "part_hash_mismatch"
    status = client_a.get(f"/gateway/uploads/chunked/{uid}/status").json()
    assert status["received_parts"] == []
    # tmp 不残留
    updir = store.upload_dir("user-a", uid)
    assert not list(updir.glob("*.tmp"))


def test_part_missing_chunk_sha256_header_rejected(client_a):
    data = b"0123456789"
    uid = do_init(client_a, data).json()["upload_id"]
    r = client_a.put(
        f"/gateway/uploads/chunked/{uid}/part/0",
        content=data[:4],
        headers=ORIGIN,
    )
    assert r.status_code == 422
    assert r.json()["error"] == "chunk_sha256_required"


def test_part_oversize_rejected_413(client_a):
    data = b"0123456789"
    uid = do_init(client_a, data).json()["upload_id"]
    too_big = b"x" * 8  # 协议长度 4
    r = put_part(client_a, uid, 0, too_big)
    assert r.status_code == 413
    assert r.json()["error"] == "part_too_large"


def test_part_undersize_rejected_422(client_a):
    data = b"0123456789"
    uid = do_init(client_a, data).json()["upload_id"]
    r = put_part(client_a, uid, 0, b"xy")
    assert r.status_code == 422
    assert r.json()["error"] == "part_size_mismatch"


def test_full_file_sha_mismatch_failed_integrity(client_a, monkeypatch):
    data = b"0123456789"
    r = client_a.post(
        "/gateway/uploads/chunked/init",
        json={"size": len(data), "sha256": "f" * 64, "chunk_size": 4, "file_name": "v.mp4"},
        headers=ORIGIN,
    )
    uid = r.json()["upload_id"]
    for n in range(r.json()["total_parts"]):
        piece = data[n * 4:(n + 1) * 4]
        assert put_part(client_a, uid, n, piece).status_code == 200
    rc = client_a.post(f"/gateway/uploads/chunked/{uid}/complete", headers=ORIGIN)
    assert rc.status_code == 422
    assert rc.json()["error"] == "sha256_mismatch"
    status = client_a.get(f"/gateway/uploads/chunked/{uid}/status").json()
    assert status["state"] == "failed_integrity"
    assert status["received_parts"] == []


# ---------------------------------------------------------------------------
# 限额 / 磁盘
# ---------------------------------------------------------------------------


def test_init_over_limit_413(client_a, monkeypatch):
    monkeypatch.setattr(
        api, "resolve_chunked_limits", lambda: enabled_limits(max_file_mb=1),
    )
    r = client_a.post(
        "/gateway/uploads/chunked/init",
        json={"size": 2 * 1024 * 1024, "sha256": "a" * 64,
              "chunk_size": 1024 * 1024, "file_name": "big"},
        headers=ORIGIN,
    )
    assert r.status_code == 413
    assert r.json()["error"] == "over_limit"


def test_init_too_many_active_429(client_a, monkeypatch):
    monkeypatch.setattr(
        api, "resolve_chunked_limits", lambda: enabled_limits(per_user_active=1),
    )
    assert do_init(client_a, b"aaaa").status_code == 200
    r = do_init(client_a, b"bbbb")
    assert r.status_code == 429
    assert r.json()["error"] == "too_many_active"


def test_init_disk_reserve_507(client_a, monkeypatch):
    monkeypatch.setattr(store, "_disk_free_bytes", lambda _p: 0)
    monkeypatch.setattr(
        api, "resolve_chunked_limits", lambda: enabled_limits(disk_floor_gb=1),
    )
    r = do_init(client_a, b"0123456789")
    assert r.status_code == 507
    assert r.json()["error"] == "insufficient_storage"


# ---------------------------------------------------------------------------
# 状态机经 HTTP 面
# ---------------------------------------------------------------------------


def test_complete_missing_parts_409(client_a):
    data = b"0123456789"
    uid = do_init(client_a, data).json()["upload_id"]
    put_part(client_a, uid, 0, data[:4])
    r = client_a.post(f"/gateway/uploads/chunked/{uid}/complete", headers=ORIGIN)
    assert r.status_code == 409
    assert r.json()["error"] == "missing_parts"


def test_part_rejected_after_ready_409(client_a):
    data = b"0123456789"
    uid = upload_all(client_a, data)
    r = put_part(client_a, uid, 0, data[:4])
    assert r.status_code == 409
    assert r.json()["error"] == "wrong_state"


def test_delete_receiving_ok_ready_409(client_a):
    data = b"0123456789"
    uid = do_init(client_a, data).json()["upload_id"]
    assert client_a.delete(f"/gateway/uploads/chunked/{uid}", headers=ORIGIN).status_code == 200
    uid2 = upload_all(client_a, b"abcdefgh")
    r = client_a.delete(f"/gateway/uploads/chunked/{uid2}", headers=ORIGIN)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# feature gate（kill-switch）与 R6 limits
# ---------------------------------------------------------------------------


def test_disabled_gate_r1_r2_r3_uniform_404(client_a, monkeypatch):
    data = b"0123456789"
    uid = do_init(client_a, data).json()["upload_id"]
    put_part(client_a, uid, 0, data[:4])
    monkeypatch.setattr(
        api, "resolve_chunked_limits", lambda: enabled_limits(enabled=False),
    )
    r_init = do_init(client_a, b"zzzz")
    r_part = put_part(client_a, uid, 1, data[4:8])
    r_complete = client_a.post(f"/gateway/uploads/chunked/{uid}/complete", headers=ORIGIN)
    assert r_init.status_code == r_part.status_code == r_complete.status_code == 404
    assert r_init.content == r_part.content == r_complete.content
    # status / delete 留给清理路径
    assert client_a.get(f"/gateway/uploads/chunked/{uid}/status").status_code == 200
    assert client_a.delete(f"/gateway/uploads/chunked/{uid}", headers=ORIGIN).status_code == 200


def test_limits_endpoint_shape(client_a, monkeypatch):
    r = client_a.get("/gateway/uploads/chunked/limits")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "enabled": True,
        "threshold_mb": api.SINGLE_REQUEST_THRESHOLD_MB,
        "max_file_mb": 2048,
        "chunk_mb": 64,
    }
    monkeypatch.setattr(
        api, "resolve_chunked_limits", lambda: enabled_limits(enabled=False),
    )
    r2 = client_a.get("/gateway/uploads/chunked/limits")
    assert r2.status_code == 200  # 关闭时 200 + enabled:false，前端据此隐藏入口
    assert r2.json()["enabled"] is False


def test_unauthenticated_user_gets_401(uploads_env, monkeypatch):
    monkeypatch.setattr(api, "resolve_chunked_limits", enabled_limits)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: None
    client = TestClient(app)
    assert client.get("/gateway/uploads/chunked/limits").status_code == 401
    r = client.post(
        "/gateway/uploads/chunked/init",
        json={"size": 4, "sha256": "a" * 64, "chunk_size": 4, "file_name": "x"},
        headers=ORIGIN,
    )
    assert r.status_code == 401


def test_content_length_required(client_a):
    """Content-Length 缺失拒绝（411）。TestClient 总会带 CL，直接调 handler 难——
    改为断言源码契约：缺头走 411 分支。分片接收逻辑自 plan §9 起抽到共享
    helper ``receive_part``（注册档 R2 / 匿名档 A2 复用），契约断言跟随。"""
    import inspect

    src = inspect.getsource(api.receive_part)
    assert 'headers.get("content-length")' in src
    assert "411" in src
    # R2 handler 必须经由共享 helper（防止未来再分叉出第二份流式接收实现）
    handler_src = inspect.getsource(api.chunked_upload_part)
    assert "receive_part(" in handler_src
