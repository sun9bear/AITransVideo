"""Phase 4.0b 协议扩展守卫 + 端到端测试。

按 plan §Phase 4.0b 通过标准 + Codex 二轮 review 建议补的守卫：

1. **三个 response 顶层 ``worker_request_id`` 必填**：clone / synthesize-batch /
   delete 三个 endpoint 返回的 response 都要含非空 ``worker_request_id``，
   形成 audit trail 主锚点。
2. **``WorkerSegmentResult.provider_request_id`` 透传**：mock 路径下 None，
   real 路径下从 SDK 取（mock 测试覆盖前者 + AST 守卫覆盖后者代码路径存在）。
3. **AST 守卫**：``RealCosyvoiceProvider.synthesize_segment`` 方法体内
   不允许出现 ``len(seg.text)``（plan §Phase 4.0b §B 硬规则；现在用
   ``billing_character_count``）。
4. **类型守卫**：``WorkerCloneResponse / WorkerSynthesizeBatchResponse /
   WorkerDeleteVoiceResponse`` 必须有 ``worker_request_id: str`` 字段；
   ``WorkerSegmentResult`` 必须有 ``provider_request_id``。
"""
from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from services.mainland_worker.client import (
    MainlandWorkerClient,
    WorkerCredentials,
)
from services.mainland_worker.hmac_auth import HmacKey
from services.mainland_worker.types import (
    WorkerCloneConsent,
    WorkerCloneRequest,
    WorkerCloneResponse,
    WorkerCloneSample,
    WorkerDeleteVoiceRequest,
    WorkerDeleteVoiceResponse,
    WorkerSegmentRequest,
    WorkerSegmentResult,
    WorkerSynthesizeBatchRequest,
    WorkerSynthesizeBatchResponse,
    compute_text_hash,
)
from services.mainland_worker.worker.app import create_app
from services.mainland_worker.worker.audit import InMemoryAuditLogger
from services.mainland_worker.worker.config import WORKER_MODE_MOCK, WorkerConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_COSYVOICE_PATH = (
    REPO_ROOT
    / "src"
    / "services"
    / "mainland_worker"
    / "worker"
    / "providers"
    / "real_cosyvoice.py"
)


# ---------------------------------------------------------------------------
# 1. Type schema 守卫：三个 response 必须有 worker_request_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response_cls", [
    WorkerCloneResponse,
    WorkerSynthesizeBatchResponse,
    WorkerDeleteVoiceResponse,
])
def test_response_has_worker_request_id_field(response_cls) -> None:
    """Phase 4.0b §A：三个 response 顶层都加 ``worker_request_id`` 必填字段。"""
    annotations = response_cls.__annotations__
    assert "worker_request_id" in annotations, (
        f"{response_cls.__name__} 必须有 worker_request_id 字段（Phase 4.0b §A）"
    )


@pytest.mark.parametrize("response_cls", [
    WorkerCloneResponse,
    WorkerDeleteVoiceResponse,
])
def test_response_has_provider_request_id_field(response_cls) -> None:
    """``provider_request_id`` 在 clone / delete response 顶层（nullable）。"""
    annotations = response_cls.__annotations__
    assert "provider_request_id" in annotations


def test_segment_result_has_provider_request_id_field() -> None:
    """``WorkerSegmentResult.provider_request_id`` segment 级（每段独立）。"""
    annotations = WorkerSegmentResult.__annotations__
    assert "provider_request_id" in annotations


# ---------------------------------------------------------------------------
# 2. 端到端：mock 路径下 worker_request_id 必填且非空
# ---------------------------------------------------------------------------

KEY_ID = "test-key"
SECRET = "test-secret-deadbeef-1234"


class _TestClientTransport(httpx.BaseTransport):
    def __init__(self, app) -> None:
        self._tc = TestClient(app, base_url="http://worker.test")

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.query:
            path = path + "?" + request.url.query.decode("ascii")
        r = self._tc.request(
            request.method,
            path,
            headers={k: v for k, v in request.headers.items()},
            content=request.content,
        )
        return httpx.Response(r.status_code, headers=dict(r.headers), content=r.content)


