"""Phase 4.2 D.1 — clone-gate API + `_resolve_clone_gate` 共享授权函数测试。

Codex 2026-05-27 review 锁死的 D.1 测试矩阵：

**Section A** (8 cases) — `_resolve_clone_gate` 纯函数单元测试：

==============================  ==========  ==========  ==========  =====================
case                            user_role   allowlist?  GA flag     expected reason
==============================  ==========  ==========  ==========  =====================
admin_ga_false_no_allowlist     admin       no          False       "admin"
admin_ga_true_no_allowlist      admin       no          True        "admin"
admin_in_allowlist              admin       yes         False       "admin"  (优先级)
normal_in_allowlist             normal      yes         False       "allowlist"
normal_in_allowlist_ga_true     normal      yes         True        "allowlist" (优先级)
normal_not_in_allowlist_ga_true normal      no          True        "general_availability"
normal_not_in_allowlist_ga_false normal     no          False       "none" (denied)
unauthenticated                 (None)      -           -           "none" (denied)
==============================  ==========  ==========  ==========  =====================

**Section B** (5 cases) — GET ``/clone-gate`` endpoint:

- B1: 未登录 → 401 unauthenticated
- B2: admin → 200 + can_access_clone=True + reason="admin"
- B3: allowlist user → 200 + can_access_clone=True + reason="allowlist"
- B4: 普通 user + GA=false + 不在 allowlist → 200 + can_access_clone=False
       + reason="none"（**不**是 403）
- B5: 普通 user + GA=true → 200 + can_access_clone=True + reason="general_availability"

**Section C** (4 parametrized cases) — GET vs POST 一致性守卫：

锁死 GET ``/clone-gate`` 的 ``can_access_clone`` 与 POST ``/clone``
``_check_authorized`` 是否拒绝 完全一致 —— 防止 GET 与 POST 两侧授权 OR
逻辑漂移（Codex P1.2）。

==========  ==========  ==========  ==========================  ==================
user_role   allowlist?  GA flag     GET can_access_clone        POST Layer 1
==========  ==========  ==========  ==========================  ==================
admin       no          False       True                        passes (not blocked)
normal      yes         False       True                        passes (not blocked)
normal      no          True        True                        passes (not blocked)
normal      no          False       False                       403 forbidden
==========  ==========  ==========  ==========================  ==================
"""
from __future__ import annotations

import io
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT / "gateway", REPO_ROOT / "src", REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


