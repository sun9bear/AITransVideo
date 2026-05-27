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
