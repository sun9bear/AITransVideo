"""Phase 4.1 C.2: ``POST /api/voice/cosyvoice/clone`` endpoint 测试。

覆盖 17 条 fail-closed 错误路径 + happy path。**任何**测试路径都不调
真实 ffprobe / ffmpeg / DashScope / OSS。

策略：
- 用 FastAPI TestClient 直接命中 endpoint
- monkeypatch ``validate_sample_bytes`` / ``normalize_sample_for_dashscope``
  / ``build_mainland_voice_worker_client`` / ``load_settings`` / ``add_user_voice``
- DB session 用 in-memory SQLite + `database.get_db` override
- ``user`` / ``_is_admin`` 通过 `get_current_user` dependency override
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# gateway/ 已加 sys.path 由 conftest.py
import auth  # noqa: F401
from admin_settings import AdminSettings  # type: ignore[import-not-found]
from cosyvoice_clone import api as clone_api  # type: ignore[import-not-found]
from cosyvoice_clone.api import (  # type: ignore[import-not-found]
    CONSENT_MODAL_VERSION,
    router as clone_router,
)
from cosyvoice_clone.sample_validator import (  # type: ignore[import-not-found]
    ErrorCode as ValidatorErrorCode,
    SampleValidationResult,
)
from cosyvoice_clone.audio_processor import AudioProcessingError  # type: ignore[import-not-found]

from services.mainland_worker.client import (
    WorkerError,
    WorkerNetworkError,
    WorkerSignatureRejectedError,
)
from services.mainland_worker.types import (
    PLATFORM_DASHSCOPE_MAINLAND,
    PROVIDER_COSYVOICE_VOICE_CLONE,
    REGION_CONSTRAINT_MAINLAND_ONLY,
    TTS_PROVIDER_COSYVOICE,
    WorkerCloneResponse,
)


# ---------------------------------------------------------------------------
# Test fixtures: 最小 FastAPI app, dependency override, validator/transcoder mocks
# ---------------------------------------------------------------------------


class _FakeUser:
    """Stand-in for User ORM row."""

    def __init__(self, user_id: str = "u-test", role: str = "user"):
        self.id = user_id
        self.role = role


def _make_app() -> FastAPI:
    """构造仅含 cosyvoice_clone router 的最小 FastAPI app。

    避免拉起整个 gateway/main.py（会触发 DB / R2 / 等多种 startup）。
    """
    app = FastAPI()
    app.include_router(clone_router)
    return app


def _consent_form_fields() -> dict:
    """合法 consent 三件套（happy path）。"""
    return {
        "consent_voice_clone_confirmed": "true",
        "consent_modal_version": CONSENT_MODAL_VERSION,
        "consent_confirmed_at": "2026-05-25T03:00:00Z",
    }


def _default_form() -> dict:
    return {
        "target_model": "cosyvoice-v3.5-flash",
        "speaker_id": "speaker_a",
        "speaker_name": "Test Speaker",
        **_consent_form_fields(),
    }


def _post_clone(
    client: TestClient,
    *,
    sample_bytes: bytes = b"FAKE-AUDIO" * 200,
    sample_filename: str = "sample.wav",
    sample_content_type: str = "audio/wav",
    form: dict | None = None,
) -> httpx.Response:
    """触发 POST /api/voice/cosyvoice/clone 的便捷工具。"""
    form_data = _default_form()
    if form:
        form_data.update(form)
    files = {
        "sample": (sample_filename, io.BytesIO(sample_bytes), sample_content_type),
    }
    return client.post("/api/voice/cosyvoice/clone", data=form_data, files=files)


@pytest.fixture
def fake_admin_settings(monkeypatch) -> AdminSettings:
    """默认 admin settings：feature enabled / 当前 user 在 allowlist。"""
    s = AdminSettings(
        cosyvoice_clone_worker_enabled=True,
        cosyvoice_clone_user_allowlist=["u-test"],
        cosyvoice_clone_default_target_model="cosyvoice-v3.5-flash",
        cosyvoice_clone_max_voices_per_user=3,
        cosyvoice_clone_max_concurrent_jobs=2,
    )
    monkeypatch.setattr(clone_api, "load_settings", lambda: s)
    return s


@pytest.fixture
def fake_uploader(monkeypatch):
    """注入 ``InMemoryUploader``，返 ``mem://...`` URL。

    Codex 2026-05-25 C.2 二轮 review fix #1 + 部署前项 #A：endpoint 在
    Layer 3 做两步 fail-closed：

    1. ``cosyvoice_sample_uploader == "local_fs_stub"`` → 503
    2. ``backend not in PRODUCTION_READY_BACKENDS`` → 503

    测试默认让 Layer 3 放行：把 ``cosyvoice_sample_uploader`` 设为 ``aliyun_oss``
    并把 ``PRODUCTION_READY_BACKENDS`` 临时加入 ``aliyun_oss``，再用 InMemoryUploader
    真正承接 upload_and_sign 调用（模拟生产 OSS 配置已就绪的状态）。
    """
    from cosyvoice_clone.sample_uploader import InMemoryUploader  # type: ignore[import-not-found]

    uploader = InMemoryUploader()
    monkeypatch.setattr(
        clone_api,
        "build_sample_uploader_from_settings",
        lambda settings: uploader,
    )
    # Fix #1: 让 endpoint Layer 3 uploader-backend guard 放过测试
    monkeypatch.setattr(clone_api.gw_settings, "cosyvoice_sample_uploader", "aliyun_oss")
    monkeypatch.setattr(clone_api.gw_settings, "cosyvoice_oss_endpoint", "https://s3.oss-cn-beijing.aliyuncs.com")
    monkeypatch.setattr(clone_api.gw_settings, "cosyvoice_oss_bucket", "avt-cosyvoice-test")
    monkeypatch.setattr(clone_api.gw_settings, "cosyvoice_oss_access_key_id", "ak-test")
    monkeypatch.setattr(clone_api.gw_settings, "cosyvoice_oss_access_key_secret", "sk-test")
    # 部署前项 #A: 让 prod-ready 检查放行；模拟 AliyunOssUploader 已就绪
    monkeypatch.setattr(
        clone_api, "PRODUCTION_READY_BACKENDS", frozenset({"aliyun_oss"}),
    )
    return uploader


@pytest.fixture
def fake_worker_client(monkeypatch):
    """注入 mock worker client 返 happy CloneResponse。"""

    class _FakeClient:
        calls: list = []
        clone_response: Any = WorkerCloneResponse(
            ok=True,
            voice_id="cosyvoice_custom_test",
            provider=PROVIDER_COSYVOICE_VOICE_CLONE,
            tts_provider=TTS_PROVIDER_COSYVOICE,
            target_model="cosyvoice-v3.5-flash",
            region_constraint=REGION_CONSTRAINT_MAINLAND_ONLY,
            requires_worker=True,
            platform=PLATFORM_DASHSCOPE_MAINLAND,
            sample_sha256="a" * 64,
            created_at="2026-05-25T03:01:00Z",
            worker_request_id="wrk_test",
            provider_request_id="dashscope_req_test",
        )
        should_raise: Any = None
        close_count: int = 0

        def clone(self, req):
            self.calls.append(req)
            if self.should_raise:
                raise self.should_raise
            return self.clone_response

        def close(self):
            self.close_count += 1

    fake = _FakeClient()
    monkeypatch.setattr(
        clone_api,
        "build_mainland_voice_worker_client",
        lambda settings: fake,
    )
    return fake


@pytest.fixture
def fake_validator(monkeypatch):
    """默认 validator 返 ``is_valid=True`` happy path；测试可覆盖。"""

    happy = SampleValidationResult(
        is_valid=True,
        detected_format="wav",
        duration_ms=15_000,
        sample_rate_hz=16_000,
        channels=1,
        codec_name="pcm_s16le",
        bits_per_sample=16,
        size_bytes=320_000,
    )
    calls: list = []

    def _fake(data: bytes, **kwargs):
        calls.append({"size": len(data), "kwargs": kwargs})
        return _fake._next_result

    _fake._next_result = happy
    _fake.calls = calls
    monkeypatch.setattr(clone_api, "validate_sample_bytes", _fake)
    return _fake


@pytest.fixture
def fake_normalizer(monkeypatch):
    """默认 normalizer 返合法 WAV bytes；测试可覆盖。"""
    from services.mainland_worker.silent_wav import generate_silent_wav

    calls: list = []
    transcoded = generate_silent_wav(30_000)  # 30 秒静音

    def _fake(data: bytes, **kwargs):
        calls.append({"size": len(data), "kwargs": kwargs})
        if _fake._raise:
            raise _fake._raise
        return _fake._next_result

    _fake._next_result = transcoded
    _fake._raise = None
    _fake.calls = calls
    monkeypatch.setattr(clone_api, "normalize_sample_for_dashscope", _fake)
    return _fake


@pytest.fixture
def fake_db_add(monkeypatch):
    """``add_user_voice`` 替换为记录调用的 mock，避免真实 DB session。"""

    calls: list = []

    async def _fake_add(db, **kwargs):
        calls.append(kwargs)
        return object()  # 返一个 stand-in voice 对象

    monkeypatch.setattr(clone_api, "add_user_voice", _fake_add)
    return calls


@pytest.fixture
def fake_voice_count(monkeypatch):
    """``count_active_voices_for_user_and_provider`` 默认返 0；测试可覆盖。

    Codex 2026-05-25 C.2 二轮 review fix #3：Layer 7 配额检查会读 DB；
    默认不超额，但单测可以把 ``_next`` 设到 max 触发 409。
    """

    calls: list = []

    async def _fake_count(db, user_id, *, provider):
        calls.append({"user_id": user_id, "provider": provider})
        return _fake_count._next

    _fake_count._next = 0
    _fake_count.calls = calls
    monkeypatch.setattr(clone_api, "count_active_voices_for_user_and_provider", _fake_count)
    return _fake_count


@pytest.fixture
def fake_db_session(monkeypatch):
    """``get_db`` 注入一个 no-op AsyncSession。"""
    from database import get_db

    async def _fake_get_db():
        yield object()  # 任意 sentinel；fake_db_add 不实际用它

    return _fake_get_db


@pytest.fixture
def test_client(
    fake_admin_settings,
    fake_uploader,
    fake_worker_client,
    fake_validator,
    fake_normalizer,
    fake_db_add,
    fake_db_session,
    fake_voice_count,
) -> TestClient:
    """组装所有 fixture 的 TestClient（happy path 默认配置）。"""
    from auth import get_current_user
    from database import get_db

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: _FakeUser("u-test", "user")
    app.dependency_overrides[get_db] = fake_db_session
    return TestClient(app)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_returns_201_with_voice_metadata(
    test_client, fake_worker_client, fake_db_add,
) -> None:
    resp = _post_clone(test_client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["voice_id"] == "cosyvoice_custom_test"
    assert body["target_model"] == "cosyvoice-v3.5-flash"
    assert body["region_constraint"] == REGION_CONSTRAINT_MAINLAND_ONLY
    assert body["requires_worker"] is True
    assert body["platform"] == PLATFORM_DASHSCOPE_MAINLAND
    assert body["clone_api_model"] == "voice-enrollment"  # Codex 决策
    assert body["worker_request_id"] == "wrk_test"
    assert body["provider_request_id"] == "dashscope_req_test"


def test_happy_path_writes_user_voice_with_phase4_fields(
    test_client, fake_db_add,
) -> None:
    """plan §Phase 4.1 通过标准 锁死的字段必须落库。"""
    _post_clone(test_client)
    assert len(fake_db_add) == 1
    row = fake_db_add[0]
    # 5 个 Codex 锁死字段
    assert row["target_model"] == "cosyvoice-v3.5-flash"
    assert row["requires_worker"] is True
    assert row["region_constraint"] == REGION_CONSTRAINT_MAINLAND_ONLY
    assert row["clone_worker_request_id"] == "wrk_test"
    assert row["clone_provider_request_id"] == "dashscope_req_test"
    # billing_sku 保持 None（Codex 三轮决策）
    assert row["billing_sku"] is None
    # Other expected fields
    assert row["provider"] == PROVIDER_COSYVOICE_VOICE_CLONE
    assert row["tts_provider"] == TTS_PROVIDER_COSYVOICE
    assert row["platform"] == PLATFORM_DASHSCOPE_MAINLAND
    assert row["clone_api_model"] == "voice-enrollment"


def test_happy_path_invokes_worker_with_correct_payload(
    test_client, fake_worker_client,
) -> None:
    _post_clone(test_client, form={"speaker_id": "spk_x", "speaker_name": "X"})
    assert len(fake_worker_client.calls) == 1
    req = fake_worker_client.calls[0]
    assert req.user_id == "u-test"
    assert req.speaker_id == "spk_x"
    assert req.speaker_name == "X"
    assert req.target_model == "cosyvoice-v3.5-flash"
    assert req.consent.voice_clone_confirmed is True
    assert req.sample.kind == "download_url"
    assert req.sample.url.startswith("mem://")
    assert len(req.sample.sha256) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# Layer 1: 认证
# ---------------------------------------------------------------------------

def test_unauthenticated_returns_401(fake_admin_settings, fake_uploader,
                                      fake_worker_client, fake_validator,
                                      fake_normalizer, fake_db_add,
                                      fake_db_session, fake_voice_count) -> None:
    from auth import get_current_user
    from database import get_db

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: None  # 未登录
    app.dependency_overrides[get_db] = fake_db_session
    client = TestClient(app)
    resp = _post_clone(client)
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# Layer 2: 授权 allowlist
# ---------------------------------------------------------------------------

def test_user_not_in_allowlist_returns_403(monkeypatch, fake_uploader,
                                            fake_worker_client, fake_validator,
                                            fake_normalizer, fake_db_add,
                                            fake_db_session, fake_voice_count) -> None:
    s = AdminSettings(
        cosyvoice_clone_worker_enabled=True,
        cosyvoice_clone_user_allowlist=["u-other"],  # 不含 u-test
    )
    monkeypatch.setattr(clone_api, "load_settings", lambda: s)

    from auth import get_current_user
    from database import get_db

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: _FakeUser("u-test", "user")
    app.dependency_overrides[get_db] = fake_db_session
    client = TestClient(app)
    resp = _post_clone(client)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "forbidden_not_in_allowlist"


def test_admin_always_authorized_even_without_allowlist(monkeypatch, fake_uploader,
                                                         fake_worker_client, fake_validator,
                                                         fake_normalizer, fake_db_add,
                                                         fake_db_session, fake_voice_count) -> None:
    """admin 角色绕过 allowlist 检查（plan §Phase 4.1 §授权规则）。"""
    s = AdminSettings(
        cosyvoice_clone_worker_enabled=True,
        cosyvoice_clone_user_allowlist=[],  # 空 allowlist
    )
    monkeypatch.setattr(clone_api, "load_settings", lambda: s)

    from auth import get_current_user
    from database import get_db

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: _FakeUser("u-admin", "admin")
    app.dependency_overrides[get_db] = fake_db_session
    client = TestClient(app)
    resp = _post_clone(client)
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Layer 3: feature flag
# ---------------------------------------------------------------------------

def test_clone_feature_disabled_returns_503(monkeypatch, fake_uploader,
                                              fake_worker_client, fake_validator,
                                              fake_normalizer, fake_db_add,
                                              fake_db_session, fake_voice_count) -> None:
    s = AdminSettings(
        cosyvoice_clone_worker_enabled=False,  # 关
        cosyvoice_clone_user_allowlist=["u-test"],
    )
    monkeypatch.setattr(clone_api, "load_settings", lambda: s)

    from auth import get_current_user
    from database import get_db

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: _FakeUser("u-test", "user")
    app.dependency_overrides[get_db] = fake_db_session
    client = TestClient(app)
    resp = _post_clone(client)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "clone_feature_disabled"


# ---------------------------------------------------------------------------
# Layer 4: consent
# ---------------------------------------------------------------------------

def test_consent_not_confirmed_returns_400(test_client) -> None:
    resp = _post_clone(test_client, form={"consent_voice_clone_confirmed": "false"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "consent_required"


def test_consent_outdated_modal_version_returns_400(test_client) -> None:
    resp = _post_clone(test_client, form={"consent_modal_version": "2025-01-01-old"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "consent_outdated"


def test_consent_missing_confirmed_at_returns_400(test_client) -> None:
    resp = _post_clone(test_client, form={"consent_confirmed_at": "   "})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Layer 5: target_model
# ---------------------------------------------------------------------------

def test_invalid_target_model_returns_400(test_client) -> None:
    resp = _post_clone(test_client, form={"target_model": "cosyvoice-v3-old"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_target_model"


def test_target_model_plus_accepted(test_client, fake_worker_client) -> None:
    """flash + plus 双 voice：plan §Phase 4.2 UI 应一次提交 2 个 endpoint 调用。"""
    fake_worker_client.clone_response = WorkerCloneResponse(
        ok=True,
        voice_id="cosyvoice_custom_plus",
        provider=PROVIDER_COSYVOICE_VOICE_CLONE,
        tts_provider=TTS_PROVIDER_COSYVOICE,
        target_model="cosyvoice-v3.5-plus",
        region_constraint=REGION_CONSTRAINT_MAINLAND_ONLY,
        requires_worker=True,
        platform=PLATFORM_DASHSCOPE_MAINLAND,
        sample_sha256="b" * 64,
        created_at="2026-05-25T03:01:00Z",
        worker_request_id="wrk_test_plus",
        provider_request_id="req_plus",
    )
    resp = _post_clone(test_client, form={"target_model": "cosyvoice-v3.5-plus"})
    assert resp.status_code == 201
    assert resp.json()["target_model"] == "cosyvoice-v3.5-plus"


# ---------------------------------------------------------------------------
# Layer 6: sample validator fail-closed（不调 audio_processor / worker）
# ---------------------------------------------------------------------------

def test_sample_validator_failure_blocks_pipeline(
    test_client, fake_validator, fake_normalizer, fake_worker_client, fake_db_add,
) -> None:
    fake_validator._next_result = SampleValidationResult(
        is_valid=False,
        error_code=ValidatorErrorCode.DURATION_TOO_SHORT,
        error_message="500 ms < min 3000 ms (3s)",
        size_bytes=100_000,
    )
    resp = _post_clone(test_client)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == ValidatorErrorCode.DURATION_TOO_SHORT
    # fail-closed: 不调 normalizer / worker / DB
    assert fake_normalizer.calls == []
    assert fake_worker_client.calls == []
    assert fake_db_add == []


# ---------------------------------------------------------------------------
# Layer 7: audio_processor fail-closed
# ---------------------------------------------------------------------------

def test_audio_processor_failure_blocks_pipeline(
    test_client, fake_normalizer, fake_worker_client, fake_db_add,
) -> None:
    fake_normalizer._raise = AudioProcessingError(
        "ffmpeg failed", code="audio_processing_failed",
    )
    resp = _post_clone(test_client)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "audio_processing_failed"
    # fail-closed: 不调 worker / DB
    assert fake_worker_client.calls == []
    assert fake_db_add == []


# ---------------------------------------------------------------------------
# Layer 8: worker disabled / network / business errors
# ---------------------------------------------------------------------------

def test_worker_disabled_returns_503(monkeypatch, fake_admin_settings, fake_uploader,
                                     fake_validator, fake_normalizer, fake_db_add,
                                     fake_db_session, fake_voice_count) -> None:
    """build_mainland_voice_worker_client 返 None → 503，不写 DB/不上传 sample。"""
    monkeypatch.setattr(
        clone_api, "build_mainland_voice_worker_client", lambda s: None,
    )
    from auth import get_current_user
    from database import get_db

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: _FakeUser("u-test", "user")
    app.dependency_overrides[get_db] = fake_db_session
    client = TestClient(app)
    resp = _post_clone(client)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "worker_disabled"
    assert fake_uploader.calls == []
    assert fake_db_add == []


def test_worker_network_error_returns_502(test_client, fake_worker_client, fake_db_add) -> None:
    fake_worker_client.should_raise = WorkerNetworkError("simulated DNS fail")
    resp = _post_clone(test_client)
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "worker_unreachable"
    assert fake_db_add == []


def test_worker_signature_rejection_returns_502(
    test_client, fake_worker_client, fake_db_add,
) -> None:
    fake_worker_client.should_raise = WorkerSignatureRejectedError(
        "bad signature", code="signature_invalid", http_status=401,
    )
    resp = _post_clone(test_client)
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "worker_auth_error"
    assert fake_db_add == []


def test_worker_business_error_returns_502(test_client, fake_worker_client, fake_db_add) -> None:
    fake_worker_client.should_raise = WorkerError(
        "sample too large at worker", code="sample_too_large", http_status=400,
    )
    resp = _post_clone(test_client)
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "worker_sample_too_large"
    assert fake_db_add == []


# ---------------------------------------------------------------------------
# 9: 整链 fail-closed 顺序守卫（关键属性）
# ---------------------------------------------------------------------------

def test_fail_closed_order_consent_first_then_validator_then_normalizer(
    test_client, fake_validator, fake_normalizer, fake_worker_client, fake_db_add,
) -> None:
    """consent 错时不调 validator / normalizer / worker / DB。"""
    resp = _post_clone(
        test_client,
        form={"consent_voice_clone_confirmed": "false"},
    )
    assert resp.status_code == 400
    # Phase 1: consent 拒后，下游全部不调
    assert fake_validator.calls == []
    assert fake_normalizer.calls == []
    assert fake_worker_client.calls == []
    assert fake_db_add == []


def test_fail_closed_target_model_before_sample_io(
    test_client, fake_validator, fake_normalizer, fake_worker_client, fake_db_add,
) -> None:
    """target_model 错时不读 sample（避免大文件白白上传到 Gateway 进程）。

    注：FastAPI multipart 解析阶段 sample 已经被读到内存，但 endpoint 内部
    不调 validator → 不调 normalizer → 不调 worker → 不写 DB。
    """
    resp = _post_clone(test_client, form={"target_model": "invalid-model"})
    assert resp.status_code == 400
    assert fake_validator.calls == []
    assert fake_normalizer.calls == []
    assert fake_worker_client.calls == []
    assert fake_db_add == []


# ---------------------------------------------------------------------------
# Single target_model 守卫（plan §Phase 4.1：拒"复合 flash+plus"）
# ---------------------------------------------------------------------------

def test_endpoint_accepts_single_target_model_only(test_client) -> None:
    """target_model 是单 str field；前端无法同时传 flash + plus（FastAPI Form
    单值），后端再用白名单兜底。"""
    # 即便前端传 "flash,plus" 试图组合，也不在 ALLOWED 集合里 → 400
    resp = _post_clone(test_client, form={"target_model": "cosyvoice-v3.5-flash,cosyvoice-v3.5-plus"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_target_model"


# ---------------------------------------------------------------------------
# Audit log（plan §账单观测 简版守卫）
# ---------------------------------------------------------------------------

def test_audit_log_emitted_on_success(test_client, caplog) -> None:
    """成功 clone 后必须有 ``cosyvoice_clone_audit`` log（含 worker_request_id）。"""
    import logging
    with caplog.at_level(logging.INFO, logger="cosyvoice_clone.api"):
        _post_clone(test_client)
    audit_lines = [r.getMessage() for r in caplog.records if "cosyvoice_clone_audit" in r.getMessage()]
    assert audit_lines, "audit log should be emitted on success"
    audit_text = audit_lines[0]
    assert "wrk_test" in audit_text
    assert "dashscope_req_test" in audit_text
    assert "cosyvoice_custom_test" in audit_text
    assert "u-test" in audit_text


# ---------------------------------------------------------------------------
# source_segments JSON 解析
# ---------------------------------------------------------------------------

def test_source_segments_parsed_as_int_list(test_client, fake_db_add) -> None:
    _post_clone(test_client, form={"source_segments": "[1, 2, 3]"})
    assert fake_db_add[0]["clone_sample_segment_ids"] == [1, 2, 3]


def test_source_segments_invalid_json_returns_400(test_client) -> None:
    resp = _post_clone(test_client, form={"source_segments": "not-json"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_source_segments"


def test_source_segments_omitted_means_empty_list(test_client, fake_db_add) -> None:
    """``source_segments`` 字段省略时落库为 None（不是 []）。"""
    _post_clone(test_client)
    # _default_form 没传 source_segments
    assert fake_db_add[0]["clone_sample_segment_ids"] is None


# ---------------------------------------------------------------------------
# 关键不变量守卫（Codex 2026-05-25 决策必须落库的字段）
# ---------------------------------------------------------------------------

def test_user_voices_row_locks_codex_required_fields(test_client, fake_db_add) -> None:
    """plan §Phase 4.1 通过标准 + Codex C.2 锁死的字段名集合。"""
    _post_clone(test_client)
    row = fake_db_add[0]
    required_keys = {
        "target_model",
        "requires_worker",
        "region_constraint",
        "clone_worker_request_id",
        "clone_provider_request_id",
    }
    for k in required_keys:
        assert k in row, f"add_user_voice 缺 {k!r}（Codex C.2 锁死字段）"
        assert row[k] is not None, f"{k} 必须落非空值（实际 None）"


def test_billing_sku_stays_none(test_client, fake_db_add) -> None:
    """Codex 三轮 finding：billing_sku 等首次实账单回填，C.2 不写死字符串。"""
    _post_clone(test_client)
    assert fake_db_add[0]["billing_sku"] is None


# ===========================================================================
# Codex 2026-05-25 C.2 二轮 review — 5 fix 守卫测试集
# ===========================================================================

# ---------------------------------------------------------------------------
# Fix #1: 生产路径不能默认 LocalFsStubUploader
# ---------------------------------------------------------------------------

def test_local_fs_stub_uploader_blocks_endpoint_when_worker_enabled(
    monkeypatch, fake_admin_settings, fake_worker_client, fake_validator,
    fake_normalizer, fake_db_add, fake_db_session, fake_voice_count,
) -> None:
    """worker enabled 但 uploader 仍是 local_fs_stub → 503，且不调任何下游。

    Codex 2026-05-25 C.2 二轮 review fix #1：生产 / worker enabled 时禁止
    默认 stub uploader。fail-closed：不读样本 / 不转码 / 不上传 / 不调 worker。
    """
    # 关键：把 gw_settings.cosyvoice_sample_uploader 设回 stub
    monkeypatch.setattr(clone_api.gw_settings, "cosyvoice_sample_uploader", "local_fs_stub")

    # 即便 build_sample_uploader_from_settings 被 mock 也无所谓——endpoint 在
    # 调用它之前就 503 了。这里不重新 mock，沿用 fake_uploader 已 patch 的
    # InMemoryUploader 工厂；目标是验 endpoint 用 gw_settings 字段先 gate。
    from cosyvoice_clone.sample_uploader import InMemoryUploader  # type: ignore[import-not-found]
    uploader = InMemoryUploader()
    monkeypatch.setattr(
        clone_api, "build_sample_uploader_from_settings", lambda s: uploader,
    )

    from auth import get_current_user
    from database import get_db

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: _FakeUser("u-test", "user")
    app.dependency_overrides[get_db] = fake_db_session
    client = TestClient(app)
    resp = _post_clone(client)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "sample_uploader_not_configured"
    # fail-closed: 一切下游都不调
    assert fake_validator.calls == []
    assert fake_normalizer.calls == []
    assert fake_worker_client.calls == []
    assert fake_db_add == []
    # uploader 也没被实际调用（factory 调了但 upload_and_sign 没调）
    assert uploader.calls == []


def test_aliyun_oss_uploader_backend_passes_layer3_guard(
    monkeypatch, fake_admin_settings, fake_uploader, fake_worker_client,
    fake_validator, fake_normalizer, fake_db_add, fake_db_session,
    fake_voice_count,
) -> None:
    """``cosyvoice_sample_uploader=aliyun_oss`` 且在 PRODUCTION_READY_BACKENDS 时 Layer 3 放行。"""
    # fake_uploader 已 monkeypatch backend + PRODUCTION_READY_BACKENDS

    from auth import get_current_user
    from database import get_db

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: _FakeUser("u-test", "user")
    app.dependency_overrides[get_db] = fake_db_session
    client = TestClient(app)
    resp = _post_clone(client)
    assert resp.status_code == 201, resp.text


def test_aliyun_oss_configured_but_required_config_missing_returns_503(
    monkeypatch, fake_admin_settings, fake_worker_client, fake_validator,
    fake_normalizer, fake_db_add, fake_db_session, fake_voice_count,
) -> None:
    """backend=aliyun_oss 但 OSS 必需配置缺失 → 503，
    且不读样本 / 不转码 / 不上传 / 不调付费 worker。
    """
    monkeypatch.setattr(clone_api.gw_settings, "cosyvoice_sample_uploader", "aliyun_oss")
    monkeypatch.setattr(clone_api, "PRODUCTION_READY_BACKENDS", frozenset({"aliyun_oss"}))
    monkeypatch.setattr(clone_api.gw_settings, "cosyvoice_oss_endpoint", "")
    monkeypatch.setattr(clone_api.gw_settings, "cosyvoice_oss_bucket", "")
    monkeypatch.setattr(clone_api.gw_settings, "cosyvoice_oss_access_key_id", "")
    monkeypatch.setattr(clone_api.gw_settings, "cosyvoice_oss_access_key_secret", "")

    def _factory_should_not_be_called(settings):
        raise AssertionError("Layer 3 config gate 应该早期 fail-closed，不应该走到工厂")
    monkeypatch.setattr(
        clone_api, "build_sample_uploader_from_settings", _factory_should_not_be_called,
    )

    from auth import get_current_user
    from database import get_db

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: _FakeUser("u-test", "user")
    app.dependency_overrides[get_db] = fake_db_session
    client = TestClient(app)
    resp = _post_clone(client)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "sample_uploader_config_missing"
    # fail-closed: 下游什么都不该调
    assert fake_validator.calls == []
    assert fake_normalizer.calls == []
    assert fake_worker_client.calls == []
    assert fake_db_add == []


def test_sample_uploader_factory_builds_aliyun_oss_uploader() -> None:
    """Phase 4.1.x: ``aliyun_oss`` 工厂应构造真实 AliyunOssUploader。"""
    from cosyvoice_clone.sample_uploader import (  # type: ignore[import-not-found]
        AliyunOssUploader,
        build_sample_uploader_from_settings,
        IMPLEMENTED_BACKENDS,
        KNOWN_BACKENDS,
        PRODUCTION_READY_BACKENDS,
    )

    assert "aliyun_oss" in KNOWN_BACKENDS
    assert "aliyun_oss" in IMPLEMENTED_BACKENDS
    assert "aliyun_oss" in PRODUCTION_READY_BACKENDS

    class _S:
        cosyvoice_sample_uploader = "aliyun_oss"
        cosyvoice_sample_local_dir = ""
        cosyvoice_oss_endpoint = "https://s3.oss-cn-beijing.aliyuncs.com"
        cosyvoice_oss_bucket = "avt-cosyvoice-test"
        cosyvoice_oss_access_key_id = "ak-test"
        cosyvoice_oss_access_key_secret = "sk-test"
        cosyvoice_oss_region = "cn-beijing"
        cosyvoice_oss_key_prefix = "cosyvoice/clone-samples"
        cosyvoice_oss_connect_timeout_s = 10
        cosyvoice_oss_read_timeout_s = 30

    uploader = build_sample_uploader_from_settings(_S())

    assert isinstance(uploader, AliyunOssUploader)
    assert uploader.bucket == "avt-cosyvoice-test"


def test_sample_uploader_factory_raises_valueerror_for_unknown_backend() -> None:
    """未知 backend（不在 KNOWN_BACKENDS）→ ValueError，区别于 NotImplementedError。"""
    from cosyvoice_clone.sample_uploader import (  # type: ignore[import-not-found]
        build_sample_uploader_from_settings,
    )

    class _S:
        cosyvoice_sample_uploader = "totally_made_up_backend"
        cosyvoice_sample_local_dir = ""

    with pytest.raises(ValueError, match="Unknown"):
        build_sample_uploader_from_settings(_S())


def test_uploaded_sample_is_deleted_after_worker_clone(
    monkeypatch, test_client, fake_worker_client,
) -> None:
    """AliyunOssUploader 支持 cleanup 时，worker clone 后应删除临时 sample object。"""

    class _DeletingUploader:
        def __init__(self):
            self.uploads: list[str] = []
            self.deletes: list[str] = []

        def upload_and_sign(self, data: bytes, *, filename_hint: str, ttl_seconds: int) -> str:
            url = "https://bucket.s3.oss-cn-beijing.aliyuncs.com/cosyvoice/sample.wav?sig=1"
            self.uploads.append(url)
            return url

        def delete_uploaded_url(self, url: str) -> None:
            self.deletes.append(url)

    uploader = _DeletingUploader()
    monkeypatch.setattr(
        clone_api,
        "build_sample_uploader_from_settings",
        lambda settings: uploader,
    )

    resp = _post_clone(test_client)

    assert resp.status_code == 201, resp.text
    assert uploader.uploads == [
        "https://bucket.s3.oss-cn-beijing.aliyuncs.com/cosyvoice/sample.wav?sig=1"
    ]
    assert uploader.deletes == uploader.uploads
    assert fake_worker_client.calls


def test_uploaded_sample_cleanup_failure_does_not_mask_worker_success(
    monkeypatch, test_client,
) -> None:
    """sample cleanup 是 best-effort，失败不应把成功 clone 改成 5xx。"""

    class _FailingDeleteUploader:
        def upload_and_sign(self, data: bytes, *, filename_hint: str, ttl_seconds: int) -> str:
            return "https://bucket.s3.oss-cn-beijing.aliyuncs.com/cosyvoice/sample.wav?sig=1"

        def delete_uploaded_url(self, url: str) -> None:
            raise RuntimeError("delete failed")

    monkeypatch.setattr(
        clone_api,
        "build_sample_uploader_from_settings",
        lambda settings: _FailingDeleteUploader(),
    )

    resp = _post_clone(test_client)

    assert resp.status_code == 201, resp.text


def test_uploaded_sample_is_deleted_after_worker_error(
    monkeypatch, test_client, fake_worker_client,
) -> None:
    """worker 报错时也要清理已经上传的临时 sample object。"""

    class _DeletingUploader:
        def __init__(self):
            self.deletes: list[str] = []

        def upload_and_sign(self, data: bytes, *, filename_hint: str, ttl_seconds: int) -> str:
            return "https://bucket.s3.oss-cn-beijing.aliyuncs.com/cosyvoice/sample.wav?sig=1"

        def delete_uploaded_url(self, url: str) -> None:
            self.deletes.append(url)

    uploader = _DeletingUploader()
    monkeypatch.setattr(
        clone_api,
        "build_sample_uploader_from_settings",
        lambda settings: uploader,
    )
    fake_worker_client.should_raise = WorkerNetworkError("network down")

    resp = _post_clone(test_client)

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "worker_unreachable"
    assert uploader.deletes == [
        "https://bucket.s3.oss-cn-beijing.aliyuncs.com/cosyvoice/sample.wav?sig=1"
    ]


def test_sample_upload_failure_closes_worker_client(
    monkeypatch, test_client, fake_worker_client, fake_db_add,
) -> None:
    """Codex PR #8 P2: uploader failure must close the already-built worker client."""

    class _FailingUpload:
        def upload_and_sign(self, data: bytes, *, filename_hint: str, ttl_seconds: int) -> str:
            raise RuntimeError("OSS upload failed")

    monkeypatch.setattr(
        clone_api,
        "build_sample_uploader_from_settings",
        lambda settings: _FailingUpload(),
    )

    resp = _post_clone(test_client)

    assert resp.status_code == 500
    assert resp.json()["detail"]["code"] == "sample_upload_failed"
    assert fake_worker_client.calls == []
    assert fake_worker_client.close_count == 1
    assert fake_db_add == []


