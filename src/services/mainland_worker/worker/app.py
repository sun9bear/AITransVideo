"""Worker FastAPI app — Phase 1 mock-only。

四个 endpoint（plan §Worker API）：

- ``GET  /healthz``                              健康检查（不验签）
- ``POST /cosyvoice/clone``                      创建自定义音色
- ``POST /cosyvoice/synthesize-batch``           批量 / 单段合成
- ``DELETE /cosyvoice/voices/{voice_id}``        删除音色

设计要点：

- HMAC 校验作为 FastAPI ``Depends``，每个 cosyvoice 路由都挂；
  ``/healthz`` 故意不挂，让容器探活 / Nginx 健康检查不被签名阻塞。
- handler 直接读 raw body bytes（``await request.body()``），不让
  FastAPI 自动 parse — 这样签名校验的 body 与 handler 处理的 body
  字节级一致，避免 normalize/排序差异。
- 错误响应统一形状：``{"ok": False, "error": {"code": "...", "message": "..."}}``
  签名错误返 401，业务错误返 400/500，retryable 错误带 ``retryable: true``。
- 单段 / batch 合成走**同一个 handler** —— Studio post-edit 的
  regenerate-tts 通过传 ``len(segments) == 1`` 复用，不开 ``/synthesize-one``
  （plan §Studio Post-Edit / Regenerate TTS）。

retry 上限：worker 内部不重试 provider 调用。"retry 最多 N 次"的语义
完全由 US client（``MainlandWorkerClient``）承担，避免双层 retry 把
hard cap 撑爆（CLAUDE.md "batch / loop / retry 里无上限调用付费 API"）。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import time
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from services.mainland_worker.hmac_auth import (
    HmacKeyStore,
    InMemoryHmacKeyStore,
    InMemoryNonceStore,
    NonceStore,
    SignatureError,
    verify_request,
)
from services.mainland_worker.types import (
    PLATFORM_DASHSCOPE_MAINLAND,
    PROVIDER_COSYVOICE_VOICE_CLONE,
    REGION_CONSTRAINT_MAINLAND_ONLY,
    TTS_PROVIDER_COSYVOICE,
    WorkerCloneConsent,
    WorkerCloneRequest,
    WorkerCloneSample,
    WorkerSegmentRequest,
    WorkerSynthesizeBatchRequest,
    compute_text_hash,
)
from services.mainland_worker.worker.audit import (
    AuditLogger,
    JsonlAuditLogger,
)
from services.mainland_worker.worker.config import (
    WORKER_MODE_LIVE,
    WORKER_MODE_MOCK,
    WorkerConfig,
    load_from_env,
)
from services.mainland_worker.worker.providers.base import (
    CosyvoiceProvider,
    ProviderError,
)
from services.mainland_worker.worker.providers.mock_cosyvoice import (
    MockCosyvoiceProvider,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App 工厂
# ---------------------------------------------------------------------------

def create_app(
    *,
    config: WorkerConfig,
    cosyvoice_provider: CosyvoiceProvider | None = None,
    key_store: HmacKeyStore | None = None,
    nonce_store: NonceStore | None = None,
    audit_logger: AuditLogger | None = None,
) -> FastAPI:
    """构造 FastAPI app。

    依赖注入：所有可替换组件都通过参数传入，方便测试。生产路径下
    ``main()`` 入口从 ``config.WorkerConfig`` 一次性装配；测试路径下
    单独传 mock provider / in-memory store。
    """
    if cosyvoice_provider is None:
        if config.mode == WORKER_MODE_MOCK:
            cosyvoice_provider = MockCosyvoiceProvider()
        elif config.mode == WORKER_MODE_LIVE:
            # Phase 2 落地：真实 DashScope provider。lazy import 让 mock 模式
            # 启动路径完全不感知 RealCosyvoiceProvider（dashscope SDK 也是方法体
            # 内 lazy import，所以 mock 模式不会因为 dashscope 不可用而挂）。
            import os as _os
            from services.mainland_worker.worker.providers.real_cosyvoice import (
                RealCosyvoiceProvider,
            )

            api_key = _os.environ.get("DASHSCOPE_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError(
                    "WORKER_MODE=live requires DASHSCOPE_API_KEY env var; "
                    "set it before starting the worker (Phase 2 落地约束)"
                )
            cosyvoice_provider = RealCosyvoiceProvider(api_key=api_key)
        else:
            raise RuntimeError(
                f"unknown WorkerConfig.mode={config.mode!r}; "
                f"must be 'mock' or 'live'"
            )

    if key_store is None:
        key_store = InMemoryHmacKeyStore(list(config.hmac_keys))

    if nonce_store is None:
        nonce_store = InMemoryNonceStore()

    if audit_logger is None:
        # 生产默认：落盘 JSONL append-only（plan §审计日志）。测试路径
        # 应当显式传 ``InMemoryAuditLogger()`` 避免污染真实文件系统。
        audit_logger = JsonlAuditLogger(config.audit_log_path)

    app = FastAPI(
        title=config.worker_name,
        version="0.1.0",
        docs_url=None,  # 内部 API，不暴露 swagger
        redoc_url=None,
    )

    # Stash deps on app.state 让 handler 取
    app.state.config = config
    app.state.cosyvoice_provider = cosyvoice_provider
    app.state.key_store = key_store
    app.state.nonce_store = nonce_store
    app.state.audit_logger = audit_logger

    _install_routes(app)
    return app


# ---------------------------------------------------------------------------
# HMAC 校验 dependency
# ---------------------------------------------------------------------------

async def _verify_hmac_dependency(request: Request) -> bytes:
    """FastAPI dependency：验签 + 返回 raw body bytes 给 handler 使用。

    handler 拿到的 body 与签名校验的 body 是同一份字节，避免 FastAPI
    auto-parse 引入潜在 normalize 差异。
    """
    body = await request.body()

    headers = dict(request.headers)  # FastAPI 已经是小写 key
    # verify_request 接受任意大小写 key，但传小写最稳
    try:
        verify_request(
            method=request.method,
            path=request.url.path,
            headers=headers,
            body=body,
            key_store=request.app.state.key_store,
            nonce_store=request.app.state.nonce_store,
        )
    except SignatureError as exc:
        logger.warning("[mainland-worker] signature reject: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "signature_invalid", "message": str(exc)},
        ) from exc
    return body


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _install_routes(app: FastAPI) -> None:
    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        config: WorkerConfig = request.app.state.config
        return {
            "ok": True,
            "worker": config.worker_name,
            "region": config.worker_region,
            "providers": {
                "cosyvoice": {
                    "configured": True,
                    "mode": config.mode,
                },
            },
        }

    @app.post("/cosyvoice/clone")
    async def cosyvoice_clone(
        request: Request,
        body: bytes = Depends(_verify_hmac_dependency),
    ) -> JSONResponse:
        return await _handle_clone(request, body)

    @app.post("/cosyvoice/synthesize-batch")
    async def cosyvoice_synthesize_batch(
        request: Request,
        body: bytes = Depends(_verify_hmac_dependency),
    ) -> JSONResponse:
        return await _handle_synthesize_batch(request, body)

    @app.delete("/cosyvoice/voices/{voice_id}")
    async def cosyvoice_delete(
        voice_id: str,
        request: Request,
        body: bytes = Depends(_verify_hmac_dependency),
    ) -> JSONResponse:
        return await _handle_delete(request, body, voice_id)


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------

async def _handle_clone(request: Request, body: bytes) -> JSONResponse:
    provider: CosyvoiceProvider = request.app.state.cosyvoice_provider
    audit: AuditLogger = request.app.state.audit_logger

    payload = _parse_json_body(body)
    # Phase 4.0b §A: worker_request_id 必填，作为审计 trail 主锚点；
    # 同一个 UUID 在 audit log + response 都用，让 Gateway / worker / DashScope
    # 三段可以串起来。
    worker_request_id = uuid.uuid4().hex
    created_at = _utc_now_iso()

    # Fix #2：consent 校验失败也要进审计。这种事件正是"有人在没授权
    # 的情况下尝试 clone"的证据，对运营和安全审计价值最高。先尽力
    # 从原始 payload 抽 id 字段（这些字段不属于敏感数据，未通过完整
    # 校验也能落盘）。
    try:
        req = _build_clone_request(payload)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        audit.emit({
            "event_id": uuid.uuid4().hex,
            "request_id": worker_request_id,
            "job_id": str(payload.get("job_id") or ""),
            "user_id": str(payload.get("user_id") or ""),
            "speaker_id": str(payload.get("speaker_id") or ""),
            "operation": "clone",
            "provider": PROVIDER_COSYVOICE_VOICE_CLONE,
            "target_model": str(payload.get("target_model") or ""),
            "status": "failed",
            "error_code": str(detail.get("code") or "invalid_request"),
            "created_at": created_at,
        })
        raise

    audit_event: dict[str, Any] = {
        "event_id": uuid.uuid4().hex,
        "request_id": worker_request_id,
        "job_id": req.job_id,
        "user_id": req.user_id,
        "speaker_id": req.speaker_id,
        "operation": "clone",
        "provider": PROVIDER_COSYVOICE_VOICE_CLONE,
        "target_model": req.target_model,
        "created_at": created_at,
    }

    try:
        outcome = provider.clone(req)
    except ProviderError as exc:
        audit.emit({
            **audit_event,
            "status": "failed",
            "error_code": exc.code,
        })
        return _provider_error_response(exc)

    audit_emit_data: dict[str, Any] = {
        **audit_event,
        "voice_id": outcome.voice_id,
        "status": "ok",
    }
    if outcome.provider_request_id:
        audit_emit_data["provider_request_id"] = outcome.provider_request_id
    audit.emit(audit_emit_data)

    return JSONResponse({
        "ok": True,
        "voice_id": outcome.voice_id,
        "provider": PROVIDER_COSYVOICE_VOICE_CLONE,
        "tts_provider": TTS_PROVIDER_COSYVOICE,
        "target_model": req.target_model,
        "region_constraint": REGION_CONSTRAINT_MAINLAND_ONLY,
        "requires_worker": True,
        "platform": PLATFORM_DASHSCOPE_MAINLAND,
        "sample_sha256": req.sample.sha256,
        "created_at": created_at,
        # Phase 4.0b §A
        "worker_request_id": worker_request_id,
        "provider_request_id": outcome.provider_request_id,
    })


def _build_clone_request(payload: dict[str, Any]) -> WorkerCloneRequest:
    try:
        sample_raw = payload["sample"]
        consent_raw = payload["consent"]
        # plan §Clone Flow 要求用户**显式**授权。``bool("false") == True`` 是
        # 个经典坑：JSON 客户端用字符串 "false" / "0" / "no" 时 Python 仍然
        # 视为 truthy，等于绕过授权检查。这里强制 ``is True`` —— 任何非真布尔
        # 都拒，让"未授权 → 走付费 API"无法在 wire 协议层混入。
        confirmed_raw = consent_raw["voice_clone_confirmed"]
        if confirmed_raw is not True:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "consent_required",
                    "message": (
                        "consent.voice_clone_confirmed must be JSON literal `true` "
                        f"(got {confirmed_raw!r}); plan §Clone Flow 要求用户显式授权"
                    ),
                },
            )
        return WorkerCloneRequest(
            job_id=str(payload["job_id"]),
            user_id=str(payload["user_id"]),
            speaker_id=str(payload["speaker_id"]),
            speaker_name=str(payload.get("speaker_name") or payload["speaker_id"]),
            target_model=str(payload["target_model"]),
            sample=WorkerCloneSample(
                kind=str(sample_raw["kind"]),
                url=str(sample_raw["url"]),
                sha256=str(sample_raw["sha256"]),
            ),
            source_segments=tuple(int(x) for x in payload.get("source_segments", [])),
            consent=WorkerCloneConsent(
                voice_clone_confirmed=True,  # 已经在上面 strict 校验
                confirmed_at=str(consent_raw["confirmed_at"]),
            ),
        )
    except HTTPException:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_request", "message": f"clone payload invalid: {exc}"},
        ) from exc


# ---------------------------------------------------------------------------
# Synthesize batch
# ---------------------------------------------------------------------------

async def _handle_synthesize_batch(request: Request, body: bytes) -> JSONResponse:
    provider: CosyvoiceProvider = request.app.state.cosyvoice_provider
    audit: AuditLogger = request.app.state.audit_logger

    payload = _parse_json_body(body)
    batch = _build_batch_request(payload)

    # Phase 4.0b §A: worker_request_id 必填，batch 顶层一个；每个 segment
    # 还会有 provider_request_id（real 模式从 SDK 取，mock 为 None）
    worker_request_id = uuid.uuid4().hex
    created_at = _utc_now_iso()
    expires_at = _utc_iso_at(time.time() + 3600)

    seg_results: list[dict[str, Any]] = []
    seg_files: list[tuple[str, bytes]] = []

    for seg in batch.segments:
        # text_hash 校验：client 传的 hash 与 worker 重算的必须一致
        expected_hash = compute_text_hash(seg.text)
        if seg.text_hash and seg.text_hash != expected_hash:
            audit.emit({
                "event_id": uuid.uuid4().hex,
                "request_id": worker_request_id,
                "job_id": batch.job_id,
                "operation": "synthesize_segment",
                "provider": TTS_PROVIDER_COSYVOICE,
                "target_model": batch.target_model,
                "speaker_id": seg.speaker_id,
                "segment_id": seg.segment_id,
                "voice_id": seg.voice_id,
                "status": "failed",
                "error_code": "text_hash_mismatch",
                "created_at": _utc_now_iso(),
            })
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "ok": False,
                    "error": {
                        "code": "text_hash_mismatch",
                        "message": f"segment {seg.segment_id}: client text_hash differs from server recompute",
                    },
                },
            )

        try:
            outcome = provider.synthesize_segment(
                seg, target_model=batch.target_model
            )
        except ProviderError as exc:
            audit.emit({
                "event_id": uuid.uuid4().hex,
                "request_id": worker_request_id,
                "job_id": batch.job_id,
                "operation": "synthesize_segment",
                "provider": TTS_PROVIDER_COSYVOICE,
                "target_model": batch.target_model,
                "speaker_id": seg.speaker_id,
                "segment_id": seg.segment_id,
                "voice_id": seg.voice_id,
                "status": "failed",
                "error_code": exc.code,
                "created_at": _utc_now_iso(),
            })
            return _provider_error_response(exc)

        wav_bytes = outcome.audio_bytes
        duration_ms = outcome.duration_ms
        billed_chars = outcome.billed_chars
        seg_provider_request_id = outcome.provider_request_id

        audio_path = f"segments/segment_{seg.segment_id:03d}_{seg.speaker_id}.wav"
        sha = hashlib.sha256(wav_bytes).hexdigest()
        seg_results.append({
            "segment_id": seg.segment_id,
            "speaker_id": seg.speaker_id,
            "voice_id": seg.voice_id,
            "audio_path": audio_path,
            "duration_ms": duration_ms,
            "billed_chars": billed_chars,
            "sha256": sha,
            # Phase 4.0b §A: segment 级 provider_request_id（nullable）
            "provider_request_id": seg_provider_request_id,
        })
        seg_files.append((audio_path, wav_bytes))

        synth_audit: dict[str, Any] = {
            "event_id": uuid.uuid4().hex,
            "request_id": worker_request_id,
            "job_id": batch.job_id,
            "operation": "synthesize_segment",
            "provider": TTS_PROVIDER_COSYVOICE,
            "target_model": batch.target_model,
            "speaker_id": seg.speaker_id,
            "segment_id": seg.segment_id,
            "voice_id": seg.voice_id,
            "status": "ok",
            "duration_ms": duration_ms,
            "billed_chars": billed_chars,
            "audio_seconds": duration_ms / 1000.0,
            "created_at": _utc_now_iso(),
        }
        if seg_provider_request_id:
            synth_audit["provider_request_id"] = seg_provider_request_id
        audit.emit(synth_audit)

    package_bytes, package_sha = _build_zip_package(seg_files)
    package = {
        "kind": "inline_base64",
        "download_url": "",  # Phase 1 mock 不落盘；Phase 3 切 zip 文件下载
        "sha256": package_sha,
        "expires_at": expires_at,
        "inline_base64": base64.b64encode(package_bytes).decode("ascii"),
    }

    return JSONResponse({
        "ok": True,
        "job_id": batch.job_id,
        "target_model": batch.target_model,
        "segments": seg_results,
        "package": package,
        # Phase 4.0b §A: batch 顶层 worker_request_id（必填）
        "worker_request_id": worker_request_id,
    })


def _build_batch_request(payload: dict[str, Any]) -> WorkerSynthesizeBatchRequest:
    try:
        segs_raw = payload["segments"]
        if not isinstance(segs_raw, list) or not segs_raw:
            raise ValueError("segments must be non-empty list")
        segments = []
        for s in segs_raw:
            segments.append(WorkerSegmentRequest(
                segment_id=int(s["segment_id"]),
                speaker_id=str(s["speaker_id"]),
                voice_id=str(s["voice_id"]),
                text=str(s["text"]),
                speech_rate=float(s.get("speech_rate", 1.0)),
                target_duration_ms=(int(s["target_duration_ms"]) if s.get("target_duration_ms") is not None else None),
                text_hash=str(s.get("text_hash") or ""),
            ))
        return WorkerSynthesizeBatchRequest(
            job_id=str(payload["job_id"]),
            target_model=str(payload["target_model"]),
            audio_format=str(payload.get("audio_format", "wav")),
            segments=tuple(segments),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_request", "message": f"synthesize-batch payload invalid: {exc}"},
        ) from exc


def _build_zip_package(seg_files: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    """打包 segments 为 zip，返回 (zip_bytes, sha256_hex)。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in seg_files:
            zf.writestr(name, data)
    raw = buf.getvalue()
    sha = hashlib.sha256(raw).hexdigest()
    return raw, sha


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

