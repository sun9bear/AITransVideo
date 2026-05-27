"""Phase 4.2 A.2c — admin_setting GA gate + endpoint Layer 1 守卫测试。

Codex 2026-05-26 拆 PR 范围声明：

- A.2c **唯一**做的事是：admin_setting ``cosyvoice_clone_general_availability_enabled``
  + endpoint Layer 1 ``_check_authorized`` 多一条 GA 分支
- 不动 source_segments / Layer 7.5 mutex（A.2b 已落）
- 不接前端 dispatch（D-E 范围）

4 类授权矩阵（plan v4-followup §8.1 收紧）：

==============================  =================  ============  ====================  ==========
case                             user role          in allowlist  GA flag (admin set)   outcome
==============================  =================  ============  ====================  ==========
admin_ga_false                    admin              no            False                 ✅ 通过
beta_user_ga_false                normal             yes           False                 ✅ 通过
normal_user_ga_false              normal             no            False                 ❌ 403
normal_user_ga_true               normal             no            True                  ✅ 通过
admin_ga_true                     admin              no            True                  ✅ 通过
unauthenticated                   (None)             -             -                     ❌ 401
==============================  =================  ============  ====================  ==========

**关键安全守卫**：所有 ❌ case 必须**不**触发下游 I/O：
- 不读 ``sample`` UploadFile bytes
- 不调 ``assemble_sample_from_job_segments``
- 不调 ``validate_sample_bytes``
- 不调 ``normalize_sample_for_dashscope``
- 不调 ``build_sample_uploader_from_settings``
- 不调 ``build_mainland_voice_worker_client``
"""
from __future__ import annotations

import io
import sys
import uuid
from pathlib import Path
from typing import Any

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
    """带 sample 文件触发 endpoint。"""
    files = {"sample": ("sample.wav", io.BytesIO(b"FAKE-AUDIO" * 200), "audio/wav")}
    return client.post(
        "/api/voice/cosyvoice/clone", data=_default_form(), files=files,
    )


@pytest.fixture
def downstream_call_recorder(monkeypatch):
    """**关键**：注入所有下游函数的"被调用"计数。任何 Layer 1 拒绝 case
    都要求所有下游计数仍为 0。

    Layer 1 之后的层（feature flag / uploader / consent / target_model /
    source_segments / quota / sample bytes / transcode / uploader / worker
    client / worker.clone）只要被 Layer 1 拒就**绝不**应该被调到。
    """
    counts = {
        "load_settings": 0,
        "validate_sample_bytes": 0,
        "normalize_sample_for_dashscope": 0,
        "build_sample_uploader_from_settings": 0,
        "build_mainland_voice_worker_client": 0,
        "assemble_sample_from_job_segments": 0,
    }

    original_load_settings = clone_api.load_settings

    def _wrap_load_settings():
        counts["load_settings"] += 1
        return original_load_settings()

    monkeypatch.setattr(clone_api, "load_settings", _wrap_load_settings)

    def _fake_validator(*args, **kwargs):
        counts["validate_sample_bytes"] += 1
        raise AssertionError("downstream should not run when Layer 1 blocks")

    def _fake_normalizer(*args, **kwargs):
        counts["normalize_sample_for_dashscope"] += 1
        raise AssertionError("downstream should not run when Layer 1 blocks")

    def _fake_uploader_factory(*args, **kwargs):
        counts["build_sample_uploader_from_settings"] += 1
        raise AssertionError("downstream should not run when Layer 1 blocks")

    def _fake_worker_factory(*args, **kwargs):
        counts["build_mainland_voice_worker_client"] += 1
        raise AssertionError("downstream should not run when Layer 1 blocks")

    async def _fake_assembler(*args, **kwargs):
        counts["assemble_sample_from_job_segments"] += 1
        raise AssertionError("downstream should not run when Layer 1 blocks")

    monkeypatch.setattr(clone_api, "validate_sample_bytes", _fake_validator)
    monkeypatch.setattr(
        clone_api, "normalize_sample_for_dashscope", _fake_normalizer
    )
    monkeypatch.setattr(
        clone_api, "build_sample_uploader_from_settings", _fake_uploader_factory
    )
    monkeypatch.setattr(
        clone_api, "build_mainland_voice_worker_client", _fake_worker_factory
    )
    monkeypatch.setattr(
        clone_api, "assemble_sample_from_job_segments", _fake_assembler
    )

    return counts