# ---------------------------------------------------------------------------
# Fix #2: 写库前必须校验 worker 回包 target_model
# ---------------------------------------------------------------------------

def test_worker_target_model_mismatch_blocks_db_write_and_returns_502(
    test_client, fake_worker_client, fake_db_add,
) -> None:
    """worker 返回的 voice 的 target_model ≠ 请求 target_model → 502，不写库。

    Codex 2026-05-25 C.2 二轮 review fix #2：voice_id ↔ target_model 强绑定
    约束。worker bug 让 mismatch 落库会导致后续 TTS 全失败。
    """
    # 请求 flash 但 worker 错误地返回 plus 的 voice
    fake_worker_client.clone_response = WorkerCloneResponse(
        ok=True,
        voice_id="cosyvoice_custom_mismatch",
        provider=PROVIDER_COSYVOICE_VOICE_CLONE,
        tts_provider=TTS_PROVIDER_COSYVOICE,
        target_model="cosyvoice-v3.5-plus",  # ⚠️ 不匹配请求的 flash
        region_constraint=REGION_CONSTRAINT_MAINLAND_ONLY,
        requires_worker=True,
        platform=PLATFORM_DASHSCOPE_MAINLAND,
        sample_sha256="c" * 64,
        created_at="2026-05-25T03:01:00Z",
        worker_request_id="wrk_mismatch",
        provider_request_id="req_mismatch",
    )
    resp = _post_clone(test_client, form={"target_model": "cosyvoice-v3.5-flash"})
    assert resp.status_code == 502
    body = resp.json()
    assert body["detail"]["code"] == "worker_target_model_mismatch"
    assert body["detail"]["worker_request_id"] == "wrk_mismatch"
    # 严格守卫：DB 不能落库
    assert fake_db_add == []


