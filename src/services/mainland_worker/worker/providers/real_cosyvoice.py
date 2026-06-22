"""Real CosyVoice provider for the mainland DashScope worker.

This module is the only place in ``services.mainland_worker`` that may import
the DashScope SDK. Tests monkeypatch the SDK modules and never make live
network calls.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import httpx

from services.mainland_worker.billing_chars import billing_character_count
from services.mainland_worker.silent_wav import wav_duration_ms
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


logger = logging.getLogger(__name__)


MAX_SAMPLE_BYTES = 1 * 1024 * 1024
QUERY_POLL_INTERVAL_S = 1.0
QUERY_MAX_POLLS = 60
DEFAULT_AUDIO_FORMAT_NAME = "WAV_16000HZ_MONO_16BIT"
MAINLAND_HTTP_API_URL = "https://dashscope.aliyuncs.com/api/v1"
MAINLAND_WEBSOCKET_API_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

# Codex 2026-05-25 决策对齐 DashScope ``max_prompt_audio_length``：
# 官方默认 10 秒；Phase 4.1 显式传 30 秒让服务端用更长参考音频，相似度
# 更高。与 Gateway 端 ``audio_processor.DEFAULT_TARGET_DURATION_S=30`` 对齐。
DEFAULT_MAX_PROMPT_AUDIO_LENGTH_S = 30.0

_DASHSCOPE_LOCK = threading.RLock()


@contextmanager
def _dashscope_mainland_context(api_key: str) -> Iterator[Any]:
    """Bind DashScope SDK globals to mainland endpoints and restore them.

    The DashScope Python SDK uses module-level globals for api key and endpoint
    URL. Keep all live calls behind one lock so concurrent requests cannot mix
    credentials or accidentally drift to the international endpoint.
    """
    import dashscope

    with _DASHSCOPE_LOCK:
        prev_api_key = getattr(dashscope, "api_key", None)
        prev_http_url = getattr(dashscope, "base_http_api_url", None)
        prev_ws_url = getattr(dashscope, "base_websocket_api_url", None)

        dashscope.api_key = api_key
        dashscope.base_http_api_url = MAINLAND_HTTP_API_URL
        dashscope.base_websocket_api_url = MAINLAND_WEBSOCKET_API_URL
        try:
            yield dashscope
        finally:
            dashscope.api_key = prev_api_key
            dashscope.base_http_api_url = prev_http_url
            dashscope.base_websocket_api_url = prev_ws_url


def _retryable_keywords(text: str) -> bool:
    """Return True only for transient DashScope-looking failures."""
    lowered = text.lower()
    for kw in (
        "timeout",
        "503",
        "502",
        "504",
        "429",
        "rate limit",
        "throttl",
        "temporarily unavailable",
        "service unavailable",
    ):
        if kw in lowered:
            return True
    return False


class RealCosyvoiceProvider(CosyvoiceProvider):
    """DashScope-backed CosyVoice clone + TTS + delete provider."""

    def __init__(
        self,
        api_key: str,
        *,
        max_sample_bytes: int = MAX_SAMPLE_BYTES,
        query_poll_interval_s: float = QUERY_POLL_INTERVAL_S,
        query_max_polls: int = QUERY_MAX_POLLS,
        language_hints: tuple[str, ...] = ("zh",),
        max_prompt_audio_length_s: float = DEFAULT_MAX_PROMPT_AUDIO_LENGTH_S,
    ) -> None:
        if not api_key:
            raise ValueError(
                "RealCosyvoiceProvider requires non-empty api_key "
                "(set DASHSCOPE_API_KEY before switching WORKER_MODE=live)"
            )
        self._api_key = api_key
        self._max_sample_bytes = max_sample_bytes
        self._poll_interval = query_poll_interval_s
        self._max_polls = query_max_polls
        self._language_hints = list(language_hints)
        self._max_prompt_audio_length_s = max_prompt_audio_length_s

    def clone(self, req: WorkerCloneRequest) -> CloneOutcome:
        self._validate_sample_size(req.sample.url)

        with _dashscope_mainland_context(self._api_key):
            from dashscope.audio.tts_v2 import VoiceEnrollmentService

            service = VoiceEnrollmentService()
            prefix = self._sanitize_prefix(req.speaker_id)

            try:
                voice_id = service.create_voice(
                    target_model=req.target_model,
                    prefix=prefix,
                    url=req.sample.url,
                    language_hints=self._language_hints,
                    max_prompt_audio_length=self._max_prompt_audio_length_s,
                )
            except Exception as exc:
                msg = f"create_voice failed: {exc}"
                logger.warning("[real_cosyvoice] %s", msg)
                raise ProviderError(
                    msg,
                    code="create_voice_failed",
                    retryable=_retryable_keywords(str(exc)),
                ) from exc

            # Phase 4.0b §A: provider_request_id 取 create_voice 的 last_request_id
            # （不是后续 query_voice 的）—— 这是 clone 计费记录的主锚点，
            # 与阿里云后台账单 request_id 列对账（plan §Phase 4.0a 决策）
            provider_request_id = self._safe_get_last_request_id(service)

            if not voice_id:
                raise ProviderError(
                    "create_voice returned empty voice_id",
                    code="create_voice_empty",
                    retryable=False,
                )

            for poll_idx in range(self._max_polls):
                try:
                    status = service.query_voice(voice_id)
                except Exception as exc:
                    msg = f"query_voice failed at poll #{poll_idx}: {exc}"
                    logger.warning("[real_cosyvoice] %s", msg)
                    raise ProviderError(
                        msg,
                        code="query_voice_failed",
                        retryable=_retryable_keywords(str(exc)),
                    ) from exc

                if self._is_voice_ready(status):
                    logger.info(
                        "[real_cosyvoice] voice %s ready after %d poll(s) "
                        "(create request id=%s)",
                        voice_id,
                        poll_idx + 1,
                        provider_request_id,
                    )
                    return CloneOutcome(
                        voice_id=voice_id,
                        provider_request_id=provider_request_id,
                    )

                time.sleep(self._poll_interval)

        raise ProviderError(
            f"voice {voice_id} did not become ready within {self._max_polls} polls "
            f"({self._max_polls * self._poll_interval:.0f}s)",
            code="query_voice_timeout",
            retryable=False,
        )

    def synthesize_segment(
        self,
        seg: WorkerSegmentRequest,
        *,
        target_model: str,
    ) -> SegmentSynthesisOutcome:
        if not seg.text:
            raise ProviderError("empty text", code="empty_text", retryable=False)

        # Phase 4.0b §B：billed_chars 用 billing_character_count（plan §Phase 4.0a
        # 决策路径 B：本地实现）。SDK live response 不暴露 usage.characters，
        # 所以无法走路径 A。
        billed_chars = billing_character_count(seg.text)

        provider_request_id: str | None = None
        with _dashscope_mainland_context(self._api_key):
            from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

            audio_format = getattr(AudioFormat, DEFAULT_AUDIO_FORMAT_NAME)
            synthesizer = SpeechSynthesizer(
                model=target_model,
                voice=seg.voice_id,
                format=audio_format,
                speech_rate=seg.speech_rate,
            )
            try:
                try:
                    audio = synthesizer.call(seg.text)
                except Exception as exc:
                    msg = f"synthesize failed for segment {seg.segment_id}: {exc}"
                    logger.warning("[real_cosyvoice] %s", msg)
                    raise ProviderError(
                        msg,
                        code="synthesize_failed",
                        retryable=_retryable_keywords(str(exc)),
                    ) from exc
                # Phase 4.0b §A: provider_request_id 取 SpeechSynthesizer 的
                # last_request_id（plan §Phase 4.0a Observation Log 路径 A）
                provider_request_id = self._safe_get_synth_request_id(synthesizer)
            finally:
                self._close_synthesizer(synthesizer)

        if not audio or not isinstance(audio, (bytes, bytearray)):
            raise ProviderError(
                f"synthesize returned unexpected: {type(audio).__name__}",
                code="synthesize_empty",
                retryable=True,
            )

        audio_bytes = bytes(audio)
        duration_ms = wav_duration_ms(audio_bytes)
        if duration_ms <= 0:
            raise ProviderError(
                "synthesized audio has zero duration",
                code="zero_duration_audio",
                retryable=False,
            )

        return SegmentSynthesisOutcome(
            audio_bytes=audio_bytes,
            duration_ms=duration_ms,
            billed_chars=billed_chars,
            provider_request_id=provider_request_id,
        )

    def delete_voice(self, voice_id: str) -> DeleteOutcome:
        if not voice_id:
            raise ProviderError("empty voice_id", code="invalid_input", retryable=False)

        with _dashscope_mainland_context(self._api_key):
            from dashscope.audio.tts_v2 import VoiceEnrollmentService

            service = VoiceEnrollmentService()
            try:
                service.delete_voice(voice_id)
            except Exception as exc:
                msg = f"delete_voice failed: {exc}"
                logger.warning("[real_cosyvoice] %s", msg)
                raise ProviderError(
                    msg,
                    code="delete_voice_failed",
                    retryable=_retryable_keywords(str(exc)),
                ) from exc
            # Phase 4.0b §A: delete 后取 service.get_last_request_id()
            # 作为 delete audit 锚点
            provider_request_id = self._safe_get_last_request_id(service)
        return DeleteOutcome(provider_request_id=provider_request_id)

    def _validate_sample_size(self, url: str) -> None:
        """Validate sample reachability/size using GET, not HEAD.

        Aliyun OSS presigned URLs are signed for a specific HTTP method. The
        gateway gives DashScope a GET URL, so using HEAD here returns 403 even
        though the URL is valid for the provider. Use a one-byte Range GET
        instead; if the server ignores Range, fall back to the response content
        length.
        """
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as c:
                resp = c.get(url, headers={"Range": "bytes=0-0"})
        except Exception as exc:
            raise ProviderError(
                f"sample GET probe failed for url: {exc}",
                code="sample_get_failed",
                retryable=False,
            ) from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"sample URL GET probe returned {resp.status_code}",
                code="sample_url_unreachable",
                retryable=False,
            )

        cl = 0
        content_range = resp.headers.get("content-range", "")
        if "/" in content_range:
            try:
                cl = int(content_range.rsplit("/", 1)[1])
            except ValueError:
                cl = 0
        if cl <= 0:
            cl_str = resp.headers.get("content-length", "0")
            try:
                cl = int(cl_str)
            except ValueError:
                cl = 0
        if cl <= 0:
            cl = len(resp.content or b"")

        if cl > self._max_sample_bytes:
            raise ProviderError(
                f"sample too large: {cl} bytes > {self._max_sample_bytes} bytes "
                "(Phase -1 observed DashScope InputDownloadFailed above 1 MB)",
                code="sample_too_large",
                retryable=False,
            )

    @staticmethod
    def _sanitize_prefix(speaker_id: str) -> str:
        base = "".join(ch for ch in (speaker_id or "") if ch.isalnum())
        if not base:
            base = "spk"
        return f"avt{base[:5]}"[:10]

    @staticmethod
    def _is_voice_ready(status: Any) -> bool:
        text = repr(status).upper()
        if "OK" in text and "FAIL" not in text:
            return True
        return False

    @staticmethod
    def _safe_get_last_request_id(service: Any) -> str | None:
        """Try ``service.get_last_request_id()``; fall back to ``None``.

        plan §Phase 4.0a Observation Log 路径 A：DashScope SDK 在
        ``VoiceEnrollmentService`` 上暴露 ``get_last_request_id()``。本函数
        包一层 defensive try/except，让 SDK 行为变化（升级 / 替换）不
        crashing provider 调用——拿不到 request id 时只损失一条 audit
        锚点，业务路径仍走完。
        """
        getter = getattr(service, "get_last_request_id", None)
        if not callable(getter):
            return None
        try:
            rid = getter()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("[real_cosyvoice] get_last_request_id raised: %s", exc)
            return None
        if rid is None:
            return None
        text = str(rid).strip()
        return text or None

    @staticmethod
    def _safe_get_synth_request_id(synthesizer: Any) -> str | None:
        """Try ``synthesizer.get_last_request_id()`` first, then ``.last_request_id``.

        plan §Phase 4.0a Observation Log：``SpeechSynthesizer`` 同时暴露
        method 和 property 两种接口；先试 method，失败再试 property。
        """
        getter = getattr(synthesizer, "get_last_request_id", None)
        if callable(getter):
            try:
                rid = getter()
                if rid:
                    return str(rid).strip() or None
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("[real_cosyvoice] synth.get_last_request_id raised: %s", exc)

        rid = getattr(synthesizer, "last_request_id", None)
        if rid is None:
            return None
        text = str(rid).strip()
        return text or None

    @staticmethod
    def _close_synthesizer(synthesizer: Any) -> None:
        ws = getattr(synthesizer, "ws", None)
        if ws is not None:
            try:
                setattr(ws, "keep_running", False)
            except Exception:
                pass
            ws_close = getattr(ws, "close", None)
            if callable(ws_close):
                try:
                    ws_close()
                except Exception:
                    pass
        close_fn = getattr(synthesizer, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass
