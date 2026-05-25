"""Worker provider 协议 — clone / synthesize / delete（Phase 4.0b 扩展）。

设计原则：

- ``clone`` / ``synthesize_segment`` / ``delete_voice`` 是**同步**接口，
  让 mock 和 real provider 共享相同的调用形状。real provider 内部
  如果用异步 SDK，自己起 event loop 包装。
- 返回 ``CloneOutcome`` / ``SegmentSynthesisOutcome`` / ``DeleteOutcome``
  dataclass（Phase 4.0b 引入）替代旧的 tuple 返回值——目的是把
  ``provider_request_id`` 这种新增字段以兼容方式加入，避免每次扩 schema
  都改 tuple 形状（caller 改一处）。
- 异常类型仅两类：``ProviderError``（业务错误，可 retry / 不可 retry）
  和 stdlib 异常（视为 unknown，HTTP 500）。Phase 4 RealCosyvoiceProvider
  再细分错误码。

付费 API 硬约束：

- 这个 protocol 不暗藏 retry。worker handler 调用 provider **每个用户
  请求只调一次**。retry 由 US client 端的 ``MainlandWorkerClient`` 收口。

Phase 4.0b 新增 outcome dataclass（plan §Phase 4.0b §A）：

- ``CloneOutcome``: ``voice_id`` + ``provider_request_id`` (nullable)
- ``SegmentSynthesisOutcome``: ``audio_bytes / duration_ms / billed_chars
  / provider_request_id``
- ``DeleteOutcome``: ``provider_request_id`` (nullable)

``provider_request_id`` 即 DashScope SDK ``get_last_request_id()`` /
``synthesizer.last_request_id`` 返回的 UUID。mock 模式下留 ``None``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.mainland_worker.types import (
    WorkerCloneRequest,
    WorkerSegmentRequest,
)


class ProviderError(Exception):
    """Provider 调用失败。"""

    def __init__(self, message: str, *, code: str = "provider_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Outcome dataclasses（Phase 4.0b §A 引入）
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CloneOutcome:
    """``provider.clone()`` 的返回结构。

    ``provider_request_id`` 可空：mock 模式下 None；real 模式下从
    DashScope SDK ``service.get_last_request_id()`` 取（plan §Phase 4.0a
    Observation Log 路径 A）。
    """
    voice_id: str
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class SegmentSynthesisOutcome:
    """``provider.synthesize_segment()`` 的返回结构。

    Phase 4.0b 把旧的 ``tuple[bytes, int, int]`` 升级为带 ``provider_request_id``
    的 dataclass，方便未来加字段不破坏 caller。
    """
    audio_bytes: bytes
    duration_ms: int
    billed_chars: int
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    """``provider.delete_voice()`` 的返回结构。"""
    provider_request_id: str | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class CosyvoiceProvider(Protocol):
    """CosyVoice clone + TTS + delete 抽象。"""

    def clone(self, req: WorkerCloneRequest) -> CloneOutcome:
        """从样本创建自定义音色，返回 ``CloneOutcome``。

        实现要求：

        - 创建成功后立即可用（mock：deterministic；real：阻塞轮询到 OK）
        - 失败抛 ``ProviderError``
        - 不做内部 retry — 上层 client 已经规定 clone 最多 1 次。
        - real 模式：``provider_request_id`` 取 ``create_voice``（不是 query）
          后的 ``service.get_last_request_id()``，作为对账主锚点
        """
        ...

    def synthesize_segment(
        self,
        seg: WorkerSegmentRequest,
        *,
        target_model: str,
    ) -> SegmentSynthesisOutcome:
        """合成单段，返回 ``SegmentSynthesisOutcome``。"""
        ...

    def delete_voice(self, voice_id: str) -> DeleteOutcome:
        """删除自定义音色。失败抛 ``ProviderError``。"""
        ...