def test_worker_target_model_mismatch_triggers_best_effort_delete(
    test_client, fake_worker_client, fake_db_add,
) -> None:
    """mismatch 时 endpoint 必须调用 ``worker_client.delete_voice`` 做 cleanup。

    bounded best-effort：只调一次，delete 失败也不阻塞 502 返回。
    """
    delete_calls: list = []

    def _record_delete(voice_id, req):
        delete_calls.append({"voice_id": voice_id, "reason": req.reason})

    fake_worker_client.delete_voice = _record_delete  # type: ignore[attr-defined]
    fake_worker_client.clone_response = WorkerCloneResponse(
        ok=True,
        voice_id="cosyvoice_to_delete",
        provider=PROVIDER_COSYVOICE_VOICE_CLONE,
        tts_provider=TTS_PROVIDER_COSYVOICE,
        target_model="cosyvoice-v3.5-plus",
        region_constraint=REGION_CONSTRAINT_MAINLAND_ONLY,
        requires_worker=True,
        platform=PLATFORM_DASHSCOPE_MAINLAND,
        sample_sha256="d" * 64,
        created_at="2026-05-25T03:01:00Z",
        worker_request_id="wrk_for_delete",
        provider_request_id="req_for_delete",
    )
    resp = _post_clone(test_client, form={"target_model": "cosyvoice-v3.5-flash"})
    assert resp.status_code == 502
    # 已发起 best-effort delete
    assert len(delete_calls) == 1
    assert delete_calls[0]["voice_id"] == "cosyvoice_to_delete"
    assert delete_calls[0]["reason"] == "target_model_mismatch_rollback"
    assert fake_db_add == []