async def _handle_delete(request: Request, body: bytes, voice_id: str) -> JSONResponse:
    provider: CosyvoiceProvider = request.app.state.cosyvoice_provider
    audit: AuditLogger = request.app.state.audit_logger

    payload = _parse_json_body(body)
    job_id = str(payload.get("job_id") or "")
    user_id = str(payload.get("user_id") or "")
    reason = str(payload.get("reason") or "")

    # Phase 4.0b §A: worker_request_id 必填
    worker_request_id = uuid.uuid4().hex
    deleted_at = _utc_now_iso()

    try:
        outcome = provider.delete_voice(voice_id)
    except ProviderError as exc:
        audit.emit({
            "event_id": uuid.uuid4().hex,
            "request_id": worker_request_id,
            "job_id": job_id,
            "user_id": user_id,
            "voice_id": voice_id,
            "operation": "delete_voice",
            "provider": TTS_PROVIDER_COSYVOICE,
            "status": "failed",
            "error_code": exc.code,
            "created_at": deleted_at,
        })
        return _provider_error_response(exc)

    audit_event: dict[str, Any] = {
        "event_id": uuid.uuid4().hex,
        "request_id": worker_request_id,
        "job_id": job_id,
        "user_id": user_id,
        "voice_id": voice_id,
        "operation": "delete_voice",
        "provider": TTS_PROVIDER_COSYVOICE,
        "status": "ok",
        "created_at": deleted_at,
    }
    if outcome.provider_request_id:
        audit_event["provider_request_id"] = outcome.provider_request_id
    audit.emit(audit_event)

    return JSONResponse({
        "ok": True,
        "voice_id": voice_id,
        "deleted_at": deleted_at,
        # Phase 4.0b §A
        "worker_request_id": worker_request_id,
        "provider_request_id": outcome.provider_request_id,
    })


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _parse_json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_json", "message": f"body is not valid JSON: {exc}"},
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_json", "message": "body must be JSON object"},
        )
    return data


