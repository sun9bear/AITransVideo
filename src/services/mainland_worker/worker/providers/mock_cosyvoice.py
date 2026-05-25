"""Mock CosyVoice provider — Phase 1 默认。

特性：

- **Deterministic**：相同输入 → 相同 voice_id / wav_bytes。让 e2e 测试
  可以精确断言而无 flake。
- **不调外部网络 / 不 import dashscope**：付费 API 硬约束 + AGENTS.md
  "tests, local development, and default paths, prefer mocks/stubs/fakes
  over live external services"。
- **不内部 retry**：worker provider 调用方（handler）每个请求只调
  provider 一次；任何 retry 都由 US client 端收口（plan §Retry）。

voice_id 形状：``mock_cosy_<sha256(speaker_id + target_model + sample_sha256)[:16]>``。

duration_ms 估算规则：

- 中文（CJK 字符为主）：约 0.27 秒/字（≈ 220 字/分钟）
- 其他：约 0.06 秒/字符（≈ 1000 字符/分钟）
- 至少 1000 ms，避免极短文本被估成 0 让对齐崩
- ``speech_rate`` 直接除（speech_rate=2.0 → 时长减半）
- 最大 60 秒，防 mock 路径上不小心被传超长文本

billed_chars：使用 ``billing_character_count(text)``，保持与 Phase 4.0a
实测后的 DashScope API 字符口径一致（CJK Unified Ideographs 计 2，其它
字符计 1）。
"""
from __future__ import annotations

import hashlib
import unicodedata

from services.mainland_worker.billing_chars import billing_character_count
from services.mainland_worker.silent_wav import generate_silent_wav
from services.mainland_worker.types import (
    WorkerCloneRequest,
    WorkerSegmentRequest,
)
from services.mainland_worker.worker.providers.base import (
    CloneOutcome,
    CosyvoiceProvider,
    DeleteOutcome,
    ProviderError,
    SegmentSynthesisOutcome,
)


# ---- 时长估算常量 ----
_SEC_PER_CJK = 0.27  # ~ 220 chars/min
_SEC_PER_OTHER = 0.06  # ~ 1000 chars/min
_MIN_DURATION_MS = 1000
_MAX_DURATION_MS = 60_000


def _is_cjk(ch: str) -> bool:
    """简易 CJK 判断 — 用 unicodedata.east_asian_width。"""
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _estimate_duration_ms(text: str, speech_rate: float) -> int:
    """根据文本估算合成时长。"""
    if not text:
        return _MIN_DURATION_MS

    sec = 0.0
    for ch in text:
        if ch.isspace():
            continue
        sec += _SEC_PER_CJK if _is_cjk(ch) else _SEC_PER_OTHER

    if speech_rate <= 0:
        speech_rate = 1.0
    sec /= speech_rate

    ms = int(sec * 1000)
    return max(_MIN_DURATION_MS, min(_MAX_DURATION_MS, ms))


class MockCosyvoiceProvider(CosyvoiceProvider):
    """Mock 实现 — deterministic、本地、零外部依赖。

    Phase 4.0b：返 ``CloneOutcome`` / ``SegmentSynthesisOutcome`` /
    ``DeleteOutcome``，``provider_request_id`` 全部 ``None``（mock 路径
    无真实 SDK，无 request id）。
    """

    def clone(self, req: WorkerCloneRequest) -> CloneOutcome:
        # 拒绝未确认 consent — plan §Clone 说"用户显式确认"是硬规则。
        if not req.consent.voice_clone_confirmed:
            raise ProviderError(
                f"clone refused: consent.voice_clone_confirmed=False for speaker {req.speaker_id!r}",
                code="consent_required",
                retryable=False,
            )

        seed = f"{req.speaker_id}|{req.target_model}|{req.sample.sha256}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        return CloneOutcome(
            voice_id=f"mock_cosy_{digest}",
            provider_request_id=None,
        )

    def synthesize_segment(
        self,
        seg: WorkerSegmentRequest,
        *,
        target_model: str,
    ) -> SegmentSynthesisOutcome:
        if not seg.text:
            raise ProviderError(
                f"empty text for segment {seg.segment_id}",
                code="empty_text",
                retryable=False,
            )

        duration_ms = _estimate_duration_ms(seg.text, seg.speech_rate)
        wav_bytes = generate_silent_wav(duration_ms)
        # Phase 4.0b §B：billed_chars 用 billing_character_count（CJK = 2），
        # 不再用 len(text) 低估中文（plan §Phase 4.0a Observation Log 路径 B）
        billed_chars = billing_character_count(seg.text)
        return SegmentSynthesisOutcome(
            audio_bytes=wav_bytes,
            duration_ms=duration_ms,
            billed_chars=billed_chars,
            provider_request_id=None,
        )

    def delete_voice(self, voice_id: str) -> DeleteOutcome:
        # Mock：永远 "成功"。Real provider 失败时抛 ProviderError，
        # worker handler 才能写 retryable tombstone（plan §Delete）。
        if not voice_id:
            raise ProviderError("empty voice_id", code="invalid_input", retryable=False)
        return DeleteOutcome(provider_request_id=None)