def test_worker_target_model_mismatch_delete_failure_does_not_block_502(
    test_client, fake_worker_client, fake_db_add,
) -> None:
    """delete_voice 抛异常时仍返 502，cleanup 失败不阻塞用户响应。"""
    def _failing_delete(voice_id, req):
        raise RuntimeError("simulated worker delete network error")

    fake_worker_client.delete_voice = _failing_delete  # type: ignore[attr-defined]
    fake_worker_client.clone_response = WorkerCloneResponse(
        ok=True,
        voice_id="cosyvoice_delete_fail",
        provider=PROVIDER_COSYVOICE_VOICE_CLONE,
        tts_provider=TTS_PROVIDER_COSYVOICE,
        target_model="cosyvoice-v3.5-plus",
        region_constraint=REGION_CONSTRAINT_MAINLAND_ONLY,
        requires_worker=True,
        platform=PLATFORM_DASHSCOPE_MAINLAND,
        sample_sha256="e" * 64,
        created_at="2026-05-25T03:01:00Z",
        worker_request_id="wrk_delfail",
        provider_request_id="req_delfail",
    )
    resp = _post_clone(test_client, form={"target_model": "cosyvoice-v3.5-flash"})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "worker_target_model_mismatch"
    assert fake_db_add == []


# ---------------------------------------------------------------------------
# Fix #3: max_voices_per_user 配额（付费前 gate）
# ---------------------------------------------------------------------------

