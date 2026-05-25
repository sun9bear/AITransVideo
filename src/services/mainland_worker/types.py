"""Mainland Worker 请求/响应数据合约。

这些 dataclass 同时被 US 主机的 client 和武汉 worker server 使用，
是 worker API 的运行时 single source of truth。请求/响应 JSON 的形状
直接 ``asdict()`` 出来，与 plan §Worker API 章节里的 JSON 示例完全
对应。

设计约束：

- 所有字段都是简单值（str / int / float / bool / dict / list），
  没有 framework-specific 类型（不依赖 pydantic / fastapi），
  这样 worker 端可以直接用 dict 校验、不强制把 dataclass 接进 FastAPI
  的 request body parser。
- 不引入版本化（``v1`` 字段等）。Phase 1 是 mock；Phase 2+ 真实落地
  时再决定要不要 schema 版本号。
- ``WorkerSegmentRequest.text_hash`` 是 plan §POST /cosyvoice/synthesize-batch
  规范字段：``sha256(text.encode("utf-8")).hexdigest()``。Worker 会
  重新计算并校验，请求里传的 text_hash 仅作为幂等键存档使用。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 决策字段：voice metadata 中关于 worker 路由的字段
# ---------------------------------------------------------------------------

REGION_CONSTRAINT_OVERSEAS_OK = "overseas_ok"
REGION_CONSTRAINT_MAINLAND_ONLY = "mainland_only"

PLATFORM_DASHSCOPE_MAINLAND = "dashscope_mainland"

# Provider 名（plan §分发决策字段）
PROVIDER_COSYVOICE_VOICE_CLONE = "cosyvoice_voice_clone"
TTS_PROVIDER_COSYVOICE = "cosyvoice"
WORKER_PROVIDER_COSYVOICE = "cosyvoice"
WORKER_REGION_CN_WUHAN = "cn-wuhan"


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorkerCloneSample:
    """Clone 样本来源。

    plan §Clone 接受 ``kind="download_url"`` 形式：worker 从 US 主机
    或预签名 URL 下载样本。``sha256`` 用于校验下载完整性。
    """
    kind: str  # 当前仅支持 "download_url"
    url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class WorkerCloneConsent:
    """用户授权确认。

    Phase 1 mock 阶段也保留这个字段，让 client/handler 路径与 Phase 2+
    完全一致；可以通过守卫测试断言"未带 consent.voice_clone_confirmed=true
    的请求被拒绝"。
    """
    voice_clone_confirmed: bool
    confirmed_at: str  # ISO8601 UTC


@dataclass(frozen=True, slots=True)
class WorkerCloneRequest:
    """``POST /cosyvoice/clone`` 请求体。"""
    job_id: str
    user_id: str
    speaker_id: str
    speaker_name: str
    target_model: str
    sample: WorkerCloneSample
    source_segments: tuple[int, ...]
    consent: WorkerCloneConsent

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "speaker_id": self.speaker_id,
            "speaker_name": self.speaker_name,
            "target_model": self.target_model,
            "sample": {
                "kind": self.sample.kind,
                "url": self.sample.url,
                "sha256": self.sample.sha256,
            },
            "source_segments": list(self.source_segments),
            "consent": {
                "voice_clone_confirmed": self.consent.voice_clone_confirmed,
                "confirmed_at": self.consent.confirmed_at,
            },
        }


@dataclass(frozen=True, slots=True)
class WorkerCloneResponse:
    """``POST /cosyvoice/clone`` 响应体。

    Phase 4.0b §A 增加 audit 锚点字段：

    - ``worker_request_id``: 必填，worker 端生成 UUID。用于 Gateway audit
      把 client 侧 / worker 侧 / DashScope 侧三段串起来
    - ``provider_request_id``: nullable，real 模式下从 DashScope
      ``service.get_last_request_id()`` 取（create_voice 后立即取，
      不是后续 query_voice）；mock 模式留 None
    """
    ok: bool
    voice_id: str
    provider: str  # "cosyvoice_voice_clone"
    tts_provider: str  # "cosyvoice"
    target_model: str
    region_constraint: str  # "mainland_only"
    requires_worker: bool  # True
    platform: str  # "dashscope_mainland"
    sample_sha256: str
    created_at: str  # ISO8601 UTC
    worker_request_id: str = ""  # Phase 4.0b: 必填（__post_init__ 校验非空）
    provider_request_id: str | None = None  # Phase 4.0b: nullable

    def __post_init__(self) -> None:
        # Codex 2026-05-25 P1 finding：``worker_request_id`` 是审计主锚点，
        # 不能因为默认值 ``""`` 让 worker 漏字段静默通过。fail-closed。
        if not self.worker_request_id or not self.worker_request_id.strip():
            raise ValueError(
                "WorkerCloneResponse.worker_request_id must be non-empty "
                "(Phase 4.0b §A 必填审计主锚点)"
            )


# ---------------------------------------------------------------------------
# Synthesize batch
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorkerSegmentRequest:
    """合成请求中的单个 segment。

    ``text_hash`` 是规范字段，由 client 端计算 ``sha256(text.encode("utf-8")).hexdigest()``。
    Worker 端会重新计算并校验一致性 — 不一致直接拒绝（防止 client/server
    在 normalize 规则上偷偷漂移导致幂等键失效）。
    """
    segment_id: int
    speaker_id: str
    voice_id: str
    text: str
    speech_rate: float = 1.0
    target_duration_ms: int | None = None
    text_hash: str = ""

    def to_dict(self) -> dict:
        d = {
            "segment_id": self.segment_id,
            "speaker_id": self.speaker_id,
            "voice_id": self.voice_id,
            "text": self.text,
            "speech_rate": self.speech_rate,
            "text_hash": self.text_hash or compute_text_hash(self.text),
        }
        if self.target_duration_ms is not None:
            d["target_duration_ms"] = self.target_duration_ms
        return d


@dataclass(frozen=True, slots=True)
class WorkerSynthesizeBatchRequest:
    """``POST /cosyvoice/synthesize-batch`` 请求体。

    ``len(segments) == 1`` 必须被支持（plan §Studio Post-Edit / Regenerate TTS）：
    Studio 单段 regenerate-tts 也走这个 endpoint，不开 ``/synthesize-one``。
    """
    job_id: str
    target_model: str
    audio_format: str  # 当前仅 "wav"
    segments: tuple[WorkerSegmentRequest, ...]

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("segments must not be empty")

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "target_model": self.target_model,
            "audio_format": self.audio_format,
            "segments": [s.to_dict() for s in self.segments],
        }


@dataclass(frozen=True, slots=True)
class WorkerSegmentResult:
    """合成响应中的单个 segment 结果。

    Phase 4.0b §A 增加 ``provider_request_id``: nullable，每段一个
    （DashScope ``SpeechSynthesizer.last_request_id`` per call）。mock 留 None。
    """
    segment_id: int
    speaker_id: str
    voice_id: str
    audio_path: str  # package 内相对路径
    duration_ms: int
    billed_chars: int
    sha256: str
    provider_request_id: str | None = None  # Phase 4.0b: nullable


@dataclass(frozen=True, slots=True)
class WorkerArtifactPackage:
    """合成结果 artifact package 元数据。

    Phase 1 mock 路径用 ``kind="inline_base64"`` 并填充 ``inline_bytes``
    （base64 解码后的 zip bytes）；Phase 3 切换 ``kind="zip"`` + 真实
    ``download_url`` 后，``inline_bytes`` 留 None，由 client 从 URL 拉取。
    """
    kind: str  # "zip" | "inline_base64"
    download_url: str
    sha256: str
    expires_at: str  # ISO8601 UTC
    inline_bytes: bytes | None = None


@dataclass(frozen=True, slots=True)
class WorkerSynthesizeBatchResponse:
    """``POST /cosyvoice/synthesize-batch`` 响应体。

    Phase 4.0b §A 增加 ``worker_request_id``: batch 顶层一个必填 UUID。
    segment 级 ``provider_request_id`` 在每个 ``WorkerSegmentResult`` 里。
    """
    ok: bool
    job_id: str
    target_model: str
    segments: tuple[WorkerSegmentResult, ...]
    package: WorkerArtifactPackage
    worker_request_id: str = ""  # Phase 4.0b: 必填（__post_init__ 校验非空）

    def __post_init__(self) -> None:
        # Codex 2026-05-25 P1 finding：fail-closed，不允许 worker 漏字段
        if not self.worker_request_id or not self.worker_request_id.strip():
            raise ValueError(
                "WorkerSynthesizeBatchResponse.worker_request_id must be non-empty "
                "(Phase 4.0b §A 必填审计主锚点)"
            )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorkerDeleteVoiceRequest:
    """``DELETE /cosyvoice/voices/{voice_id}`` 请求体。"""
    job_id: str
    user_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class WorkerDeleteVoiceResponse:
    """``DELETE /cosyvoice/voices/{voice_id}`` 响应体。

    Phase 4.0b §A 增加 audit 锚点字段（plan §Phase 4.0b §A）：
    ``worker_request_id`` 必填、``provider_request_id`` nullable。
    """
    ok: bool
    voice_id: str
    deleted_at: str  # ISO8601 UTC
    worker_request_id: str = ""  # Phase 4.0b: 必填（__post_init__ 校验非空）
    provider_request_id: str | None = None  # Phase 4.0b: nullable

    def __post_init__(self) -> None:
        # Codex 2026-05-25 P1 finding：fail-closed
        if not self.worker_request_id or not self.worker_request_id.strip():
            raise ValueError(
                "WorkerDeleteVoiceResponse.worker_request_id must be non-empty "
                "(Phase 4.0b §A 必填审计主锚点)"
            )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorkerProviderHealth:
    """单 provider 的健康状态。"""
    configured: bool
    mode: str  # "mock" | "live"


@dataclass(frozen=True, slots=True)
class WorkerHealthResponse:
    """``GET /healthz`` 响应体。"""
    ok: bool
    worker: str  # "aivideotrans-mainland-worker"
    region: str  # "cn-wuhan"
    providers: dict[str, WorkerProviderHealth] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def compute_text_hash(text: str) -> str:
    """plan §POST /cosyvoice/synthesize-batch `text_hash` 规范。

    ``sha256(text.encode("utf-8")).hexdigest()``，不做 Unicode normalize，
    大小写敏感。Client 和 worker 端必须用这同一个函数算 hash，否则
    幂等键失效。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