def _provider_error_response(exc: ProviderError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY if exc.retryable else status.HTTP_400_BAD_REQUEST,
        content={
            "ok": False,
            "error": {
                "code": exc.code,
                "message": str(exc),
                "retryable": exc.retryable,
            },
        },
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_iso_at(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------------------------------------------------------------------------
# Env-backed ASGI 入口
# ---------------------------------------------------------------------------

def create_app_from_env() -> FastAPI:
    """从 env 装配一份生产 FastAPI app。

    武汉 ECS 部署时 systemd unit / uvicorn 命令可以直接：

    ::

        uvicorn services.mainland_worker.worker.app:app --host 127.0.0.1 --port 8791

    或者：

    ::

        gunicorn services.mainland_worker.worker.app:app -k uvicorn.workers.UvicornWorker

    需要的 env 变量见 ``worker.config.load_from_env``。
    """
    config = load_from_env()
    return create_app(config=config)


def _lazy_app() -> FastAPI:
    """模块级 ASGI 句柄；首次访问时构造。

    使用 lazy 模式而非 module top-level ``app = create_app_from_env()`` 的
    原因：测试 / 本地开发时 ``from services.mainland_worker.worker import app``
    不应触发 env 读取（缺 ``WORKER_HMAC_KEYS`` 会直接 ValueError 让 import
    失败）。lazy 在 ASGI server 真正调用时才报错，单元测试 import 模块
    不受影响。
    """
    global _APP_SINGLETON
    if _APP_SINGLETON is None:
        _APP_SINGLETON = create_app_from_env()
    return _APP_SINGLETON


_APP_SINGLETON: FastAPI | None = None


class _LazyASGI:
    """ASGI callable wrapper：第一次被 server 调用时才装配真 app。"""

    async def __call__(self, scope, receive, send):
        await _lazy_app()(scope, receive, send)


# 模块级 ASGI 入口
app = _LazyASGI()