def test_voice_quota_at_limit_returns_409_before_worker_call(
    test_client, fake_voice_count, fake_worker_client, fake_db_add,
    fake_normalizer, fake_validator, fake_uploader,
) -> None:
    """已达 max_voices_per_user 上限 → 409 ``voice_quota_exceeded``，不调付费 worker。

    Codex 2026-05-25 C.2 二轮 review fix #3：admin_settings 默认 max=3，
    模拟 user 已有 3 个 active CosyVoice 音色 → 拒绝新 clone。
    """
    fake_voice_count._next = 3  # 已满
    resp = _post_clone(test_client)
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "voice_quota_exceeded"
    assert body["detail"]["current"] == 3
    assert body["detail"]["limit"] == 3
    # fail-closed: 不读样本 / 不调 normalizer / uploader / worker / DB
    assert fake_validator.calls == []
    assert fake_normalizer.calls == []
    assert fake_uploader.calls == []
    assert fake_worker_client.calls == []
    assert fake_db_add == []


def test_voice_quota_below_limit_proceeds(
    test_client, fake_voice_count, fake_worker_client, fake_db_add,
) -> None:
    """quota 未满（2/3）→ 通过 Layer 7，正常 clone。"""
    fake_voice_count._next = 2
    resp = _post_clone(test_client)
    assert resp.status_code == 201
    assert fake_db_add and fake_db_add[0]["target_model"] == "cosyvoice-v3.5-flash"