@pytest.fixture
def client(tmp_path: Path):
    cfg = WorkerConfig(
        mode=WORKER_MODE_MOCK,
        hmac_keys=(HmacKey(key_id=KEY_ID, secret=SECRET),),
        audit_log_path=tmp_path / "audit.jsonl",
        artifact_dir=tmp_path / "art",
    )
    app = create_app(config=cfg, audit_logger=InMemoryAuditLogger())
    transport = _TestClientTransport(app)
    c = MainlandWorkerClient(
        base_url="http://worker.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=transport,
    )
    yield c
    c.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_clone_response_has_nonempty_worker_request_id(client) -> None:
    """clone response 的 ``worker_request_id`` 必须非空（UUID hex）。"""
    req = WorkerCloneRequest(
        job_id="j",
        user_id="u",
        speaker_id="s",
        speaker_name="s",
        target_model="cosyvoice-v3.5-flash",
        sample=WorkerCloneSample(kind="download_url", url="http://x/y.wav", sha256="a" * 64),
        source_segments=(1,),
        consent=WorkerCloneConsent(voice_clone_confirmed=True, confirmed_at=_now_iso()),
    )
    resp = client.clone(req)
    assert resp.worker_request_id, "clone response 必须含非空 worker_request_id"
    assert len(resp.worker_request_id) >= 16, "worker_request_id 应是 UUID hex"
    # mock 路径下 provider_request_id 为 None
    assert resp.provider_request_id is None


def test_synthesize_batch_response_has_nonempty_worker_request_id(client) -> None:
    """synthesize-batch response 的 batch 顶层 ``worker_request_id`` 必须非空。"""
    text = "测试"
    req = WorkerSynthesizeBatchRequest(
        job_id="j",
        target_model="cosyvoice-v3.5-flash",
        audio_format="wav",
        segments=(WorkerSegmentRequest(
            segment_id=1, speaker_id="a", voice_id="v", text=text,
            speech_rate=1.0, text_hash=compute_text_hash(text),
        ),),
    )
    resp = client.synthesize_batch(req)
    assert resp.worker_request_id
    assert len(resp.worker_request_id) >= 16
    # mock 路径下 segment.provider_request_id 为 None
    assert resp.segments[0].provider_request_id is None


def test_delete_voice_response_has_nonempty_worker_request_id(client) -> None:
    req = WorkerDeleteVoiceRequest(job_id="j", user_id="u", reason="test")
    resp = client.delete_voice("v_xyz", req)
    assert resp.worker_request_id
    assert resp.provider_request_id is None


def test_segment_billed_chars_uses_billing_character_count(client) -> None:
    """Phase 4.0b §B：billed_chars 不再用 len(text)，中文必须按 CJK = 2 算。

    锁定 plan §通过标准：``"你好"`` → 4，而不是 len("你好") = 2。
    """
    text = "你好"
    req = WorkerSynthesizeBatchRequest(
        job_id="j",
        target_model="cosyvoice-v3.5-flash",
        audio_format="wav",
        segments=(WorkerSegmentRequest(
            segment_id=1, speaker_id="a", voice_id="v", text=text,
            speech_rate=1.0, text_hash=compute_text_hash(text),
        ),),
    )
    resp = client.synthesize_batch(req)
    assert resp.segments[0].billed_chars == 4
    assert resp.segments[0].billed_chars != len(text)


# ---------------------------------------------------------------------------
# 3. AST 守卫：synthesize_segment 不许用 len(seg.text)
# ---------------------------------------------------------------------------

def test_real_synthesize_segment_does_not_use_len_text() -> None:
    """Phase 4.0b §B 硬规则：``RealCosyvoiceProvider.synthesize_segment``
    必须用 ``billing_character_count``，不能用 ``len(seg.text)``。

    AST 扫该方法体；出现 ``len(seg.text)`` / ``len(text)`` 立即红。
    """
    tree = ast.parse(REAL_COSYVOICE_PATH.read_text(encoding="utf-8"))

    target_src: str = ""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "RealCosyvoiceProvider"):
            continue
        for member in node.body:
            if isinstance(member, ast.FunctionDef) and member.name == "synthesize_segment":
                target_src = ast.unparse(member)
                break

    assert target_src, "找不到 RealCosyvoiceProvider.synthesize_segment"

    # 必须 import / 调用 billing_character_count
    assert "billing_character_count" in target_src, (
        "synthesize_segment 必须用 billing_character_count（plan §Phase 4.0b §B）"
    )
    # 禁止 len(seg.text) / len(text) 字面量
    for pattern in ("len(seg.text)", "len(seg . text)", "len(text)"):
        assert pattern not in target_src, (
            f"synthesize_segment 出现 {pattern!r}；plan §Phase 4.0b §B "
            f"禁止用 len() 估算 billed_chars（中文低估）"
        )


