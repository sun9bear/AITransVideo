"""US 主机 → mainland worker HTTP client。

调用 worker 的唯一入口。封装：

- HMAC-SHA256 签名（plan §Worker API 通用请求头）
- 重试硬上限（plan §Retry）
- artifact 解包（Phase 1 inline base64；Phase 3 切 zip 下载）
- 业务错误转 ``WorkerError`` 异常

retry 策略（plan §Retry，client 端收口）：

- ``synthesize_batch``: HTTP / 网络错误最多 ``MAX_NETWORK_RETRIES`` 次，
  退避 ``RETRY_BACKOFF``。Provider 业务错误（worker 返 ``ok=false``）
  **不重试** —— 这种错误重试也不会变好，且会重复扣费。
- ``clone``: **永不重试**（plan §Clone "每次用户确认最多 1 次"）。
  网络错误也由用户重新点击触发。
- ``delete_voice``: 最多 ``MAX_NETWORK_RETRIES`` 次（幂等操作）。
- ``health``: 最多 1 次（健康检查不该被重试拖延）。

依赖：

- ``httpx``（已在 gateway/requirements.txt 中）
- 不 import 任何 ``services.tts.*`` —— client 是 worker 路径的入口，
  不能反向依赖 TTS pipeline 模块（守卫测试断言）。
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
from dataclasses import dataclass
from typing import Any

import httpx

from services.mainland_worker.hmac_auth import (
    HEADER_JOB_ID,
    HEADER_KEY_ID,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    SignatureMaterial,
    sign,
)
from services.mainland_worker.types import (
    WorkerArtifactPackage,
    WorkerCloneRequest,
    WorkerCloneResponse,
    WorkerDeleteVoiceRequest,
    WorkerDeleteVoiceResponse,
    WorkerHealthResponse,
    WorkerProviderHealth,
    WorkerSegmentResult,
    WorkerSynthesizeBatchRequest,
    WorkerSynthesizeBatchResponse,
)


logger = logging.getLogger(__name__)


# ---- retry 策略（plan §Retry，client 收口） ----
#
# plan §Retry 把单段 TTS 和 batch 当成两种不同语义：
#
#   单段 TTS  ：最多 3 次（含首次）—— 退避 1s -> 5s -> 15s
#   多段 batch：整体最多重提 1 次 == 总 2 次 attempts
#
# 原因：多段 batch 失败重提整批，等于把已经成功的 segment 又算一次
# provider 调用（Phase 1 mock 不做 segment-level 幂等去重；Phase 4 真实
# provider 时 plan 要求"batch 重提必须跳过已经成功且 sha256 校验通过的
# segment"）。在没有去重之前把多段批量的总尝试压到 2 次，可以防失败
# 重提把付费 provider 调用次数翻番。
#
# ``MAX_NETWORK_RETRIES`` 现在是"client 配置的上限"，实际 endpoint 内部
# 还会再按 plan 语义夹一道：
#
#   - clone:              max_attempts = 1（plan §Retry/Clone "每次用户
#                         确认最多 1 次 provider call"）
#   - synthesize_batch:   单段时 min(MAX_NETWORK_RETRIES, 3)
#                         多段时 min(MAX_NETWORK_RETRIES, 2)
#   - delete_voice:       max_attempts = MAX_NETWORK_RETRIES
#   - health:             max_attempts = 1
#
MAX_NETWORK_RETRIES = 3
SINGLE_SEGMENT_MAX_ATTEMPTS = 3
MULTI_SEGMENT_MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 5.0, 15.0)
DEFAULT_TIMEOUT_SECONDS = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)


class WorkerError(Exception):
    """Worker 返回业务错误（``ok=false`` 或 4xx）。"""

    def __init__(self, message: str, *, code: str, http_status: int, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


class WorkerSignatureRejectedError(WorkerError):
    """Worker 拒绝签名（401）。通常是 HMAC key 配错或时间漂移。"""


class WorkerNetworkError(Exception):
    """连续网络错误超过 ``MAX_NETWORK_RETRIES``。"""


class WorkerArtifactIntegrityError(Exception):
    """Artifact bytes 与 worker 返回的 sha256 manifest 不匹配。

    可能原因：
    - 传输被中间人篡改（plan §Security：Phase 0/1 走 HTTP 仅靠 HMAC
      保护请求头，artifact body 仍可能被改）
    - Worker 内部 zip 打包逻辑漂移
    - 客户端解 base64 时数据被截断
    """


def _require_nonempty_worker_request_id(data: dict, response_kind: str) -> str:
    """Codex 2026-05-25 P1 finding：worker_request_id 是审计主锚点，必须
    非空。client 在 dataclass 构造前主动校验，给出明确 ``protocol_invalid_response``
    错误码，避免上层把它当业务错误处理。

    与 dataclass ``__post_init__`` 双层防护：client 这层抛 ``WorkerError``
    （HTTP 业务层），dataclass 那层是 fail-closed 兜底（防止 client 误改）。
    """
    raw = data.get("worker_request_id")
    if not raw:
        raise WorkerError(
            f"worker response missing required worker_request_id (kind={response_kind!r}); "
            "Phase 4.0b §A 要求 worker 必填此字段作为审计主锚点",
            code="protocol_invalid_response",
            http_status=502,
            retryable=False,
        )
    return str(raw).strip() or _fail_protocol(response_kind)


def _fail_protocol(response_kind: str) -> str:
    """worker_request_id 仅是空白字符也按缺字段处理。"""
    raise WorkerError(
        f"worker response has blank worker_request_id (kind={response_kind!r})",
        code="protocol_invalid_response",
        http_status=502,
        retryable=False,
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorkerCredentials:
    """Client 持有的签名凭证。"""
    key_id: str
    secret: str


class MainlandWorkerClient:
    """US 主机调用武汉 worker 的客户端。

    Parameters
    ----------
    base_url : str
        Worker API base，例如 ``http://8.148.83.128/internal/voice-clone``。
        注意末尾不加斜杠（构造请求时拼
        ``/cosyvoice/clone``、``/cosyvoice/synthesize-batch``）。
    credentials : WorkerCredentials
        当前签名用的 ``(key_id, secret)``。
    transport : httpx.BaseTransport | None
        测试可以传 ``httpx.ASGITransport(app=worker_app)`` 直接 in-process 调用。
    """

    def __init__(
        self,
        *,
        base_url: str,
        credentials: WorkerCredentials,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        max_network_retries: int = MAX_NETWORK_RETRIES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credentials = credentials
        self._timeout = timeout
        self._max_network_retries = max_network_retries

        # httpx Client 复用 connection pool；调用方可以选择持久化或一次性
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MainlandWorkerClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def health(self) -> WorkerHealthResponse:
        resp = self._send_request(
            method="GET",
            path="/healthz",
            body=b"",
            job_id="",
            sign_request=False,  # /healthz 不验签
            max_attempts=1,  # 健康检查不重试
        )
        data = resp.json()
        providers = {
            name: WorkerProviderHealth(
                configured=bool(p.get("configured", False)),
                mode=str(p.get("mode", "")),
            )
            for name, p in (data.get("providers") or {}).items()
        }
        return WorkerHealthResponse(
            ok=bool(data.get("ok", False)),
            worker=str(data.get("worker", "")),
            region=str(data.get("region", "")),
            providers=providers,
        )

    def clone(self, request: WorkerCloneRequest) -> WorkerCloneResponse:
        body = json.dumps(request.to_dict(), ensure_ascii=False).encode("utf-8")
        # plan §Retry/Clone：永不自动重试。网络错误也直接抛，给用户在 UI 手动重试。
        resp = self._send_request(
            method="POST",
            path="/cosyvoice/clone",
            body=body,
            job_id=request.job_id,
            sign_request=True,
            max_attempts=1,
        )
        data = resp.json()
        if not data.get("ok"):
            self._raise_worker_error(resp, data)
        # Phase 4.0b §A: worker_request_id 必填校验（Codex P1 fail-closed）
        worker_request_id = _require_nonempty_worker_request_id(data, "clone")
        return WorkerCloneResponse(
            ok=True,
            voice_id=str(data["voice_id"]),
            provider=str(data["provider"]),
            tts_provider=str(data["tts_provider"]),
            target_model=str(data["target_model"]),
            region_constraint=str(data["region_constraint"]),
            requires_worker=bool(data["requires_worker"]),
            platform=str(data["platform"]),
            sample_sha256=str(data["sample_sha256"]),
            created_at=str(data["created_at"]),
            worker_request_id=worker_request_id,
            provider_request_id=(str(data["provider_request_id"])
                                  if data.get("provider_request_id") else None),
        )

    def synthesize_batch(
        self, request: WorkerSynthesizeBatchRequest
    ) -> WorkerSynthesizeBatchResponse:
        """合成 batch — 单段 / 多段共用入口（plan §Studio Post-Edit）。

        Retry 上限按 segments 数量分两种：

        - **单段**（``len(segments) == 1``，Studio post-edit
          regenerate-tts 走这条）：``SINGLE_SEGMENT_MAX_ATTEMPTS = 3``，
          等价于 plan "单段 TTS 最多 3 次"。
        - **多段**（``len(segments) > 1``，主 pipeline batch 合成）：
          ``MULTI_SEGMENT_MAX_ATTEMPTS = 2``，等价于 plan "batch 整体最多
          重提 1 次"。

        与 ``self._max_network_retries`` 取 ``min`` —— 调用方可以从外部
        把上限调得更小（例如灰度阶段限到 1），但不能让多段 batch 把它
        放大到 3 次。
        """
        body = json.dumps(request.to_dict(), ensure_ascii=False).encode("utf-8")
        is_single_segment = len(request.segments) == 1
        if is_single_segment:
            max_attempts = min(self._max_network_retries, SINGLE_SEGMENT_MAX_ATTEMPTS)
        else:
            max_attempts = min(self._max_network_retries, MULTI_SEGMENT_MAX_ATTEMPTS)
        resp = self._send_request(
            method="POST",
            path="/cosyvoice/synthesize-batch",
            body=body,
            job_id=request.job_id,
            sign_request=True,
            max_attempts=max_attempts,
        )
        data = resp.json()
        if not data.get("ok"):
            self._raise_worker_error(resp, data)

        segments = tuple(
            WorkerSegmentResult(
                segment_id=int(s["segment_id"]),
                speaker_id=str(s["speaker_id"]),
                voice_id=str(s["voice_id"]),
                audio_path=str(s["audio_path"]),
                duration_ms=int(s["duration_ms"]),
                billed_chars=int(s["billed_chars"]),
                sha256=str(s["sha256"]),
                # Phase 4.0b §A: segment 级 provider_request_id（nullable）
                provider_request_id=(str(s["provider_request_id"])
                                       if s.get("provider_request_id") else None),
            )
            for s in data["segments"]
        )

        pkg_raw = data["package"]
        inline_bytes: bytes | None = None
        if pkg_raw.get("kind") == "inline_base64":
            inline_b64 = pkg_raw.get("inline_base64") or ""
            inline_bytes = base64.b64decode(inline_b64) if inline_b64 else b""
        package = WorkerArtifactPackage(
            kind=str(pkg_raw["kind"]),
            download_url=str(pkg_raw.get("download_url") or ""),
            sha256=str(pkg_raw["sha256"]),
            expires_at=str(pkg_raw["expires_at"]),
            inline_bytes=inline_bytes,
        )

        # Phase 4.0b §A: batch 顶层 worker_request_id 必填（Codex P1 fail-closed）
        worker_request_id = _require_nonempty_worker_request_id(data, "synthesize-batch")
        return WorkerSynthesizeBatchResponse(
            ok=True,
            job_id=str(data["job_id"]),
            target_model=str(data["target_model"]),
            segments=segments,
            package=package,
            worker_request_id=worker_request_id,
        )

    def delete_voice(
        self, voice_id: str, request: WorkerDeleteVoiceRequest
    ) -> WorkerDeleteVoiceResponse:
        body = json.dumps(
            {
                "job_id": request.job_id,
                "user_id": request.user_id,
                "reason": request.reason,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        resp = self._send_request(
            method="DELETE",
            path=f"/cosyvoice/voices/{voice_id}",
            body=body,
            job_id=request.job_id,
            sign_request=True,
            max_attempts=self._max_network_retries,
        )
        data = resp.json()
        if not data.get("ok"):
            self._raise_worker_error(resp, data)
        # Phase 4.0b §A: worker_request_id 必填校验（Codex P1 fail-closed）
        worker_request_id = _require_nonempty_worker_request_id(data, "delete_voice")
        return WorkerDeleteVoiceResponse(
            ok=True,
            voice_id=str(data["voice_id"]),
            deleted_at=str(data["deleted_at"]),
            worker_request_id=worker_request_id,
            provider_request_id=(str(data["provider_request_id"])
                                  if data.get("provider_request_id") else None),
        )

    # ------------------------------------------------------------------
    # Artifact 解包
    # ------------------------------------------------------------------

    @staticmethod
    def extract_artifact_segments(
        response: WorkerSynthesizeBatchResponse,
    ) -> dict[str, bytes]:
        """从 batch 响应里提取并校验 ``audio_path → wav_bytes`` 映射。

        Phase 1 mock 路径：inline_base64 直接解 zip。
        Phase 3 真实路径：会从 ``package.download_url`` 拉 zip（待实现）。

        返回值用 ``audio_path`` 做 key，与每个 ``WorkerSegmentResult.audio_path``
        对齐，方便调用方按 segment id 路由到 ``TTSResult.audio_path``。

        三层 sha256 完整性校验（plan §POST /cosyvoice/synthesize-batch
        响应里 package 和每 segment 都有 sha256 字段）：

        1. **Package 级**：``sha256(zip_bytes) == response.package.sha256``。
           防 zip 包整体被传输层篡改。
        2. **Manifest 级**：每个 ``response.segments[i].audio_path`` 必须
           出现在 zip 内。防 worker 端 zip 漏文件。
        3. **Segment 级**：每条 wav bytes 的 sha256 必须与
           ``response.segments[i].sha256`` 匹配。防单个文件被替换或损坏。

        任一失败抛 ``WorkerArtifactIntegrityError``。
        """
        package = response.package
        if package.kind != "inline_base64":
            # Phase 3 hook
            raise NotImplementedError(
                f"package.kind={package.kind!r} not supported in Phase 1; "
                f"only inline_base64 implemented (Phase 3 will add zip download)"
            )

        payload = package.inline_bytes or b""
        if not payload:
            # 空 batch 路径已经在 dataclass 层禁止 segments=()；这里 payload
            # 空只可能是 worker bug 或被截断。
            if response.segments:
                raise WorkerArtifactIntegrityError(
                    f"package has 0 bytes but manifest lists {len(response.segments)} segment(s)"
                )
            return {}

        # 1. Package-level sha256
        actual_pkg_sha = hashlib.sha256(payload).hexdigest()
        if actual_pkg_sha != package.sha256:
            raise WorkerArtifactIntegrityError(
                f"package sha256 mismatch: manifest={package.sha256!r} actual={actual_pkg_sha!r}; "
                f"artifact bytes were modified in transit or worker zip logic drifted"
            )

        # 解 zip
        buf = io.BytesIO(payload)
        with zipfile.ZipFile(buf, "r") as zf:
            zip_names = set(zf.namelist())
            extracted: dict[str, bytes] = {name: zf.read(name) for name in zip_names}

        # 2. Manifest-level: 每段 audio_path 必须在 zip 内
        missing = [
            seg.audio_path for seg in response.segments if seg.audio_path not in extracted
        ]
        if missing:
            raise WorkerArtifactIntegrityError(
                f"segments missing from zip: {missing}; manifest says {len(response.segments)} segments"
            )

        # 3. Segment-level sha256
        for seg in response.segments:
            wav_bytes = extracted[seg.audio_path]
            actual_seg_sha = hashlib.sha256(wav_bytes).hexdigest()
            if actual_seg_sha != seg.sha256:
                raise WorkerArtifactIntegrityError(
                    f"segment {seg.segment_id} ({seg.audio_path!r}) sha256 mismatch: "
                    f"manifest={seg.sha256!r} actual={actual_seg_sha!r}"
                )

        # 只返回 manifest 里列出的 segments；zip 内多余文件不暴露给调用方
        return {seg.audio_path: extracted[seg.audio_path] for seg in response.segments}

    # ------------------------------------------------------------------
    # Internal: HTTP 发送 + retry
    # ------------------------------------------------------------------

    def _send_request(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        job_id: str,
        sign_request: bool,
        max_attempts: int,
    ) -> httpx.Response:
        """带 retry 和退避的请求发送。

        retry 策略（CLAUDE.md "batch / loop / retry 里无上限调用付费 API"）：

        - 仅对网络错误 / 5xx 重试。
        - 4xx（含 401 签名拒绝）立刻抛，避免误把 signature mismatch
          当临时错误重试。
        - retry 次数固定 ``max_attempts`` 上限，永远不进入无限循环。
        """
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                response = self._http.request(
                    method=method,
                    url=path,
                    content=body if body else None,
                    headers=self._build_headers(
                        method=method,
                        path=path,
                        body=body,
                        job_id=job_id,
                        sign_request=sign_request,
                    ),
                )
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "[mainland-worker-client] %s %s network error attempt=%d/%d: %s",
                    method, path, attempt + 1, max_attempts, exc,
                )
                if attempt + 1 >= max_attempts:
                    break
                self._sleep_backoff(attempt)
                continue

            # 401 立刻抛，不重试
            if response.status_code == 401:
                self._raise_worker_error(response, _safe_json(response))

            # 5xx 触发 retry
            if response.status_code >= 500:
                last_exc = WorkerError(
                    f"worker returned {response.status_code}",
                    code="worker_5xx",
                    http_status=response.status_code,
                    retryable=True,
                )
                logger.warning(
                    "[mainland-worker-client] %s %s server error attempt=%d/%d status=%d",
                    method, path, attempt + 1, max_attempts, response.status_code,
                )
                if attempt + 1 >= max_attempts:
                    break
                self._sleep_backoff(attempt)
                continue

            # 4xx 其它（400 etc.）：直接抛业务错误，不重试
            if response.status_code >= 400:
                self._raise_worker_error(response, _safe_json(response))

            return response

        # 所有 retry 用完
        if isinstance(last_exc, WorkerError):
            raise last_exc
        raise WorkerNetworkError(
            f"{method} {path} failed after {max_attempts} attempts: {last_exc!r}"
        ) from last_exc

    def _build_headers(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        job_id: str,
        sign_request: bool,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
        }
        if not sign_request:
            return headers

        ts = int(time.time())
        nonce = uuid.uuid4().hex
        material = SignatureMaterial(
            method=method,
            path=path,
            timestamp=ts,
            nonce=nonce,
            key_id=self._credentials.key_id,
            body=body,
        )
        signature = sign(material, self._credentials.secret)

        headers[HEADER_KEY_ID] = self._credentials.key_id
        headers[HEADER_TIMESTAMP] = str(ts)
        headers[HEADER_NONCE] = nonce
        headers[HEADER_SIGNATURE] = signature
        if job_id:
            headers[HEADER_JOB_ID] = job_id
        return headers

    def _sleep_backoff(self, attempt_index: int) -> None:
        if attempt_index < len(RETRY_BACKOFF_SECONDS):
            delay = RETRY_BACKOFF_SECONDS[attempt_index]
        else:
            delay = RETRY_BACKOFF_SECONDS[-1]
        time.sleep(delay)

    @staticmethod
    def _raise_worker_error(response: httpx.Response, data: dict[str, Any] | None) -> None:
        data = data or {}
        err = data.get("error") or {}
        code = str(err.get("code") or "worker_error")
        message = str(err.get("message") or data.get("message") or f"HTTP {response.status_code}")
        retryable = bool(err.get("retryable", False))

        if response.status_code == 401:
            raise WorkerSignatureRejectedError(
                message,
                code=code,
                http_status=response.status_code,
                retryable=False,
            )
        raise WorkerError(
            message,
            code=code,
            http_status=response.status_code,
            retryable=retryable,
        )


def _safe_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # FastAPI HTTPException(detail={...}) 会包成 {"detail": {...}}
    if "detail" in data and isinstance(data["detail"], dict) and "error" not in data:
        return {"error": data["detail"]}
    return data