def test_voice_quota_queries_cosyvoice_clone_provider_specifically(
    test_client, fake_voice_count,
) -> None:
    """quota 查询的 provider 必须是 ``cosyvoice_voice_clone``——不是全 provider 计数。

    确保 user 在其它 provider（MiniMax / VolcEngine 等）下的音色不进 quota。
    """
    _post_clone(test_client)
    assert len(fake_voice_count.calls) == 1
    call = fake_voice_count.calls[0]
    assert call["provider"] == PROVIDER_COSYVOICE_VOICE_CLONE


def test_voice_quota_lock_acquired_before_count_and_worker_call(
    monkeypatch, test_client, fake_worker_client,
) -> None:
    """PR #7 Codex review: quota guard must serialize before paid clone."""
    events: list[str] = []

    async def _fake_lock(db, *, user_id, provider):
        events.append(f"lock:{provider}:{user_id}")
        return True

    async def _fake_count(db, user_id, *, provider):
        events.append(f"count:{provider}:{user_id}")
        return 2

    original_clone = fake_worker_client.clone

    def _clone_with_event(req):
        events.append("clone")
        return original_clone(req)

    monkeypatch.setattr(clone_api, "_acquire_clone_quota_transaction_lock", _fake_lock)
    monkeypatch.setattr(clone_api, "count_active_voices_for_user_and_provider", _fake_count)
    fake_worker_client.clone = _clone_with_event

    resp = _post_clone(test_client)

    assert resp.status_code == 201, resp.text
    assert events[:3] == [
        f"lock:{PROVIDER_COSYVOICE_VOICE_CLONE}:u-test",
        f"count:{PROVIDER_COSYVOICE_VOICE_CLONE}:u-test",
        "clone",
    ]