def test_real_clone_uses_get_last_request_id() -> None:
    """RealCosyvoiceProvider.clone 必须从 SDK 取 provider_request_id。

    plan §Phase 4.0a 实测：``service.get_last_request_id()`` 可用，
    必须在 ``create_voice`` 后立即取（而不是 ``query_voice`` 之后），
    作为 clone 计费的对账主锚点。
    """
    tree = ast.parse(REAL_COSYVOICE_PATH.read_text(encoding="utf-8"))
    target_src: str = ""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "RealCosyvoiceProvider"):
            continue
        for member in node.body:
            if isinstance(member, ast.FunctionDef) and member.name == "clone":
                target_src = ast.unparse(member)
                break

    assert "_safe_get_last_request_id" in target_src, (
        "clone 必须调 _safe_get_last_request_id 取 provider_request_id"
    )


def test_real_synthesize_segment_uses_synth_last_request_id() -> None:
    """RealCosyvoiceProvider.synthesize_segment 必须从 synthesizer 取
    last_request_id（plan §Phase 4.0a 决策）。"""
    tree = ast.parse(REAL_COSYVOICE_PATH.read_text(encoding="utf-8"))
    target_src: str = ""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "RealCosyvoiceProvider"):
            continue
        for member in node.body:
            if isinstance(member, ast.FunctionDef) and member.name == "synthesize_segment":
                target_src = ast.unparse(member)
                break

    assert "_safe_get_synth_request_id" in target_src, (
        "synthesize_segment 必须调 _safe_get_synth_request_id"
    )


# ---------------------------------------------------------------------------
# 4. Outcome dataclass 类型守卫
# ---------------------------------------------------------------------------

def test_provider_protocol_uses_outcome_dataclasses() -> None:
    """``CosyvoiceProvider`` 三个方法的返回类型必须是 outcome dataclass，
    不是裸 ``str`` / ``tuple``。防回退到 Phase 4.0b 之前的 API。

    注：``base.py`` 用 ``from __future__ import annotations``，所以
    ``inspect.signature.return_annotation`` 返字符串而非类对象。用
    ``typing.get_type_hints`` 解析或直接比较字符串。
    """
    from services.mainland_worker.worker.providers.base import CosyvoiceProvider

    sig_clone = inspect.signature(CosyvoiceProvider.clone)
    assert str(sig_clone.return_annotation) == "CloneOutcome"

    sig_synth = inspect.signature(CosyvoiceProvider.synthesize_segment)
    assert str(sig_synth.return_annotation) == "SegmentSynthesisOutcome"

    sig_delete = inspect.signature(CosyvoiceProvider.delete_voice)
    assert str(sig_delete.return_annotation) == "DeleteOutcome"


def test_outcome_dataclasses_carry_provider_request_id() -> None:
    """Outcome dataclass 必须能携带 ``provider_request_id``。"""
    from services.mainland_worker.worker.providers.base import (
        CloneOutcome,
        DeleteOutcome,
        SegmentSynthesisOutcome,
    )

    assert "provider_request_id" in CloneOutcome.__annotations__
    assert "provider_request_id" in SegmentSynthesisOutcome.__annotations__
    assert "provider_request_id" in DeleteOutcome.__annotations__

    # 默认值都是 None（nullable）
    assert CloneOutcome(voice_id="v").provider_request_id is None
    assert SegmentSynthesisOutcome(
        audio_bytes=b"", duration_ms=0, billed_chars=0,
    ).provider_request_id is None
    assert DeleteOutcome().provider_request_id is None


# ---------------------------------------------------------------------------
# 5. fail-closed: worker_request_id 必填校验（Codex 2026-05-25 P1 finding）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response_cls, kwargs", [
    (WorkerCloneResponse, dict(
        ok=True, voice_id="v", provider="p", tts_provider="t",
        target_model="m", region_constraint="mainland_only",
        requires_worker=True, platform="dashscope_mainland",
        sample_sha256="a" * 64, created_at="2026-01-01T00:00:00Z",
    )),
    (WorkerDeleteVoiceResponse, dict(
        ok=True, voice_id="v", deleted_at="2026-01-01T00:00:00Z",
    )),
])
def test_response_dataclass_rejects_empty_worker_request_id(response_cls, kwargs) -> None:
    """``__post_init__`` 拒空 ``worker_request_id``（dataclass 层 fail-closed）。"""
    # 缺字段 / 空串 / 纯空白都拒
    for bad in ("", "   ", None):
        bad_kwargs = {**kwargs, "worker_request_id": bad} if bad is not None else kwargs
        with pytest.raises(ValueError, match="worker_request_id must be non-empty"):
            response_cls(**bad_kwargs)