def _build_client(
    monkeypatch,
    *,
    user: _FakeUser | None,
    ga_enabled: bool,
    allowlist: list[str] | None = None,
) -> TestClient:
    """构造仅含 cosyvoice_clone router 的 FastAPI app，注入 user + admin settings。"""
    s = AdminSettings(
        cosyvoice_clone_worker_enabled=True,
        cosyvoice_clone_user_allowlist=allowlist or [],
        cosyvoice_clone_default_target_model="cosyvoice-v3.5-flash",
        cosyvoice_clone_max_voices_per_user=3,
        cosyvoice_clone_general_availability_enabled=ga_enabled,
    )
    monkeypatch.setattr(clone_api, "load_settings", lambda: s)

    app = _make_app()

    # Dependency override：注入当前 user
    async def _get_current_user_override():
        return user

    app.dependency_overrides[clone_api.get_current_user] = _get_current_user_override
    # DB 不会被调到（Layer 1 拒），返一个明显不可用的 sentinel
    app.dependency_overrides[clone_api.get_db] = lambda: None  # type: ignore[arg-type]

    return TestClient(app)


# ---------------------------------------------------------------------------
# 4 类授权矩阵（plan v4-followup §8.1）
# ---------------------------------------------------------------------------


def _is_layer1_blocked(resp: httpx.Response) -> bool:
    """Helper：判断响应是否是 Layer 1 拒绝。

    Layer 1 拒绝的两种形态：
    - 401 + ``detail.code == "unauthenticated"``
    - 403 + ``detail.code == "forbidden_not_in_allowlist"``

    其它 401/403/4xx/5xx 都是下游层拒绝，与 Layer 1 无关（Layer 2 worker
    flag 503、Layer 3 uploader backend 503、Layer 4 consent 400 等等）。
    """
    if resp.status_code == 401:
        return resp.json().get("detail", {}).get("code") == "unauthenticated"
    if resp.status_code == 403:
        return resp.json().get("detail", {}).get("code") == "forbidden_not_in_allowlist"
    return False


def test_admin_user_allowed_when_ga_false(monkeypatch, downstream_call_recorder):
    """**case admin_ga_false**：admin 即使 GA=false 也通过 Layer 1。"""
    user = _FakeUser(user_id=str(uuid.uuid4()), role="admin")
    client = _build_client(monkeypatch, user=user, ga_enabled=False, allowlist=[])
    resp = _post_clone(client)
    # admin 通过 Layer 1 —— 不会出现 401 unauthenticated 或
    # 403 forbidden_not_in_allowlist。下游层（uploader / consent / ...）
    # 是否通过不在本测试关心范围。
    assert not _is_layer1_blocked(resp), (
        f"admin should pass Layer 1，actual status={resp.status_code} "
        f"body={resp.json()}"
    )


def test_allowlist_user_allowed_when_ga_false(monkeypatch, downstream_call_recorder):
    """**case beta_user_ga_false**：在 allowlist 里的普通用户，GA=false 也通过。"""
    uid = str(uuid.uuid4())
    user = _FakeUser(user_id=uid, role="user")
    client = _build_client(
        monkeypatch, user=user, ga_enabled=False, allowlist=[uid],
    )
    resp = _post_clone(client)
    assert not _is_layer1_blocked(resp), (
        f"allowlist user should pass Layer 1，actual status={resp.status_code} "
        f"body={resp.json()}"
    )


def test_normal_user_blocked_when_ga_false_and_not_in_allowlist(
    monkeypatch, downstream_call_recorder,
):
    """**case normal_user_ga_false（核心）**：普通用户 + GA=false + 不在 allowlist
    → 403 forbidden_not_in_allowlist。**所有**下游函数计数仍为 0。"""
    user = _FakeUser(user_id=str(uuid.uuid4()), role="user")
    client = _build_client(monkeypatch, user=user, ga_enabled=False, allowlist=[])
    resp = _post_clone(client)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "forbidden_not_in_allowlist"

    # **关键守卫**：没有任何下游调用发生
    assert downstream_call_recorder["validate_sample_bytes"] == 0
    assert downstream_call_recorder["normalize_sample_for_dashscope"] == 0
    assert downstream_call_recorder["build_sample_uploader_from_settings"] == 0
    assert downstream_call_recorder["build_mainland_voice_worker_client"] == 0
    assert downstream_call_recorder["assemble_sample_from_job_segments"] == 0


def test_normal_user_allowed_when_ga_true(monkeypatch, downstream_call_recorder):
    """**case normal_user_ga_true（GA 开关核心语义）**：GA=True 时**任意**已登录
    普通用户都通过 Layer 1。"""
    user = _FakeUser(user_id=str(uuid.uuid4()), role="user")
    client = _build_client(monkeypatch, user=user, ga_enabled=True, allowlist=[])
    resp = _post_clone(client)
    assert not _is_layer1_blocked(resp), (
        f"normal user with GA=true should pass Layer 1，actual "
        f"status={resp.status_code} body={resp.json()}"
    )


def test_admin_user_allowed_when_ga_true(monkeypatch, downstream_call_recorder):
    """**case admin_ga_true**：admin + GA=true → admin 分支先 short-circuit，仍通过。
    """
    user = _FakeUser(user_id=str(uuid.uuid4()), role="admin")
    client = _build_client(monkeypatch, user=user, ga_enabled=True, allowlist=[])
    resp = _post_clone(client)
    assert not _is_layer1_blocked(resp)