def test_voice_quota_zero_max_skips_quota_lock(
    monkeypatch, fake_uploader, fake_worker_client, fake_validator,
    fake_normalizer, fake_db_add, fake_db_session,
) -> None:
    """When admin disables max_voices_per_user, no quota lock is acquired."""
    s = AdminSettings(
        cosyvoice_clone_worker_enabled=True,
        cosyvoice_clone_user_allowlist=["u-test"],
        cosyvoice_clone_max_voices_per_user=0,
    )
    monkeypatch.setattr(clone_api, "load_settings", lambda: s)

    async def _unexpected_lock(*args, **kwargs):
        raise AssertionError("quota lock must not be acquired when max_voices_per_user=0")

    monkeypatch.setattr(clone_api, "_acquire_clone_quota_transaction_lock", _unexpected_lock)

    from auth import get_current_user
    from database import get_db

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: _FakeUser("u-test", "user")
    app.dependency_overrides[get_db] = fake_db_session
    client = TestClient(app)

    resp = _post_clone(client)

    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_clone_quota_lock_uses_postgresql_transaction_advisory_lock() -> None:
    """Lock helper must use pg_advisory_xact_lock, not a non-locking count only."""
    statements: list[tuple[object, dict | None]] = []

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    class _FakeDb:
        def get_bind(self):
            return _Bind()

        async def execute(self, stmt, params=None):
            statements.append((stmt, params))

    acquired = await clone_api._acquire_clone_quota_transaction_lock(
        _FakeDb(), user_id="u-test", provider=PROVIDER_COSYVOICE_VOICE_CLONE,
    )

    assert acquired is True
    assert len(statements) == 1
    assert "pg_advisory_xact_lock" in str(statements[0][0])
    assert isinstance(statements[0][1]["lock_key"], int)