def test_synth_batch_response_rejects_empty_worker_request_id() -> None:
    """``WorkerSynthesizeBatchResponse.__post_init__`` 拒空 worker_request_id。"""
    from services.mainland_worker.types import WorkerArtifactPackage

    pkg = WorkerArtifactPackage(
        kind="inline_base64", download_url="", sha256="x" * 64,
        expires_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(ValueError, match="worker_request_id must be non-empty"):
        WorkerSynthesizeBatchResponse(
            ok=True, job_id="j", target_model="m",
            segments=(), package=pkg, worker_request_id="",
        )


def test_client_rejects_clone_response_missing_worker_request_id(tmp_path: Path) -> None:
    """Codex 2026-05-25 P1：worker / Nginx / 旧版本返响应漏 worker_request_id 时
    client 必须 fail-closed，不能默默接受空 id。"""
    from services.mainland_worker.client import WorkerError

    class _BadTransport(httpx.BaseTransport):
        """模拟 worker 漏字段：返合法 JSON 但没有 worker_request_id。"""
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": True,
                "voice_id": "v",
                "provider": "cosyvoice_voice_clone",
                "tts_provider": "cosyvoice",
                "target_model": "cosyvoice-v3.5-flash",
                "region_constraint": "mainland_only",
                "requires_worker": True,
                "platform": "dashscope_mainland",
                "sample_sha256": "a" * 64,
                "created_at": "2026-01-01T00:00:00Z",
                # 故意不带 worker_request_id
            })

    c = MainlandWorkerClient(
        base_url="http://w.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=_BadTransport(),
    )
    try:
        req = WorkerCloneRequest(
            job_id="j", user_id="u", speaker_id="s", speaker_name="s",
            target_model="cosyvoice-v3.5-flash",
            sample=WorkerCloneSample(kind="download_url", url="http://x", sha256="a" * 64),
            source_segments=(1,),
            consent=WorkerCloneConsent(voice_clone_confirmed=True, confirmed_at=_now_iso()),
        )
        with pytest.raises(WorkerError) as exc:
            c.clone(req)
        assert exc.value.code == "protocol_invalid_response"
        assert exc.value.http_status == 502
    finally:
        c.close()


def test_client_rejects_synth_batch_response_missing_worker_request_id(tmp_path: Path) -> None:
    from services.mainland_worker.client import WorkerError

    class _BadTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": True,
                "job_id": "j",
                "target_model": "cosyvoice-v3.5-flash",
                "segments": [],
                "package": {
                    "kind": "inline_base64", "download_url": "",
                    "sha256": "x" * 64, "expires_at": "2026-01-01T00:00:00Z",
                    "inline_base64": "",
                },
                # 故意不带 worker_request_id
            })

    c = MainlandWorkerClient(
        base_url="http://w.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=_BadTransport(),
    )
    try:
        text = "test"
        req = WorkerSynthesizeBatchRequest(
            job_id="j", target_model="cosyvoice-v3.5-flash", audio_format="wav",
            segments=(WorkerSegmentRequest(
                segment_id=1, speaker_id="a", voice_id="v", text=text,
                speech_rate=1.0, text_hash=compute_text_hash(text),
            ),),
        )
        with pytest.raises(WorkerError) as exc:
            c.synthesize_batch(req)
        assert exc.value.code == "protocol_invalid_response"
    finally:
        c.close()


def test_client_rejects_delete_response_missing_worker_request_id(tmp_path: Path) -> None:
    from services.mainland_worker.client import WorkerError

    class _BadTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": True,
                "voice_id": "v",
                "deleted_at": "2026-01-01T00:00:00Z",
                # 故意不带 worker_request_id
            })

    c = MainlandWorkerClient(
        base_url="http://w.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=_BadTransport(),
    )
    try:
        with pytest.raises(WorkerError) as exc:
            c.delete_voice("v", WorkerDeleteVoiceRequest(job_id="j", user_id="u", reason="t"))
        assert exc.value.code == "protocol_invalid_response"
    finally:
        c.close()


def test_client_rejects_blank_worker_request_id() -> None:
    """空白字符 ``"   "`` 也按缺字段处理（strip 后为空）。"""
    from services.mainland_worker.client import WorkerError

    class _BadTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ok": True,
                "voice_id": "v",
                "deleted_at": "2026-01-01T00:00:00Z",
                "worker_request_id": "   ",  # 仅空白
            })

    c = MainlandWorkerClient(
        base_url="http://w.test",
        credentials=WorkerCredentials(key_id=KEY_ID, secret=SECRET),
        transport=_BadTransport(),
    )
    try:
        with pytest.raises(WorkerError) as exc:
            c.delete_voice("v", WorkerDeleteVoiceRequest(job_id="j", user_id="u", reason="t"))
        assert exc.value.code == "protocol_invalid_response"
    finally:
        c.close()