import auth  # noqa: F401
from admin_settings import AdminSettings  # type: ignore[import-not-found]
from cosyvoice_clone import api as clone_api  # type: ignore[import-not-found]
from cosyvoice_clone.api import (  # type: ignore[import-not-found]
    CONSENT_MODAL_VERSION,
    CloneGateDecision,
    _resolve_clone_gate,
    router as clone_router,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeUser:
    def __init__(self, user_id: str, role: str = "user"):
        self.id = user_id
        self.role = role


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(clone_router)
    return app


def _build_client(
    monkeypatch,
    *,
    user: _FakeUser | None,
    ga_enabled: bool,
    allowlist: list[str] | None = None,
) -> TestClient:
    """统一构造仅含 cosyvoice_clone router 的 FastAPI app。"""
    s = AdminSettings(
        cosyvoice_clone_worker_enabled=True,
        cosyvoice_clone_user_allowlist=allowlist or [],
        cosyvoice_clone_default_target_model="cosyvoice-v3.5-flash",
        cosyvoice_clone_max_voices_per_user=3,
        cosyvoice_clone_general_availability_enabled=ga_enabled,
    )
    monkeypatch.setattr(clone_api, "load_settings", lambda: s)

    app = _make_app()

    async def _get_current_user_override():
        return user

    app.dependency_overrides[clone_api.get_current_user] = _get_current_user_override
    # POST /clone 用到 get_db；GET /clone-gate 不用，但同时注入避免漏配
    app.dependency_overrides[clone_api.get_db] = lambda: None  # type: ignore[arg-type]
    return TestClient(app)


def _consent_form_fields() -> dict:
    return {
        "consent_voice_clone_confirmed": "true",
        "consent_modal_version": CONSENT_MODAL_VERSION,
        "consent_confirmed_at": "2026-05-27T00:00:00Z",
    }


def _default_form() -> dict:
    return {
        "target_model": "cosyvoice-v3.5-flash",
        "speaker_id": "spk_a",
        "speaker_name": "Test Speaker",
        **_consent_form_fields(),
    }


def _post_clone(client: TestClient) -> httpx.Response:
    files = {"sample": ("sample.wav", io.BytesIO(b"FAKE-AUDIO" * 200), "audio/wav")}
    return client.post(
        "/api/voice/cosyvoice/clone", data=_default_form(), files=files,
    )


def _is_layer1_blocked(resp: httpx.Response) -> bool:
    """与 a2c admin gate 测试一致：检查是否是 Layer 1（auth/authz）拒绝。"""
    if resp.status_code == 401:
        return resp.json().get("detail", {}).get("code") == "unauthenticated"
    if resp.status_code == 403:
        return resp.json().get("detail", {}).get("code") == "forbidden_not_in_allowlist"
    return False


# ---------------------------------------------------------------------------
# Section A — `_resolve_clone_gate` 纯函数单元测试（8 cases）
# ---------------------------------------------------------------------------


def test_resolve_clone_gate_returns_dataclass_with_required_fields():
    """合约：返回值必须是 CloneGateDecision frozen dataclass，4 字段齐全。"""
    user = _FakeUser(user_id="u1", role="admin")
    d = _resolve_clone_gate(user, allowlist=[], general_availability_enabled=False)
    assert isinstance(d, CloneGateDecision)
    assert isinstance(d.can_access_clone, bool)
    assert d.reason in ("admin", "allowlist", "general_availability", "none")
    assert isinstance(d.user_is_admin, bool)
    assert isinstance(d.user_in_allowlist, bool)
    # frozen
    with pytest.raises(Exception):
        d.can_access_clone = False  # type: ignore[misc]


def test_resolve_clone_gate_admin_ga_false_no_allowlist():
    """**A1 admin_ga_false_no_allowlist**：admin → reason=admin，无视 GA / allowlist。"""
    user = _FakeUser(user_id="u1", role="admin")
    d = _resolve_clone_gate(user, allowlist=[], general_availability_enabled=False)
    assert d.can_access_clone is True
    assert d.reason == "admin"
    assert d.user_is_admin is True
    assert d.user_in_allowlist is False


def test_resolve_clone_gate_admin_ga_true_no_allowlist():
    """**A2 admin_ga_true_no_allowlist**：admin 优先于 GA。reason 必须是 admin。"""
    user = _FakeUser(user_id="u1", role="admin")
    d = _resolve_clone_gate(user, allowlist=[], general_availability_enabled=True)
    assert d.can_access_clone is True
    assert d.reason == "admin"
    assert d.user_is_admin is True


def test_resolve_clone_gate_admin_in_allowlist_priority():
    """**A3 admin_in_allowlist**：admin 优先于 allowlist。reason 必须是 admin。"""
    user = _FakeUser(user_id="u1", role="admin")
    d = _resolve_clone_gate(user, allowlist=["u1"], general_availability_enabled=False)
    assert d.can_access_clone is True
    assert d.reason == "admin"
    assert d.user_is_admin is True
    assert d.user_in_allowlist is True  # 事实陈述


def test_resolve_clone_gate_normal_in_allowlist():
    """**A4 normal_in_allowlist**：普通用户 + allowlist → reason=allowlist。"""
    user = _FakeUser(user_id="u1", role="user")
    d = _resolve_clone_gate(user, allowlist=["u1"], general_availability_enabled=False)
    assert d.can_access_clone is True
    assert d.reason == "allowlist"
    assert d.user_is_admin is False
    assert d.user_in_allowlist is True


def test_resolve_clone_gate_normal_in_allowlist_priority_over_ga():
    """**A5 normal_in_allowlist_ga_true**：普通用户 in allowlist + GA=True。
    reason 必须是 allowlist（优先于 GA）。"""
    user = _FakeUser(user_id="u1", role="user")
    d = _resolve_clone_gate(user, allowlist=["u1"], general_availability_enabled=True)
    assert d.can_access_clone is True
    assert d.reason == "allowlist"


def test_resolve_clone_gate_normal_not_in_allowlist_ga_true():
    """**A6 normal_not_in_allowlist_ga_true**：GA=True → 普通用户 reason=general_availability。"""
    user = _FakeUser(user_id="u1", role="user")
    d = _resolve_clone_gate(user, allowlist=[], general_availability_enabled=True)
    assert d.can_access_clone is True
    assert d.reason == "general_availability"
    assert d.user_is_admin is False
    assert d.user_in_allowlist is False


def test_resolve_clone_gate_normal_not_in_allowlist_ga_false_denied():
    """**A7 normal_not_in_allowlist_ga_false**：denied。reason=none。"""
    user = _FakeUser(user_id="u1", role="user")
    d = _resolve_clone_gate(user, allowlist=[], general_availability_enabled=False)
    assert d.can_access_clone is False
    assert d.reason == "none"
    assert d.user_is_admin is False
    assert d.user_in_allowlist is False


def test_resolve_clone_gate_unauthenticated_returns_none():
    """**A8 unauthenticated**：user=None → can_access_clone=False, reason=none。
    caller（GET/POST endpoint）决定是否抛 401。"""
    d = _resolve_clone_gate(None, allowlist=["u1"], general_availability_enabled=True)
    assert d.can_access_clone is False
    assert d.reason == "none"
    assert d.user_is_admin is False
    assert d.user_in_allowlist is False


# ---------------------------------------------------------------------------
# Section B — GET /clone-gate endpoint 行为测试（5 cases）
# ---------------------------------------------------------------------------


def test_get_clone_gate_unauthenticated_returns_401(monkeypatch):
    """**B1**：未登录 → 401 unauthenticated。不返 200 + can_access_clone=False。"""
    client = _build_client(monkeypatch, user=None, ga_enabled=True, allowlist=[])
    resp = client.get("/api/voice/cosyvoice/clone-gate")
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


def test_get_clone_gate_admin_returns_can_access_admin(monkeypatch):
    """**B2**：admin → 200 + can_access_clone=True + reason=admin。"""
    user = _FakeUser(user_id="u1", role="admin")
    client = _build_client(monkeypatch, user=user, ga_enabled=False, allowlist=[])
    resp = client.get("/api/voice/cosyvoice/clone-gate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["can_access_clone"] is True
    assert body["authorization_reason"] == "admin"
    assert body["general_availability_enabled"] is False
    assert body["user_is_admin"] is True
    assert body["user_in_allowlist"] is False


def test_get_clone_gate_allowlist_user_returns_can_access_allowlist(monkeypatch):
    """**B3**：allowlist user → 200 + can_access_clone=True + reason=allowlist。"""
    uid = str(uuid.uuid4())
    user = _FakeUser(user_id=uid, role="user")
    client = _build_client(monkeypatch, user=user, ga_enabled=False, allowlist=[uid])
    resp = client.get("/api/voice/cosyvoice/clone-gate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["can_access_clone"] is True
    assert body["authorization_reason"] == "allowlist"
    assert body["user_is_admin"] is False
    assert body["user_in_allowlist"] is True


def test_get_clone_gate_normal_user_ga_false_returns_denied_200(monkeypatch):
    """**B4（关键）**：普通用户 + GA=false + 不在 allowlist。

    必须是 ``200 + can_access_clone=False + reason=none``，
    **不是** 403 —— 因为 GET 是显示层 gate 读取，登录用户都有权知道自己
    "是否能看到克隆入口"。"""
    user = _FakeUser(user_id=str(uuid.uuid4()), role="user")
    client = _build_client(monkeypatch, user=user, ga_enabled=False, allowlist=[])
    resp = client.get("/api/voice/cosyvoice/clone-gate")
    assert resp.status_code == 200  # ← 不是 403
    body = resp.json()
    assert body["can_access_clone"] is False
    assert body["authorization_reason"] == "none"
    assert body["general_availability_enabled"] is False
    assert body["user_is_admin"] is False
    assert body["user_in_allowlist"] is False


def test_get_clone_gate_normal_user_ga_true_returns_can_access_ga(monkeypatch):
    """**B5**：普通用户 + GA=True → can_access_clone=True + reason=general_availability。"""
    user = _FakeUser(user_id=str(uuid.uuid4()), role="user")
    client = _build_client(monkeypatch, user=user, ga_enabled=True, allowlist=[])
    resp = client.get("/api/voice/cosyvoice/clone-gate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["can_access_clone"] is True
    assert body["authorization_reason"] == "general_availability"
    assert body["general_availability_enabled"] is True


# ---------------------------------------------------------------------------
# Section C — GET vs POST 一致性守卫（4 parametrized cases，Codex P1.2 核心）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id, role, in_allowlist, ga_enabled, gate_should_grant",
    [
        ("admin_ga_false_no_allowlist", "admin", False, False, True),
        ("allowlist_user_ga_false", "user", True, False, True),
        ("normal_user_ga_true", "user", False, True, True),
        ("normal_user_ga_false_no_allowlist", "user", False, False, False),
    ],
    ids=lambda v: str(v),
)
def test_gate_api_and_check_authorized_always_agree(
    monkeypatch, case_id, role, in_allowlist, ga_enabled, gate_should_grant,
):
    """**Section C 核心守卫（Codex P1.2）**：

    对**任意**(role, in_allowlist, ga_enabled) 组合：
    - GET ``/clone-gate`` 返回的 ``can_access_clone`` 必须等于
    - POST ``/clone`` Layer 1 ``_check_authorized`` 是否放行

    任一漂移都让前端显示与后端实际行为脱节 —— 用户看见按钮但点击 403。
    本测试**直接**比较两侧，不允许偷工。
    """
    uid = str(uuid.uuid4())
    user = _FakeUser(user_id=uid, role=role)
    allowlist = [uid] if in_allowlist else []

    client = _build_client(
        monkeypatch, user=user, ga_enabled=ga_enabled, allowlist=allowlist,
    )

    # GET /clone-gate 决策
    get_resp = client.get("/api/voice/cosyvoice/clone-gate")
    assert get_resp.status_code == 200, (
        f"[{case_id}] GET should always be 200 for authenticated user, "
        f"got {get_resp.status_code}"
    )
    get_can_access = get_resp.json()["can_access_clone"]

    # POST /clone Layer 1 决策（用 _is_layer1_blocked helper：not blocked = granted）
    post_resp = _post_clone(client)
    post_granted = not _is_layer1_blocked(post_resp)

    # 一致性核心断言
    assert get_can_access == post_granted, (
        f"[{case_id}] gate API ({get_can_access}) and _check_authorized "
        f"({post_granted}) disagree — 两侧授权 OR 逻辑漂移。"
    )
    # 双向 ground-truth 锁
    assert get_can_access == gate_should_grant, (
        f"[{case_id}] GET can_access_clone={get_can_access}, "
        f"expected {gate_should_grant}"
    )


# ---------------------------------------------------------------------------
# Section D — runtime_ready field (PR #15 E.1 P2 fix, Codex 2026-05-27)
# ---------------------------------------------------------------------------


from dataclasses import dataclass


@dataclass
class _FakeGwSettings:
    """Stub Gateway config.Settings — only the fields _resolve_runtime_ready
    reads. Attribute names mirror ``ALIYUN_OSS_REQUIRED_SETTINGS`` in
    ``gateway/cosyvoice_clone/sample_uploader.py`` and the mainland worker
    config in ``gateway/mainland_voice_worker.py``."""

    cosyvoice_sample_uploader: str = "aliyun_oss"
    # OSS config — match the exact attribute names checked by
    # `missing_aliyun_oss_settings()`.
    cosyvoice_oss_endpoint: str | None = "https://oss-cn-beijing.aliyuncs.com"
    cosyvoice_oss_bucket: str | None = "avt-cosy-prod"
    cosyvoice_oss_access_key_id: str | None = "AK-test"
    cosyvoice_oss_access_key_secret: str | None = "SK-test"
    # Mainland voice worker config — match the exact attribute names
    # checked by `is_mainland_voice_worker_config_ready()` /
    # `build_mainland_voice_worker_client()`.
    mainland_voice_worker_enabled: bool = True
    mainland_voice_worker_url: str | None = "https://worker.example.cn"
    mainland_voice_worker_hmac_key_id: str | None = "k-test"
    mainland_voice_worker_hmac_secret: str | None = "s-test"


def _build_runtime_client(
    monkeypatch,
    *,
    user: _FakeUser | None,
    admin_overrides: dict | None = None,
    gw_overrides: dict | None = None,
) -> TestClient:
    """Constructs the FastAPI app with controllable AdminSettings + a stub
    gateway config, so we can drive ``_resolve_runtime_ready`` from tests
    without env mutations."""
    # Merge defaults then overrides so admin_overrides can clobber any default.
    admin_kwargs: dict = {
        "cosyvoice_clone_worker_enabled": True,
        "cosyvoice_clone_user_allowlist": [],
        "cosyvoice_clone_default_target_model": "cosyvoice-v3.5-flash",
        "cosyvoice_clone_max_voices_per_user": 3,
        # Default GA True so authorization doesn't gate runtime tests.
        "cosyvoice_clone_general_availability_enabled": True,
    }
    admin_kwargs.update(admin_overrides or {})
    admin = AdminSettings(**admin_kwargs)
    monkeypatch.setattr(clone_api, "load_settings", lambda: admin)

    gw = _FakeGwSettings(**(gw_overrides or {}))
    monkeypatch.setattr(clone_api, "gw_settings", gw)

    app = _make_app()

    async def _get_current_user_override():
        return user

    app.dependency_overrides[clone_api.get_current_user] = _get_current_user_override
    app.dependency_overrides[clone_api.get_db] = lambda: None  # type: ignore[arg-type]
    return TestClient(app)


def test_runtime_ready_true_when_worker_enabled_and_oss_configured(monkeypatch):
    """**D1 happy path**: worker_enabled + aliyun_oss + 配置完整 →
    runtime_ready=True, code=None, can_show_clone_button=True (GA also on)."""
    user = _FakeUser(user_id="u1", role="user")
    client = _build_runtime_client(monkeypatch, user=user)
    resp = client.get("/api/voice/cosyvoice/clone-gate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["runtime_ready"] is True
    assert body["runtime_unavailable_code"] is None
    assert body["can_show_clone_button"] is True


def test_runtime_ready_false_when_worker_disabled(monkeypatch):
    """**D2 Layer 2**: cosyvoice_clone_worker_enabled=False →
    runtime_ready=False, code='clone_feature_disabled'."""
    user = _FakeUser(user_id="u1", role="user")
    client = _build_runtime_client(
        monkeypatch,
        user=user,
        admin_overrides={"cosyvoice_clone_worker_enabled": False},
    )
    resp = client.get("/api/voice/cosyvoice/clone-gate")
    body = resp.json()
    assert body["runtime_ready"] is False
    assert body["runtime_unavailable_code"] == "clone_feature_disabled"
    # can_show_clone_button must AND can_access_clone (true here from GA)
    # with runtime_ready (false here) → false.
    assert body["can_show_clone_button"] is False
    # can_access_clone (policy) should still be True (admin/GA path) —
    # runtime gate is **separate** from policy gate. This is the whole
    # point of the P2 fix: don't conflate the two layers.
    assert body["can_access_clone"] is True


def test_runtime_ready_false_when_uploader_is_local_fs_stub(monkeypatch):
    """**D3 Layer 3a**: uploader=local_fs_stub (dev placeholder) →
    runtime_ready=False, code='sample_uploader_not_configured'."""
    user = _FakeUser(user_id="u1", role="user")
    client = _build_runtime_client(
        monkeypatch,
        user=user,
        gw_overrides={"cosyvoice_sample_uploader": "local_fs_stub"},
    )
    resp = client.get("/api/voice/cosyvoice/clone-gate")
    body = resp.json()
    assert body["runtime_ready"] is False
    assert body["runtime_unavailable_code"] == "sample_uploader_not_configured"
    assert body["can_show_clone_button"] is False


def test_runtime_ready_false_when_uploader_oss_config_missing(monkeypatch):
    """**D4 Layer 3b**: uploader=aliyun_oss but bucket missing →
    runtime_ready=False, code='sample_uploader_config_missing'."""
    user = _FakeUser(user_id="u1", role="user")
    client = _build_runtime_client(
        monkeypatch,
        user=user,
        # bucket missing → missing_aliyun_oss_settings returns non-empty
        gw_overrides={"cosyvoice_oss_bucket": None},
    )
    resp = client.get("/api/voice/cosyvoice/clone-gate")
    body = resp.json()
    assert body["runtime_ready"] is False
    assert body["runtime_unavailable_code"] == "sample_uploader_config_missing"
    assert body["can_show_clone_button"] is False


def test_runtime_ready_layer_ordering_worker_disabled_dominates(monkeypatch):
    """**D5 layer order**: Layer 2 (admin `clone_feature_disabled`) beats
    Layer 3 (uploader) which beats Layer 10 (mainland worker). When all
    are wrong, we report the highest-priority code so admin knows the
    right thing to fix first."""
    user = _FakeUser(user_id="u1", role="user")
    client = _build_runtime_client(
        monkeypatch,
        user=user,
        admin_overrides={"cosyvoice_clone_worker_enabled": False},
        # Uploader AND mainland worker also broken — but Layer 2 wins.
        gw_overrides={
            "cosyvoice_sample_uploader": "local_fs_stub",
            "mainland_voice_worker_enabled": False,
        },
    )
    resp = client.get("/api/voice/cosyvoice/clone-gate")
    body = resp.json()
    assert body["runtime_unavailable_code"] == "clone_feature_disabled"


# ---- Section D 二轮 P2 fix: mainland worker config readiness ----


def test_runtime_ready_false_when_mainland_worker_disabled(monkeypatch):
    """**D7 Layer 10a**: `mainland_voice_worker_enabled=false` →
    runtime_ready=false, code='worker_disabled'.

    Closes the "/clone-gate said ready but POST 503s on worker_disabled"
    drift that Codex 2026-05-27 二轮 review caught.
    """
    user = _FakeUser(user_id="u1", role="user")
    client = _build_runtime_client(
        monkeypatch,
        user=user,
        gw_overrides={"mainland_voice_worker_enabled": False},
    )
    body = client.get("/api/voice/cosyvoice/clone-gate").json()
    assert body["runtime_ready"] is False
    assert body["runtime_unavailable_code"] == "worker_disabled"
    assert body["can_show_clone_button"] is False


def test_runtime_ready_false_when_mainland_worker_url_missing(monkeypatch):
    """**D8 Layer 10b**: worker enabled but URL empty → worker_disabled.

    Mirrors the defensive check in build_mainland_voice_worker_client
    (theoretically startup_checks would have flipped enabled=false, but
    the factory + this probe both double-check)."""
    user = _FakeUser(user_id="u1", role="user")
    client = _build_runtime_client(
        monkeypatch,
        user=user,
        gw_overrides={"mainland_voice_worker_url": ""},
    )
    body = client.get("/api/voice/cosyvoice/clone-gate").json()
    assert body["runtime_ready"] is False
    assert body["runtime_unavailable_code"] == "worker_disabled"


def test_runtime_ready_false_when_mainland_worker_key_id_missing(monkeypatch):
    """**D9 Layer 10c**: worker enabled + URL set but HMAC key_id empty
    → worker_disabled."""
    user = _FakeUser(user_id="u1", role="user")
    client = _build_runtime_client(
        monkeypatch,
        user=user,
        gw_overrides={"mainland_voice_worker_hmac_key_id": ""},
    )
    body = client.get("/api/voice/cosyvoice/clone-gate").json()
    assert body["runtime_ready"] is False
    assert body["runtime_unavailable_code"] == "worker_disabled"


def test_runtime_ready_false_when_mainland_worker_secret_missing(monkeypatch):
    """**D10 Layer 10d**: worker enabled + URL + key_id set but HMAC
    secret empty → worker_disabled.

    Critical because the factory wouldn't construct a client without all
    three components; without this gate the frontend would show button
    and POST would 503 immediately."""
    user = _FakeUser(user_id="u1", role="user")
    client = _build_runtime_client(
        monkeypatch,
        user=user,
        gw_overrides={"mainland_voice_worker_hmac_secret": ""},
    )
    body = client.get("/api/voice/cosyvoice/clone-gate").json()
    assert body["runtime_ready"] is False
    assert body["runtime_unavailable_code"] == "worker_disabled"


def test_runtime_ready_does_not_leak_worker_secrets_to_response(monkeypatch):
    """**D11 security guard (Codex 2026-05-27 二轮)**: the response body
    MUST NOT contain raw URL / key_id / secret values from the worker
    config. Only boolean + enum code can leak to the frontend.
    """
    user = _FakeUser(user_id="u1", role="user")
    client = _build_runtime_client(
        monkeypatch,
        user=user,
        # Use distinctive sentinel values that would obviously appear if
        # they leaked.
        gw_overrides={
            "mainland_voice_worker_url": "https://SECRET-WORKER-URL.example",
            "mainland_voice_worker_hmac_key_id": "SECRET-KEY-ID-SENTINEL",
            "mainland_voice_worker_hmac_secret": "SECRET-SECRET-SENTINEL",
            "cosyvoice_oss_access_key_id": "SECRET-OSS-AK-SENTINEL",
            "cosyvoice_oss_access_key_secret": "SECRET-OSS-SECRET-SENTINEL",
        },
    )
    body_text = client.get("/api/voice/cosyvoice/clone-gate").text
    for sentinel in (
        "SECRET-WORKER-URL",
        "SECRET-KEY-ID-SENTINEL",
        "SECRET-SECRET-SENTINEL",
        "SECRET-OSS-AK-SENTINEL",
        "SECRET-OSS-SECRET-SENTINEL",
    ):
        assert sentinel not in body_text, (
            f"/clone-gate response leaked sensitive value `{sentinel}` "
            f"to frontend. Only runtime_ready boolean + enum code may "
            f"escape; URL / key_id / secret stay server-side."
        )


def test_can_show_clone_button_is_and_of_two_layers(monkeypatch):
    """**D6 joint field**: ``can_show_clone_button = can_access_clone &&
    runtime_ready``. Test all 4 combinations:

    ============  =============  ==================  ===================
    can_access     runtime_ready  can_show_button     scenario
    ============  =============  ==================  ===================
    True           True            True                happy path
    True           False           False               policy OK, runtime broken
    False          True            False               not authorized, runtime OK
    False          False           False               neither
    ============  =============  ==================  ===================
    """
    user = _FakeUser(user_id="u1", role="user")

    # Case 1: both true (GA on, runtime configured)
    c = _build_runtime_client(monkeypatch, user=user)
    body = c.get("/api/voice/cosyvoice/clone-gate").json()
    assert (body["can_access_clone"], body["runtime_ready"], body["can_show_clone_button"]) == (True, True, True)

    # Case 2: policy True, runtime False (worker disabled)
    c = _build_runtime_client(
        monkeypatch,
        user=user,
        admin_overrides={"cosyvoice_clone_worker_enabled": False},
    )
    body = c.get("/api/voice/cosyvoice/clone-gate").json()
    assert (body["can_access_clone"], body["runtime_ready"], body["can_show_clone_button"]) == (True, False, False)

    # Case 3: policy False (GA off, no allowlist), runtime True
    c = _build_runtime_client(
        monkeypatch,
        user=user,
        admin_overrides={"cosyvoice_clone_general_availability_enabled": False},
    )
    body = c.get("/api/voice/cosyvoice/clone-gate").json()
    assert (body["can_access_clone"], body["runtime_ready"], body["can_show_clone_button"]) == (False, True, False)

    # Case 4: both false
    c = _build_runtime_client(
        monkeypatch,
        user=user,
        admin_overrides={
            "cosyvoice_clone_general_availability_enabled": False,
            "cosyvoice_clone_worker_enabled": False,
        },
    )
    body = c.get("/api/voice/cosyvoice/clone-gate").json()
    assert (body["can_access_clone"], body["runtime_ready"], body["can_show_clone_button"]) == (False, False, False)
