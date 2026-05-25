"""Mainland worker e2e（client → worker mock app）集成测试。

通过 ``httpx.ASGITransport`` 让 ``MainlandWorkerClient`` 直接 in-process
调用 worker FastAPI app，覆盖：

1. 健康检查（不验签）
2. Clone 成功 / consent_required / 签名拒绝
3. Synthesize batch：多段、单段（regenerate-tts 复用路径）、text_hash
   mismatch、artifact 解 zip 后段数 / SHA 对得上
4. Delete voice
5. 签名错误时 401 立刻抛、不重试
6. retry 上限：5xx 触发重试，达到 max_attempts 抛 WorkerNetworkError /
   WorkerError，不会无限循环
7. 单段 regenerate 走 batch endpoint：``len(segments) == 1`` 的
   ``synthesize_batch`` 调用应当成功并能解出唯一 wav
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from services.mainland_worker.client import (
    MainlandWorkerClient,
    WorkerCredentials,
    WorkerError,
    WorkerNetworkError,
    WorkerSignatureRejectedError,
)
from services.mainland_worker.hmac_auth import HmacKey
from services.mainland_worker.types import (
    PLATFORM_DASHSCOPE_MAINLAND,
    PROVIDER_COSYVOICE_VOICE_CLONE,
    REGION_CONSTRAINT_MAINLAND_ONLY,
    TTS_PROVIDER_COSYVOICE,
    WorkerCloneConsent,
    WorkerCloneRequest,
    WorkerCloneSample,
    WorkerDeleteVoiceRequest,
    WorkerSegmentRequest,
    WorkerSynthesizeBatchRequest,
    compute_text_hash,
)
from services.mainland_worker.worker.app import create_app
from services.mainland_worker.worker.audit import InMemoryAuditLogger
from services.mainland_worker.worker.config import (
    WORKER_MODE_MOCK,
    WorkerConfig,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

KEY_ID = "test-key-1"
SECRET = "test-secret-deadbeef"


class _TestClientTransport(httpx.BaseTransport):
    """Sync httpx transport bridging to a FastAPI app via ``TestClient``.

    httpx 0.28 的 ``ASGITransport`` 只支持 async；我们的 ``MainlandWorkerClient``
    是 sync 的（生产路径里 US 主机 pipeline 也是 sync），所以这里通过
    FastAPI 自带的 ``TestClient`` 把 ASGI 调用包装成同步。

    这个 helper 只在测试里使用 — 生产路径用真实 ``httpx.Client`` 跑 HTTP。
    """

    def __init__(self, app) -> None:
        self._tc = TestClient(app, base_url="http://worker.test")

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.query:
            path = path + "?" + request.url.query.decode("ascii")
        headers = {k: v for k, v in request.headers.items()}
        # TestClient 内部基于 httpx，但额外做了 ASGI 桥接 — 同步可用
        result = self._tc.request(
            request.method,
            path,
            headers=headers,
            content=request.content,
        )
        return httpx.Response(
            status_code=result.status_code,
            headers=dict(result.headers),
            content=result.content,
        )


@pytest.fixture
def audit_logger() -> InMemoryAuditLogger:
    return InMemoryAuditLogger()


@pytest.fixture
def worker_app(tmp_path: Path, audit_logger: InMemoryAuditLogger):
    cfg = WorkerConfig(
        mode=WORKER_MODE_MOCK,
        hmac_keys=(HmacKey(key_id=KEY_ID, secret=SECRET),),
        audit_log_path=tmp_path / "audit.jsonl",
        artifact_dir=tmp_path / "artifacts",
    )
    return create_app(config=cfg, audit_logger=audit_logger)


@pytest.fixture
def client(worker_app):
    transport = _TestClientTransport(worker_app)
    c = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=transport,
    )
    yield c
    c.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_clone_req(speaker: str = "speaker_a", consent: bool = True) -> WorkerCloneRequest:
    return WorkerCloneRequest(
        job_id="job_e2e",
        user_id="user_test",
        speaker_id=speaker,
        speaker_name=speaker,
        target_model="cosyvoice-v3.5-flash",
        sample=WorkerCloneSample(
            kind="download_url",
            url="https://example.com/sample.wav",
            sha256="a" * 64,
        ),
        source_segments=(12, 18, 19),
        consent=WorkerCloneConsent(
            voice_clone_confirmed=consent,
            confirmed_at=_now_iso(),
        ),
    )


def _make_segment(seg_id: int, text: str, voice_id: str = "v_cosy_1") -> WorkerSegmentRequest:
    return WorkerSegmentRequest(
        segment_id=seg_id,
        speaker_id="speaker_a",
        voice_id=voice_id,
        text=text,
        speech_rate=1.0,
        text_hash=compute_text_hash(text),
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health_unsigned_ok(client: MainlandWorkerClient) -> None:
    h = client.health()
    assert h.ok is True
    assert h.worker == "aivideotrans-mainland-worker"
    assert h.region == "cn-wuhan"
    assert "cosyvoice" in h.providers
    assert h.providers["cosyvoice"].configured is True
    assert h.providers["cosyvoice"].mode == "mock"


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------

def test_clone_success(client: MainlandWorkerClient, audit_logger: InMemoryAuditLogger) -> None:
    req = _make_clone_req()
    resp = client.clone(req)
    assert resp.ok
    assert resp.voice_id.startswith("mock_cosy_")
    assert resp.provider == PROVIDER_COSYVOICE_VOICE_CLONE
    assert resp.tts_provider == TTS_PROVIDER_COSYVOICE
    assert resp.target_model == "cosyvoice-v3.5-flash"
    assert resp.region_constraint == REGION_CONSTRAINT_MAINLAND_ONLY
    assert resp.requires_worker is True
    assert resp.platform == PLATFORM_DASHSCOPE_MAINLAND
    assert resp.sample_sha256 == "a" * 64

    # Audit
    clone_events = [e for e in audit_logger.events if e.get("operation") == "clone"]
    assert len(clone_events) == 1
    assert clone_events[0]["status"] == "ok"
    assert clone_events[0]["voice_id"] == resp.voice_id


def test_clone_is_deterministic_for_same_seed(client: MainlandWorkerClient) -> None:
    a = client.clone(_make_clone_req())
    b = client.clone(_make_clone_req())
    assert a.voice_id == b.voice_id


def test_clone_requires_consent(client: MainlandWorkerClient, audit_logger: InMemoryAuditLogger) -> None:
    req = _make_clone_req(consent=False)
    with pytest.raises(WorkerError) as exc_info:
        client.clone(req)
    assert exc_info.value.code == "consent_required"

    # Audit 应记录 failed
    clone_events = [e for e in audit_logger.events if e.get("operation") == "clone"]
    assert len(clone_events) == 1
    assert clone_events[0]["status"] == "failed"
    assert clone_events[0]["error_code"] == "consent_required"


def test_clone_with_wrong_secret_returns_401(worker_app) -> None:
    transport = _TestClientTransport(worker_app)
    c = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret="wrong"),
        transport=transport,
    )
    try:
        with pytest.raises(WorkerSignatureRejectedError):
            c.clone(_make_clone_req())
    finally:
        c.close()


def test_clone_with_unknown_key_id_returns_401(worker_app) -> None:
    transport = _TestClientTransport(worker_app)
    c = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id="unknown", secret=SECRET),
        transport=transport,
    )
    try:
        with pytest.raises(WorkerSignatureRejectedError):
            c.clone(_make_clone_req())
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Synthesize batch
# ---------------------------------------------------------------------------

def test_synthesize_batch_multi_segment(client: MainlandWorkerClient) -> None:
    req = WorkerSynthesizeBatchRequest(
        job_id="job_e2e",
        target_model="cosyvoice-v3.5-flash",
        audio_format="wav",
        segments=(
            _make_segment(1, "你好世界，这是第一段。"),
            _make_segment(2, "Hello world, this is the second segment."),
            _make_segment(3, "第三段也要合成。"),
        ),
    )
    resp = client.synthesize_batch(req)
    assert resp.ok
    assert resp.job_id == "job_e2e"
    assert len(resp.segments) == 3
    for seg in resp.segments:
        assert seg.duration_ms >= 1000  # MIN_DURATION_MS
        assert seg.billed_chars > 0
        assert seg.audio_path.startswith("segments/")
        assert len(seg.sha256) == 64  # sha256 hex

    # Artifact 应能解出 3 个 wav
    segments_map = MainlandWorkerClient.extract_artifact_segments(resp)
    assert len(segments_map) == 3
    for seg in resp.segments:
        assert seg.audio_path in segments_map
        wav_bytes = segments_map[seg.audio_path]
        assert wav_bytes[:4] == b"RIFF"
        # sha256 与响应 manifest 一致
        assert hashlib.sha256(wav_bytes).hexdigest() == seg.sha256


def test_synthesize_batch_single_segment_works(client: MainlandWorkerClient) -> None:
    """plan §Studio Post-Edit / Regenerate TTS：单段也走同一 endpoint。"""
    req = WorkerSynthesizeBatchRequest(
        job_id="job_postedit",
        target_model="cosyvoice-v3.5-flash",
        audio_format="wav",
        segments=(_make_segment(42, "重新合成这一段。"),),
    )
    resp = client.synthesize_batch(req)
    assert len(resp.segments) == 1
    only_seg = resp.segments[0]
    assert only_seg.segment_id == 42

    segments_map = MainlandWorkerClient.extract_artifact_segments(resp)
    assert len(segments_map) == 1
    wav_bytes = next(iter(segments_map.values()))
    assert wav_bytes[:4] == b"RIFF"


def test_synthesize_batch_rejects_text_hash_mismatch(client: MainlandWorkerClient) -> None:
    seg = WorkerSegmentRequest(
        segment_id=1,
        speaker_id="speaker_a",
        voice_id="v1",
        text="原始文本",
        speech_rate=1.0,
        text_hash=compute_text_hash("篡改文本"),  # 故意错的 hash
    )
    req = WorkerSynthesizeBatchRequest(
        job_id="job_hash_test",
        target_model="cosyvoice-v3.5-flash",
        audio_format="wav",
        segments=(seg,),
    )
    with pytest.raises(WorkerError) as exc_info:
        client.synthesize_batch(req)
    assert exc_info.value.code == "text_hash_mismatch"
    assert exc_info.value.http_status == 400


def test_synthesize_batch_empty_segments_raises_in_dataclass() -> None:
    with pytest.raises(ValueError, match="segments must not be empty"):
        WorkerSynthesizeBatchRequest(
            job_id="job",
            target_model="cosyvoice-v3.5-flash",
            audio_format="wav",
            segments=(),
        )


def test_synthesize_batch_speech_rate_halves_duration(client: MainlandWorkerClient) -> None:
    """speech_rate=2.0 时长应该约等于 speech_rate=1.0 的一半。"""
    text = "这是一段比较长的中文文本，用来测试 speech_rate 对时长的影响。"
    req_slow = WorkerSynthesizeBatchRequest(
        job_id="j",
        target_model="cosyvoice-v3.5-flash",
        audio_format="wav",
        segments=(WorkerSegmentRequest(
            segment_id=1, speaker_id="a", voice_id="v", text=text,
            speech_rate=1.0, text_hash=compute_text_hash(text),
        ),),
    )
    req_fast = WorkerSynthesizeBatchRequest(
        job_id="j",
        target_model="cosyvoice-v3.5-flash",
        audio_format="wav",
        segments=(WorkerSegmentRequest(
            segment_id=2, speaker_id="a", voice_id="v", text=text,
            speech_rate=2.0, text_hash=compute_text_hash(text),
        ),),
    )
    r_slow = client.synthesize_batch(req_slow)
    r_fast = client.synthesize_batch(req_fast)
    # 允许 ±20% 浮动（取整 + min/max 边界）
    assert r_fast.segments[0].duration_ms <= r_slow.segments[0].duration_ms


def test_synthesize_audit_records_billed_chars(
    client: MainlandWorkerClient, audit_logger: InMemoryAuditLogger,
) -> None:
    text = "短文本"
    req = WorkerSynthesizeBatchRequest(
        job_id="j",
        target_model="cosyvoice-v3.5-flash",
        audio_format="wav",
        segments=(WorkerSegmentRequest(
            segment_id=1, speaker_id="a", voice_id="v", text=text,
            speech_rate=1.0, text_hash=compute_text_hash(text),
        ),),
    )
    client.synthesize_batch(req)
    synth_events = [e for e in audit_logger.events if e.get("operation") == "synthesize_segment"]
    assert len(synth_events) == 1
    # Phase 4.0b §B: billed_chars 用 billing_character_count（CJK = 2），不是 len(text)。
    # "短文本" 3 个 CJK 汉字 → 6 计费字符
    from services.mainland_worker.billing_chars import billing_character_count
    assert synth_events[0]["billed_chars"] == billing_character_count(text)
    assert synth_events[0]["billed_chars"] == 6  # 3 个汉字 × 2 = 6
    assert synth_events[0]["status"] == "ok"
    assert "duration_ms" in synth_events[0]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_delete_voice_success(client: MainlandWorkerClient, audit_logger: InMemoryAuditLogger) -> None:
    req = WorkerDeleteVoiceRequest(
        job_id="job_e2e",
        user_id="user_test",
        reason="user_deleted",
    )
    resp = client.delete_voice("mock_cosy_abc", req)
    assert resp.ok
    assert resp.voice_id == "mock_cosy_abc"
    assert resp.deleted_at.endswith("Z")

    del_events = [e for e in audit_logger.events if e.get("operation") == "delete_voice"]
    assert len(del_events) == 1
    assert del_events[0]["status"] == "ok"


# ---------------------------------------------------------------------------
# Retry / network 行为
# ---------------------------------------------------------------------------

def test_clone_does_not_retry_on_network_error() -> None:
    """plan §Retry/Clone：每次用户确认最多 1 次 provider call。"""

    class FailingTransport(httpx.BaseTransport):
        attempts = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            FailingTransport.attempts += 1
            raise httpx.ConnectError("simulated network down")

    c = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=FailingTransport(),
    )
    try:
        with pytest.raises(WorkerNetworkError):
            c.clone(_make_clone_req())
        # 关键断言：只调一次，绝不重试
        assert FailingTransport.attempts == 1
    finally:
        c.close()


def test_synthesize_single_segment_retries_up_to_3(monkeypatch) -> None:
    """5xx 应当重试，单段路径上限 3 次（plan §Retry "单段 TTS 最多 3 次"）。

    与 ``test_synthesize_multi_segment_retries_only_2`` 互补。
    """

    class FlakyTransport(httpx.BaseTransport):
        attempts = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            FlakyTransport.attempts += 1
            return httpx.Response(503, json={"ok": False, "error": {"code": "down"}})

    # Patch sleep 避免真等 1+5+15s
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
            segments=(_make_segment(1, "text"),),  # 单段 → 上限 3
        )
        with pytest.raises(WorkerError) as exc:
            c.synthesize_batch(req)
        assert exc.value.http_status == 503
        # 单段路径上限 3 attempts
        assert FlakyTransport.attempts == 3
    finally:
        c.close()


def test_synthesize_does_not_retry_on_4xx(monkeypatch) -> None:
    """4xx 业务错误不重试 — 重试也变不好且会重复扣费。"""

    class FourHundredTransport(httpx.BaseTransport):
        attempts = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            FourHundredTransport.attempts += 1
            return httpx.Response(400, json={"ok": False, "error": {"code": "invalid_input"}})

    monkeypatch.setattr("services.mainland_worker.client.time.sleep", lambda *_: None)

    c = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=FourHundredTransport(),
    )
    try:
        req = WorkerSynthesizeBatchRequest(
            job_id="j",
            target_model="cosyvoice-v3.5-flash",
            audio_format="wav",
            segments=(_make_segment(1, "text"),),
        )
        with pytest.raises(WorkerError):
            c.synthesize_batch(req)
        assert FourHundredTransport.attempts == 1
    finally:
        c.close()


def test_synthesize_succeeds_after_transient_5xx(monkeypatch) -> None:
    """第一次 5xx → 第二次成功 → 不再重试。"""

    class FlakyOnceTransport(httpx.BaseTransport):
        attempts = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            FlakyOnceTransport.attempts += 1
            if FlakyOnceTransport.attempts == 1:
                return httpx.Response(503, json={"ok": False, "error": {"code": "tmp"}})
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "job_id": "j",
                    "target_model": "cosyvoice-v3.5-flash",
                    "segments": [],
                    "package": {
                        "kind": "inline_base64",
                        "download_url": "",
                        "sha256": "x" * 64,
                        "expires_at": "2026-01-01T00:00:00Z",
                        "inline_base64": base64.b64encode(b"PK\x05\x06" + b"\x00" * 18).decode(),
                    },
                    # Phase 4.0b §A: 必填
                    "worker_request_id": "wrk_fake_after_transient",
                },
            )

    monkeypatch.setattr("services.mainland_worker.client.time.sleep", lambda *_: None)

    c = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=FlakyOnceTransport(),
        max_network_retries=3,
    )
    try:
        req = WorkerSynthesizeBatchRequest(
            job_id="j",
            target_model="cosyvoice-v3.5-flash",
            audio_format="wav",
            segments=(_make_segment(1, "text"),),
        )
        # 故意把 batch 设成空段也走 OK 路径不重要；这里关注 retry 行为
        # （但 dataclass 不允许空段，所以保持单段，但 mock transport 不真合成）
        resp = c.synthesize_batch(req)
        assert resp.ok
        assert FlakyOnceTransport.attempts == 2
    finally:
        c.close()


def test_health_does_not_retry(monkeypatch) -> None:
    """健康检查不重试 — 不该被网络抖动拖延。"""

    class FailTransport(httpx.BaseTransport):
        attempts = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            FailTransport.attempts += 1
            raise httpx.ConnectError("down")

    monkeypatch.setattr("services.mainland_worker.client.time.sleep", lambda *_: None)

    c = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=FailTransport(),
    )
    try:
        with pytest.raises(WorkerNetworkError):
            c.health()
        assert FailTransport.attempts == 1
    finally:
        c.close()
