"""Phase 4.3a PR1-E1 — Express auto-clone internal sample upload endpoint。

锁定 spec §5.5 完整安全合同：
- X-Internal-Key 鉴权（401）
- content-type 白名单（415）
- size cap 2MB + 空 body（413 / 400）
- user_id / job_id / speaker_id regex（400）
- uploader 未配 / runtime error（503）
- happy path（200 返 presigned_get_url + sha256 + expires_at）
- 日志脱敏（不 log presigned URL / OSS secret / bytes）
- 不调武汉 worker

策略（与 test_cosyvoice_clone_api.py 同模式）：FastAPI TestClient 命中
internal_router，monkeypatch internal_api_key + build_sample_uploader_from_settings。
不调真实 OSS / boto3 / worker。
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

_GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))

from cosyvoice_clone import api as clone_api  # type: ignore[import-not-found]
from cosyvoice_clone.api import internal_router  # type: ignore[import-not-found]


_TEST_KEY = "phase43a-e1-internal-key"
# fake signed URL 故意含 OSS 签名样式 substring，用于日志脱敏断言
_FAKE_SIGNED_URL = (
    "https://avt-clone.oss-cn-shanghai.aliyuncs.com/cosyvoice/clone-samples/"
    "2026/05/28/abc123_deadbeef.wav?OSSAccessKeyId=LTAI_SECRET_AK"
    "&Signature=FORGEDSIGVALUE123&Expires=1234567890"
)


class _FakeUploader:
    def __init__(self, *, url: str = _FAKE_SIGNED_URL, raise_runtime: bool = False):
        self._url = url
        self._raise_runtime = raise_runtime
        self.calls: list[dict] = []

    def upload_and_sign(self, data, *, filename_hint="sample.wav", ttl_seconds=3600):
        self.calls.append({
            "len": len(data),
            "filename_hint": filename_hint,
            "ttl_seconds": ttl_seconds,
        })
        if self._raise_runtime:
            raise RuntimeError("simulated OSS 5xx")
        return self._url


def _make_client(monkeypatch, *, uploader=None, uploader_raises_build=False,
                 internal_key=_TEST_KEY):
    """构造仅含 internal_router 的最小 FastAPI app + monkeypatch 依赖。"""
    monkeypatch.setattr(clone_api.gw_settings, "internal_api_key", internal_key, raising=False)

    if uploader_raises_build:
        def _build(_settings):
            raise ValueError("AVT_COSYVOICE_OSS_* not configured")
        monkeypatch.setattr(clone_api, "build_sample_uploader_from_settings", _build, raising=True)
    elif uploader is not None:
        monkeypatch.setattr(
            clone_api, "build_sample_uploader_from_settings",
            lambda _settings: uploader, raising=True,
        )

    app = FastAPI()
    app.include_router(internal_router)
    return TestClient(app)


_URL = "/api/internal/cosyvoice/express-sample-upload"
_VALID_USER = "00000000-0000-0000-0000-0000000000a1"
_VALID_JOB = "job_abc123"
_VALID_SPEAKER = "speaker_a"


def _wav_file(content: bytes = b"RIFF....WAVEfake", content_type: str = "audio/wav"):
    return {"sample": ("sample.wav", content, content_type)}


def _form(**overrides):
    base = {"user_id": _VALID_USER, "job_id": _VALID_JOB, "speaker_id": _VALID_SPEAKER}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# §5.5.1 鉴权
# ---------------------------------------------------------------------------


def test_401_no_internal_key(monkeypatch):
    client = _make_client(monkeypatch, uploader=_FakeUploader())
    resp = client.post(_URL, files=_wav_file(), data=_form())
    assert resp.status_code == 401
    assert resp.json() == {"ok": False, "error": {"code": "unauthorized"}}


def test_401_wrong_internal_key(monkeypatch):
    client = _make_client(monkeypatch, uploader=_FakeUploader())
    resp = client.post(
        _URL, files=_wav_file(), data=_form(),
        headers={"X-Internal-Key": "WRONG"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# §5.5.2 输入白名单
# ---------------------------------------------------------------------------


def test_415_unsupported_content_type(monkeypatch):
    client = _make_client(monkeypatch, uploader=_FakeUploader())
    resp = client.post(
        _URL,
        files={"sample": ("x.txt", b"hello", "text/plain")},
        data=_form(),
        headers={"X-Internal-Key": _TEST_KEY},
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_content_type"


def test_413_oversize_body(monkeypatch):
    client = _make_client(monkeypatch, uploader=_FakeUploader())
    big = b"\x00" * (2 * 1024 * 1024 + 1)  # 2MB + 1 byte
    resp = client.post(
        _URL, files=_wav_file(big), data=_form(),
        headers={"X-Internal-Key": _TEST_KEY},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "sample_too_large"


def test_400_empty_sample(monkeypatch):
    client = _make_client(monkeypatch, uploader=_FakeUploader())
    resp = client.post(
        _URL, files=_wav_file(b""), data=_form(),
        headers={"X-Internal-Key": _TEST_KEY},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_sample"


def test_400_invalid_user_id(monkeypatch):
    client = _make_client(monkeypatch, uploader=_FakeUploader())
    resp = client.post(
        _URL, files=_wav_file(), data=_form(user_id="not-a-uuid"),
        headers={"X-Internal-Key": _TEST_KEY},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_user_id"


def test_400_invalid_job_id(monkeypatch):
    client = _make_client(monkeypatch, uploader=_FakeUploader())
    resp = client.post(
        _URL, files=_wav_file(), data=_form(job_id="Job With Spaces!"),
        headers={"X-Internal-Key": _TEST_KEY},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_job_id"


def test_400_invalid_speaker_id(monkeypatch):
    client = _make_client(monkeypatch, uploader=_FakeUploader())
    resp = client.post(
        _URL, files=_wav_file(), data=_form(speaker_id="SPEAKER_A_INVALID"),
        headers={"X-Internal-Key": _TEST_KEY},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_speaker_id"


# ---------------------------------------------------------------------------
# §5.5.4 uploader 错误
# ---------------------------------------------------------------------------


def test_503_uploader_not_configured(monkeypatch):
    client = _make_client(monkeypatch, uploader_raises_build=True)
    resp = client.post(
        _URL, files=_wav_file(), data=_form(),
        headers={"X-Internal-Key": _TEST_KEY},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "uploader_not_configured"


def test_503_uploader_runtime_error(monkeypatch):
    client = _make_client(monkeypatch, uploader=_FakeUploader(raise_runtime=True))
    resp = client.post(
        _URL, files=_wav_file(), data=_form(),
        headers={"X-Internal-Key": _TEST_KEY},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "uploader_runtime_error"


# ---------------------------------------------------------------------------
# §5.5 happy path
# ---------------------------------------------------------------------------


def test_200_happy_path_returns_presigned_url_sha256_expires(monkeypatch):
    import hashlib
    uploader = _FakeUploader()
    client = _make_client(monkeypatch, uploader=uploader)
    content = b"RIFF1234WAVEfake-audio-bytes"
    resp = client.post(
        _URL, files=_wav_file(content), data=_form(),
        headers={"X-Internal-Key": _TEST_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["presigned_get_url"] == _FAKE_SIGNED_URL
    assert body["sha256"] == hashlib.sha256(content).hexdigest()
    assert "expires_at" in body and body["expires_at"]
    # uploader 被调时用 120s TTL + speaker_id filename hint
    assert uploader.calls[0]["ttl_seconds"] == 120
    assert uploader.calls[0]["filename_hint"] == "speaker_a.wav"


def test_200_uses_120s_ttl_not_default_3600(monkeypatch):
    """spec §5.5：TTL=120s，不是 uploader 默认 3600。"""
    uploader = _FakeUploader()
    client = _make_client(monkeypatch, uploader=uploader)
    client.post(
        _URL, files=_wav_file(), data=_form(),
        headers={"X-Internal-Key": _TEST_KEY},
    )
    assert uploader.calls[0]["ttl_seconds"] == 120


# ---------------------------------------------------------------------------
# §5.5.3 日志脱敏
# ---------------------------------------------------------------------------


def test_logs_do_not_leak_presigned_url_or_oss_secret(monkeypatch, caplog):
    import logging
    uploader = _FakeUploader()
    client = _make_client(monkeypatch, uploader=uploader)
    with caplog.at_level(logging.DEBUG):
        client.post(
            _URL, files=_wav_file(), data=_form(),
            headers={"X-Internal-Key": _TEST_KEY},
        )
    text = caplog.text
    # 不得 leak presigned URL / OSS AK / Signature
    assert _FAKE_SIGNED_URL not in text
    assert "OSSAccessKeyId" not in text
    assert "LTAI_SECRET_AK" not in text
    assert "Signature=FORGEDSIGVALUE123" not in text


def test_logs_do_not_leak_internal_key(monkeypatch, caplog):
    import logging
    client = _make_client(monkeypatch, uploader=_FakeUploader())
    with caplog.at_level(logging.DEBUG):
        client.post(
            _URL, files=_wav_file(), data=_form(),
            headers={"X-Internal-Key": "WRONG-SECRET-KEY-VALUE"},
        )
    assert "WRONG-SECRET-KEY-VALUE" not in caplog.text


# ---------------------------------------------------------------------------
# 不调武汉 worker（spec §5.5.5）
# ---------------------------------------------------------------------------


def test_endpoint_does_not_call_mainland_worker():
    """守卫：express_sample_upload 函数体不调武汉 worker（只上传样本）。"""
    import inspect
    src = inspect.getsource(clone_api.express_sample_upload)
    # 不应出现 worker clone 调用 / worker client 构造
    assert "build_mainland_voice_worker_client" not in src, (
        "express_sample_upload 不应构造 worker client（只上传样本，不调 worker）"
    )
    assert ".clone(" not in src, (
        "express_sample_upload 不应调 worker clone"
    )
    assert "synthesize" not in src.lower()


def test_endpoint_registered_on_internal_router():
    """守卫：endpoint 必须挂在 internal_router (prefix /api/internal/cosyvoice)。"""
    routes = [r.path for r in internal_router.routes]
    assert "/api/internal/cosyvoice/express-sample-upload" in routes, (
        f"express-sample-upload 未注册到 internal_router: {routes}"
    )


def test_internal_router_imported_and_registered_in_main():
    """守卫：gateway/main.py 必须 include internal_router（否则 endpoint 404）。"""
    main_src = (_GATEWAY / "main.py").read_text(encoding="utf-8")
    assert "internal_router as cosyvoice_clone_internal_router" in main_src, (
        "main.py 未 import cosyvoice_clone internal_router"
    )
    assert "app.include_router(cosyvoice_clone_internal_router)" in main_src, (
        "main.py 未 include cosyvoice_clone internal_router"
    )
