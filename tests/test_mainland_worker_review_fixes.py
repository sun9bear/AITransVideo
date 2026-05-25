"""Codex Phase 1 审核 fix 的回归测试。

每个 ``Fix #N`` 一组测试，对应方案审核里 Codex 给的 4 条 findings
+ 1 条 non-blocking（env-backed ASGI 入口）：

- Fix #1：``create_app()`` 未注入 ``audit_logger`` 时挂
  ``JsonlAuditLogger(config.audit_log_path)``。
- Fix #2：clone consent 必须 JSON literal ``true``；``"false"`` /
  ``"0"`` / ``0`` / ``"yes"`` 等都拒。
- Fix #3：``extract_artifact_segments`` 三层 sha256 校验
  （package / manifest / segment）任一不匹配抛
  ``WorkerArtifactIntegrityError``。
- Fix #4：``audit._sanitize_event`` 默认丢弃白名单外字段。
- Non-blocking：``create_app_from_env()`` 能从 env dict 装出 app；
  模块级 ``app`` 是 lazy，不在 import 时触发 env 读取。
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from services.mainland_worker.client import (
    MULTI_SEGMENT_MAX_ATTEMPTS,
    SINGLE_SEGMENT_MAX_ATTEMPTS,
    MainlandWorkerClient,
    WorkerArtifactIntegrityError,
    WorkerCredentials,
    WorkerError,
)
from services.mainland_worker.hmac_auth import (
    HEADER_KEY_ID,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    HmacKey,
    SignatureMaterial,
    sign,
)
from services.mainland_worker.types import (
    WorkerArtifactPackage,
    WorkerCloneConsent,
    WorkerCloneRequest,
    WorkerCloneSample,
    WorkerSegmentRequest,
    WorkerSegmentResult,
    WorkerSynthesizeBatchRequest,
    WorkerSynthesizeBatchResponse,
    compute_text_hash,
)
from services.mainland_worker.worker.app import create_app
from services.mainland_worker.worker.audit import (
    InMemoryAuditLogger,
    JsonlAuditLogger,
    _sanitize_event,
)
from services.mainland_worker.worker.config import (
    WORKER_MODE_MOCK,
    WorkerConfig,
)


KEY_ID = "test-key-1"
SECRET = "test-secret-deadbeef"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _TestClientTransport(httpx.BaseTransport):
    def __init__(self, app) -> None:
        self._tc = TestClient(app, base_url="http://worker.test")

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.query:
            path = path + "?" + request.url.query.decode("ascii")
        result = self._tc.request(
            request.method,
            path,
            headers={k: v for k, v in request.headers.items()},
            content=request.content,
        )
        return httpx.Response(
            status_code=result.status_code,
            headers=dict(result.headers),
            content=result.content,
        )


def _make_config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        mode=WORKER_MODE_MOCK,
        hmac_keys=(HmacKey(key_id=KEY_ID, secret=SECRET),),
        audit_log_path=tmp_path / "audit.jsonl",
        artifact_dir=tmp_path / "artifacts",
    )


# ---------------------------------------------------------------------------
# Fix #1：默认 JsonlAuditLogger
# ---------------------------------------------------------------------------

def test_fix1_default_audit_logger_is_jsonl(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    app = create_app(config=cfg)
    # 不传 audit_logger → 应该是 JsonlAuditLogger
    assert isinstance(app.state.audit_logger, JsonlAuditLogger)
    assert app.state.audit_logger.path == cfg.audit_log_path


def test_fix1_explicit_audit_logger_still_respected(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    memory = InMemoryAuditLogger()
    app = create_app(config=cfg, audit_logger=memory)
    assert app.state.audit_logger is memory


def test_fix1_jsonl_audit_persists_to_disk(tmp_path: Path) -> None:
    """默认 JSONL 在真实磁盘上 append；重启进程后历史仍在。"""
    cfg = _make_config(tmp_path)
    app = create_app(config=cfg)

    transport = _TestClientTransport(app)
    client = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=transport,
    )
    try:
        client.clone(WorkerCloneRequest(
            job_id="job_persist",
            user_id="u",
            speaker_id="s",
            speaker_name="s",
            target_model="cosyvoice-v3.5-flash",
            sample=WorkerCloneSample(kind="download_url", url="https://x/y.wav", sha256="a" * 64),
            source_segments=(1,),
            consent=WorkerCloneConsent(voice_clone_confirmed=True, confirmed_at=_now_iso()),
        ))
    finally:
        client.close()

    # 文件确实落盘
    assert cfg.audit_log_path.exists()
    content = cfg.audit_log_path.read_text(encoding="utf-8").strip()
    assert content, "audit log should not be empty"
    line = json.loads(content.splitlines()[-1])
    assert line["operation"] == "clone"
    assert line["job_id"] == "job_persist"
    assert line["status"] == "ok"


# ---------------------------------------------------------------------------
# Fix #2：clone consent 严格布尔
# ---------------------------------------------------------------------------

def _signed_post_clone(app, body_obj: dict) -> httpx.Response:
    """Helper：手工发签名后的 POST /cosyvoice/clone（绕过 client 的强类型）。"""
    body = json.dumps(body_obj).encode("utf-8")
    ts = int(__import__("time").time())
    nonce = "fix2-" + str(ts)
    material = SignatureMaterial(
        method="POST", path="/cosyvoice/clone",
        timestamp=ts, nonce=nonce, key_id=KEY_ID, body=body,
    )
    headers = {
        HEADER_KEY_ID: KEY_ID,
        HEADER_TIMESTAMP: str(ts),
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: sign(material, SECRET),
        "Content-Type": "application/json",
    }
    tc = TestClient(app, base_url="http://worker.test")
    return tc.request("POST", "/cosyvoice/clone", headers=headers, content=body)


def _clone_body_with_consent(consent_value) -> dict:
    return {
        "job_id": "j",
        "user_id": "u",
        "speaker_id": "s",
        "speaker_name": "s",
        "target_model": "cosyvoice-v3.5-flash",
        "sample": {"kind": "download_url", "url": "https://x/y.wav", "sha256": "a" * 64},
        "source_segments": [1],
        "consent": {
            "voice_clone_confirmed": consent_value,
            "confirmed_at": _now_iso(),
        },
    }


@pytest.mark.parametrize("bad_value", [
    "true",      # JSON 字符串 "true"，不是 boolean literal
    "false",     # 经典坑：bool("false") == True
    "0",
    "1",
    "yes",
    "no",
    1,           # int 1 在旧版 bool() 下会被认作 True
    0,
    None,
    [],
    {},
])
def test_fix2_clone_rejects_non_boolean_consent(tmp_path: Path, bad_value) -> None:
    cfg = _make_config(tmp_path)
    app = create_app(config=cfg, audit_logger=InMemoryAuditLogger())
    resp = _signed_post_clone(app, _clone_body_with_consent(bad_value))
    assert resp.status_code == 400, (
        f"consent={bad_value!r} 应当被拒，实际 {resp.status_code} {resp.text}"
    )
    err = resp.json().get("detail") or {}
    assert err.get("code") == "consent_required", err


def test_fix2_clone_accepts_only_json_true(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    app = create_app(config=cfg, audit_logger=InMemoryAuditLogger())
    resp = _signed_post_clone(app, _clone_body_with_consent(True))
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Fix #3：client artifact sha 校验
# ---------------------------------------------------------------------------

def _make_response_with_artifact(
    audio_path: str,
    wav_bytes: bytes,
    *,
    package_sha_override: str | None = None,
    segment_sha_override: str | None = None,
    zip_payload_override: bytes | None = None,
) -> WorkerSynthesizeBatchResponse:
    """构造一份合法（或刻意被篡改的）响应给 extract_artifact_segments 测试。"""
    import io
    import zipfile

    if zip_payload_override is not None:
        zip_bytes = zip_payload_override
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(audio_path, wav_bytes)
        zip_bytes = buf.getvalue()

    pkg_sha = package_sha_override or hashlib.sha256(zip_bytes).hexdigest()
    seg_sha = segment_sha_override or hashlib.sha256(wav_bytes).hexdigest()
    return WorkerSynthesizeBatchResponse(
        ok=True,
        job_id="j",
        target_model="cosyvoice-v3.5-flash",
        segments=(WorkerSegmentResult(
            segment_id=1,
            speaker_id="a",
            voice_id="v",
            audio_path=audio_path,
            duration_ms=1000,
            billed_chars=10,
            sha256=seg_sha,
        ),),
        package=WorkerArtifactPackage(
            kind="inline_base64",
            download_url="",
            sha256=pkg_sha,
            expires_at="2026-01-01T00:00:00Z",
            inline_bytes=zip_bytes,
        ),
        # Phase 4.0b §A: __post_init__ 要求非空（Codex P1 fail-closed）
        worker_request_id="wrk_fix3_fake",
    )


def test_fix3_extract_valid_response_succeeds() -> None:
    wav = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 100
    resp = _make_response_with_artifact("segments/segment_001_a.wav", wav)
    result = MainlandWorkerClient.extract_artifact_segments(resp)
    assert len(result) == 1
    assert result["segments/segment_001_a.wav"] == wav


def test_fix3_rejects_package_sha_mismatch() -> None:
    wav = b"RIFF" + b"\x00" * 100
    resp = _make_response_with_artifact(
        "segments/s1.wav", wav,
        package_sha_override="b" * 64,  # 故意错的 package sha
    )
    with pytest.raises(WorkerArtifactIntegrityError, match="package sha256 mismatch"):
        MainlandWorkerClient.extract_artifact_segments(resp)


def test_fix3_rejects_segment_sha_mismatch() -> None:
    wav = b"RIFF" + b"\x00" * 100
    resp = _make_response_with_artifact(
        "segments/s1.wav", wav,
        segment_sha_override="c" * 64,  # 故意错的 segment sha
    )
    with pytest.raises(WorkerArtifactIntegrityError, match="segment 1 .* sha256 mismatch"):
        MainlandWorkerClient.extract_artifact_segments(resp)


def test_fix3_rejects_missing_segment_in_zip() -> None:
    """Manifest 说有 segments/s1.wav，但 zip 里其实是 segments/wrong.wav。"""
    import io
    import zipfile

    wav = b"RIFF" + b"\x00" * 100
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("segments/wrong.wav", wav)  # 与 manifest 不一致
    zip_bytes = buf.getvalue()

    resp = _make_response_with_artifact(
        "segments/s1.wav", wav,
        zip_payload_override=zip_bytes,
    )
    with pytest.raises(WorkerArtifactIntegrityError, match="segments missing from zip"):
        MainlandWorkerClient.extract_artifact_segments(resp)


def test_fix3_empty_payload_with_nonempty_manifest_rejected() -> None:
    wav = b"RIFF" + b"\x00" * 100
    resp = _make_response_with_artifact("segments/s1.wav", wav)
    # 强制把 inline_bytes 改成空
    object.__setattr__(resp.package, "inline_bytes", b"")
    with pytest.raises(WorkerArtifactIntegrityError, match="0 bytes but manifest"):
        MainlandWorkerClient.extract_artifact_segments(resp)


def test_fix3_only_manifest_listed_segments_returned() -> None:
    """zip 内有多余文件时，extract 只返 manifest 列出的那些 — 防 worker
    端意外打包敏感临时文件被 client 当成 wav 用。"""
    import io
    import zipfile

    wav = b"RIFF" + b"\x00" * 100
    sneaky = b"sensitive log content"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("segments/s1.wav", wav)
        zf.writestr("extra/sneaky.log", sneaky)  # zip 里多塞一个
    zip_bytes = buf.getvalue()

    resp = _make_response_with_artifact(
        "segments/s1.wav", wav,
        zip_payload_override=zip_bytes,
    )
    result = MainlandWorkerClient.extract_artifact_segments(resp)
    assert set(result.keys()) == {"segments/s1.wav"}


# ---------------------------------------------------------------------------
# Fix #4：audit sanitizer 默认丢弃未知字段
# ---------------------------------------------------------------------------

def test_fix4_sanitize_drops_unknown_fields() -> None:
    event = {
        "event_id": "e1",
        "operation": "clone",
        "status": "ok",
        # 未知字段（可能是潜在敏感）
        "sample_url": "https://example.com/sample.wav?signed=...",
        "dashscope_response": {"voice_id": "abc"},
        "authorization": "Bearer leaked",
    }
    cleaned = _sanitize_event(event)
    # 白名单字段保留
    assert cleaned["event_id"] == "e1"
    assert cleaned["operation"] == "clone"
    assert cleaned["status"] == "ok"
    # 未知字段全部 drop
    assert "sample_url" not in cleaned
    assert "dashscope_response" not in cleaned
    assert "authorization" not in cleaned


def test_fix4_sanitize_drops_forbidden_fields() -> None:
    event = {
        "event_id": "e1",
        "operation": "clone",
        "raw_audio": b"\x00\x00",
        "api_key": "sk-xxx",
        "hmac_secret": "secret",
    }
    cleaned = _sanitize_event(event)
    assert cleaned == {"event_id": "e1", "operation": "clone"}


def test_fix4_unknown_field_does_not_leak_through_jsonl(tmp_path: Path) -> None:
    """端到端：JSONL 落盘也不会包含未知字段。"""
    path = tmp_path / "audit.jsonl"
    logger_obj = JsonlAuditLogger(path)
    logger_obj.emit({
        "event_id": "e1",
        "operation": "clone",
        "status": "ok",
        "leaky_field": "should not appear",
    })
    content = path.read_text(encoding="utf-8")
    assert "leaky_field" not in content
    assert "should not appear" not in content
    line = json.loads(content.strip())
    assert line == {"event_id": "e1", "operation": "clone", "status": "ok"}


# ---------------------------------------------------------------------------
# Non-blocking：env-backed ASGI 入口
# ---------------------------------------------------------------------------

def test_env_entry_create_app_from_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKER_MODE", "mock")
    monkeypatch.setenv("WORKER_HMAC_KEYS", "k1:s1")
    monkeypatch.setenv("WORKER_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("WORKER_ARTIFACT_DIR", str(tmp_path / "art"))

    # 重新 import app 子模块以拿到带 create_app_from_env 的版本
    from services.mainland_worker.worker import app as app_module
    importlib.reload(app_module)

    fa = app_module.create_app_from_env()
    assert fa.state.config.mode == "mock"
    assert fa.state.config.audit_log_path == tmp_path / "audit.jsonl"


# ---------------------------------------------------------------------------
# Fix #5：synthesize_batch retry 单段 / 多段分语义
# ---------------------------------------------------------------------------

def _make_segment_for_batch(seg_id: int, text: str) -> WorkerSegmentRequest:
    return WorkerSegmentRequest(
        segment_id=seg_id,
        speaker_id="a",
        voice_id="v",
        text=text,
        speech_rate=1.0,
        text_hash=compute_text_hash(text),
    )


def test_fix5_constants_match_plan() -> None:
    """plan §Retry 锁定的常量值：单段 3、多段 2。"""
    assert SINGLE_SEGMENT_MAX_ATTEMPTS == 3, (
        "plan §Retry: 单段 TTS 最多 3 次"
    )
    assert MULTI_SEGMENT_MAX_ATTEMPTS == 2, (
        "plan §Retry: batch 整体最多重提 1 次（== 2 次 attempts）"
    )


def test_fix5_multi_segment_5xx_retries_only_2(monkeypatch) -> None:
    """**关键回归**：多段 batch 5xx 总共只尝试 2 次，**不是** 3 次。

    旧实现 ``max_attempts=self._max_network_retries=3`` 把多段也跑 3 次，
    等于"重提 2 次"，超过 plan §Retry "batch 整体最多重提 1 次"。
    """

    class FlakyTransport(httpx.BaseTransport):
        attempts = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            FlakyTransport.attempts += 1
            return httpx.Response(503, json={"ok": False, "error": {"code": "down"}})

    monkeypatch.setattr("services.mainland_worker.client.time.sleep", lambda *_: None)

    c = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=FlakyTransport(),
        max_network_retries=3,  # client 上限 3
    )
    try:
        # 三段 batch → 应受多段上限 = 2 attempts 限制
        req = WorkerSynthesizeBatchRequest(
            job_id="j",
            target_model="cosyvoice-v3.5-flash",
            audio_format="wav",
            segments=(
                _make_segment_for_batch(1, "段一"),
                _make_segment_for_batch(2, "段二"),
                _make_segment_for_batch(3, "段三"),
            ),
        )
        with pytest.raises(WorkerError):
            c.synthesize_batch(req)
        assert FlakyTransport.attempts == 2, (
            f"多段 batch 5xx 应当只尝试 2 次（plan §Retry），实际 {FlakyTransport.attempts}"
        )
    finally:
        c.close()


def test_fix5_single_segment_5xx_retries_up_to_3(monkeypatch) -> None:
    """对应面：单段 batch 仍享受 3 attempts（plan §Retry "单段 TTS 最多 3 次"）。"""

    class FlakyTransport(httpx.BaseTransport):
        attempts = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            FlakyTransport.attempts += 1
            return httpx.Response(503, json={"ok": False, "error": {"code": "down"}})

    monkeypatch.setattr("services.mainland_worker.client.time.sleep", lambda *_: None)

    c = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=FlakyTransport(),
        max_network_retries=3,
    )
    try:
        req = WorkerSynthesizeBatchRequest(
            job_id="j",
            target_model="cosyvoice-v3.5-flash",
            audio_format="wav",
            segments=(_make_segment_for_batch(1, "单段重合成"),),
        )
        with pytest.raises(WorkerError):
            c.synthesize_batch(req)
        assert FlakyTransport.attempts == 3
    finally:
        c.close()


def test_fix5_caller_can_tighten_via_max_network_retries(monkeypatch) -> None:
    """调用方传更低的 max_network_retries 应该收紧（不能被多段路径放大）。

    场景：灰度期限单次尝试。
    """

    class FlakyTransport(httpx.BaseTransport):
        attempts = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            FlakyTransport.attempts += 1
            return httpx.Response(503, json={"ok": False, "error": {"code": "down"}})

    monkeypatch.setattr("services.mainland_worker.client.time.sleep", lambda *_: None)

    c = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=FlakyTransport(),
        max_network_retries=1,  # 收紧到 1
    )
    try:
        # 单段 batch
        req = WorkerSynthesizeBatchRequest(
            job_id="j",
            target_model="cosyvoice-v3.5-flash",
            audio_format="wav",
            segments=(_make_segment_for_batch(1, "text"),),
        )
        with pytest.raises(WorkerError):
            c.synthesize_batch(req)
        # min(1, 3) == 1
        assert FlakyTransport.attempts == 1
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Non-blocking：env-backed ASGI 入口（续）
# ---------------------------------------------------------------------------

def test_env_entry_module_app_is_lazy(monkeypatch) -> None:
    """模块级 ``app`` 不应在 import 时触发 env 读取。

    背景：如果 ``app = create_app_from_env()`` 在 module top-level 跑，
    pytest 一旦 import 这个模块就会要 env 变量；干净本地环境会立刻爆。
    """
    # 在没有任何 WORKER_HMAC_KEYS 的环境下 import 模块 — 不应抛
    monkeypatch.delenv("WORKER_HMAC_KEYS", raising=False)

    from services.mainland_worker.worker import app as app_module
    importlib.reload(app_module)
    # ``app`` 句柄存在
    assert hasattr(app_module, "app")
    # 而且不是已经构造好的 FastAPI（lazy wrapper）
    from fastapi import FastAPI
    assert not isinstance(app_module.app, FastAPI), (
        "模块级 app 必须是 lazy wrapper，不能在 import 时就构造 FastAPI"
    )