def test_unauthenticated_blocked_with_401(monkeypatch, downstream_call_recorder):
    """**case unauthenticated**：``user is None`` → 401 unauthenticated。
    GA 状态无关。所有下游计数仍为 0。"""
    client = _build_client(monkeypatch, user=None, ga_enabled=True, allowlist=[])
    resp = _post_clone(client)
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"

    # 即使 GA=true，未登录也不应触下游
    assert downstream_call_recorder["validate_sample_bytes"] == 0
    assert downstream_call_recorder["normalize_sample_for_dashscope"] == 0
    assert downstream_call_recorder["build_sample_uploader_from_settings"] == 0
    assert downstream_call_recorder["build_mainland_voice_worker_client"] == 0
    assert downstream_call_recorder["assemble_sample_from_job_segments"] == 0


# ---------------------------------------------------------------------------
# Default value safety guard
# ---------------------------------------------------------------------------


def test_admin_settings_default_ga_is_false() -> None:
    """**部署安全锁**：``AdminSettings`` 默认 ``cosyvoice_clone_general_availability_enabled``
    必须是 ``False``。任何 deploy 第一次启动都是 admin-only 状态。

    plan v4-followup §8.1：Stage 1 admin-only，admin 烟测通过后才能在
    admin 后台**显式**翻 True 进 Stage 2。defaults-to-False 是这套灰度的
    第一道防线 —— 如果默认是 True，新部署会在没人 review 之前就全开放。
    """
    s = AdminSettings()
    assert s.cosyvoice_clone_general_availability_enabled is False, (
        "AdminSettings 默认必须是 admin-only。改 default=True 是危险操作，"
        "必须在 admin_settings.json 显式覆盖。"
    )


def test_admin_settings_ga_field_is_bool_type() -> None:
    """字段类型必须严格 bool（不允许 str / int / etc.，防 admin UI 误传）。"""
    import inspect

    # Pydantic v2: AdminSettings.model_fields["cosyvoice_clone_general_availability_enabled"]
    field = AdminSettings.model_fields["cosyvoice_clone_general_availability_enabled"]
    assert field.annotation is bool, (
        f"cosyvoice_clone_general_availability_enabled type must be bool, "
        f"got {field.annotation}"
    )


# ---------------------------------------------------------------------------
# _check_authorized helper unit tests（细粒度边界）
# ---------------------------------------------------------------------------


def test_check_authorized_admin_returns_user():
    from cosyvoice_clone.api import _check_authorized

    user = _FakeUser(user_id="u-admin", role="admin")
    result = _check_authorized(user, allowlist=[], general_availability_enabled=False)
    assert result is user


def test_check_authorized_allowlist_returns_user():
    from cosyvoice_clone.api import _check_authorized

    user = _FakeUser(user_id="u-beta", role="user")
    result = _check_authorized(
        user, allowlist=["u-beta"], general_availability_enabled=False,
    )
    assert result is user


def test_check_authorized_ga_returns_user():
    from cosyvoice_clone.api import _check_authorized

    user = _FakeUser(user_id="u-anyone", role="user")
    result = _check_authorized(
        user, allowlist=[], general_availability_enabled=True,
    )
    assert result is user


def test_check_authorized_no_match_raises_403():
    from fastapi import HTTPException
    from cosyvoice_clone.api import _check_authorized

    user = _FakeUser(user_id="u-blocked", role="user")
    with pytest.raises(HTTPException) as exc_info:
        _check_authorized(user, allowlist=[], general_availability_enabled=False)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "forbidden_not_in_allowlist"


def test_check_authorized_none_user_raises_401():
    from fastapi import HTTPException
    from cosyvoice_clone.api import _check_authorized

    with pytest.raises(HTTPException) as exc_info:
        _check_authorized(None, allowlist=[], general_availability_enabled=True)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "unauthenticated"


def test_check_authorized_ga_overrides_allowlist_miss():
    """GA=True 时即使 user 不在 allowlist，也通过；证明 GA 是独立分支。"""
    from cosyvoice_clone.api import _check_authorized

    user = _FakeUser(user_id="u-not-in-list", role="user")
    result = _check_authorized(
        user, allowlist=["someone_else"], general_availability_enabled=True,
    )
    assert result is user


def test_check_authorized_ga_default_false_via_kwarg():
    """``general_availability_enabled`` 缺省值是 False（向后兼容旧 caller）。"""
    from fastapi import HTTPException
    from cosyvoice_clone.api import _check_authorized

    user = _FakeUser(user_id="u-blocked", role="user")
    with pytest.raises(HTTPException) as exc_info:
        # 不传 general_availability_enabled，看默认是不是 False
        _check_authorized(user, allowlist=[])
    assert exc_info.value.status_code == 403