@pytest.mark.asyncio
async def test_clone_quota_lock_noops_for_non_postgresql_sessions() -> None:
    """Local SQLite/fake sessions stay clean-local and do not attempt pg locks."""
    class _Dialect:
        name = "sqlite"

    class _Bind:
        dialect = _Dialect()

    class _FakeDb:
        def get_bind(self):
            return _Bind()

        async def execute(self, stmt, params=None):
            raise AssertionError("sqlite tests must not execute PostgreSQL advisory locks")

    acquired = await clone_api._acquire_clone_quota_transaction_lock(
        _FakeDb(), user_id="u-test", provider=PROVIDER_COSYVOICE_VOICE_CLONE,
    )

    assert acquired is False


def test_clone_quota_lock_key_is_stable_and_provider_scoped() -> None:
    key_a1 = clone_api._clone_quota_lock_key(
        user_id="u-test", provider=PROVIDER_COSYVOICE_VOICE_CLONE,
    )
    key_a2 = clone_api._clone_quota_lock_key(
        user_id="u-test", provider=PROVIDER_COSYVOICE_VOICE_CLONE,
    )
    key_b = clone_api._clone_quota_lock_key(
        user_id="u-test", provider="minimax_voice_clone",
    )

    assert key_a1 == key_a2
    assert key_a1 != key_b
    assert -(2 ** 63) <= key_a1 < 2 ** 63


def test_quota_lock_call_is_before_count_and_paid_worker_clone() -> None:
    """Static guard: future refactors must not move the lock after count/clone."""
    import inspect

    source = inspect.getsource(clone_api.cosyvoice_clone)
    lock_pos = source.index("_acquire_clone_quota_transaction_lock")
    count_pos = source.index("count_active_voices_for_user_and_provider")
    clone_pos = source.index("worker_client.clone")

    assert lock_pos < count_pos < clone_pos


def test_voice_quota_zero_max_disables_quota_check(
    monkeypatch, fake_uploader, fake_worker_client, fake_validator,
    fake_normalizer, fake_db_add, fake_db_session, fake_voice_count,
) -> None:
    """``cosyvoice_clone_max_voices_per_user=0`` 时不做配额检查（admin 关闭 gate）。"""
    s = AdminSettings(
        cosyvoice_clone_worker_enabled=True,
        cosyvoice_clone_user_allowlist=["u-test"],
        cosyvoice_clone_max_voices_per_user=0,  # 关
    )
    monkeypatch.setattr(clone_api, "load_settings", lambda: s)
    fake_voice_count._next = 100  # 即便已有很多音色也不会触发 quota

    from auth import get_current_user
    from database import get_db

    app = _make_app()
    app.dependency_overrides[get_current_user] = lambda: _FakeUser("u-test", "user")
    app.dependency_overrides[get_db] = fake_db_session
    client = TestClient(app)
    resp = _post_clone(client)
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Fix #4: source_segments 提前解析（read sample 之前）
# ---------------------------------------------------------------------------

def test_invalid_source_segments_blocks_pipeline_before_sample_io(
    test_client, fake_validator, fake_normalizer, fake_uploader,
    fake_worker_client, fake_db_add,
) -> None:
    """无效 JSON ``source_segments`` 必须在读样本前 400，不调下游任何 I/O。

    Codex 2026-05-25 C.2 二轮 review fix #4：原方案在上传样本后才 parse，
    无效 JSON 会浪费一次转码 + 上传。新顺序：解析放在 Layer 6。
    """
    resp = _post_clone(test_client, form={"source_segments": "not-valid-json"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_source_segments"
    # 严格守卫：sample 没被 validate / 转码 / 上传 / worker / DB
    assert fake_validator.calls == []
    assert fake_normalizer.calls == []
    assert fake_uploader.calls == []
    assert fake_worker_client.calls == []
    assert fake_db_add == []


# ---------------------------------------------------------------------------
# Fix #5: consent 严格 literal "true" 校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("loose_truthy_value", ["1", "yes", "on", "True", "TRUE", "YES"])
def test_consent_strict_only_literal_true_accepted(
    test_client, fake_db_add, fake_worker_client, loose_truthy_value,
) -> None:
    """FastAPI ``bool = Form(...)`` 接受 ``"1" / "yes" / "on"`` 太宽松——
    consent 必须严格只接受 literal ``"true"``。

    Codex 2026-05-25 C.2 二轮 review fix #5：避免前端意外发 ``"1"`` 就过授权关。
    """
    resp = _post_clone(
        test_client, form={"consent_voice_clone_confirmed": loose_truthy_value},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "consent_required"
    # fail-closed: 不调付费路径
    assert fake_worker_client.calls == []
    assert fake_db_add == []


@pytest.mark.parametrize("falsy_value", ["false", "False", "0", "no", "off", ""])
def test_consent_falsy_values_also_rejected(
    test_client, fake_db_add, fake_worker_client, falsy_value,
) -> None:
    """``"false" / "0" / 空字符串`` 等显式 falsy 也必须被 ``consent_required`` 拒。"""
    resp = _post_clone(
        test_client, form={"consent_voice_clone_confirmed": falsy_value},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "consent_required"
    assert fake_worker_client.calls == []
    assert fake_db_add == []


def test_consent_literal_true_lowercase_accepted(test_client) -> None:
    """严格 ``"true"`` literal 大小写敏感 → happy path 必须用小写 ``"true"``。"""
    resp = _post_clone(test_client, form={"consent_voice_clone_confirmed": "true"})
    assert resp.status_code == 201
