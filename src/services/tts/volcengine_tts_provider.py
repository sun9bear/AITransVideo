"""VolcEngine (火山引擎) 豆包 TTS provider — V3 HTTP Chunked API.

Supports both **豆包 1.0** and **豆包 2.0** via the same V3 endpoint.
The caller selects the model version through the ``resource_id`` parameter:

* ``seed-tts-1.0`` — 100+ ``_moon_bigtts`` voices, ¥5/万字符
* ``seed-tts-2.0`` — 20+ ``_uranus_bigtts`` voices, ¥3/万字符, emotion directives

Three independent concepts drive each request:

1. **resource_id** — written to the ``X-Api-Resource-Id`` header;
   selects the underlying model (1.0 vs 2.0).
2. **req_params.model** — optional field in the request body;
   e.g. ``seed-tts-1.1`` for improved 1.0 quality / latency.
3. **speaker** — the voice ID in ``req_params.speaker``;
   must be compatible with the chosen resource_id.

Endpoint: POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
认证: X-Api-App-Id + X-Api-Access-Key + X-Api-Resource-Id
输出: Provider 请求 PCM，本地封装为 WAV 返回

参考: https://www.volcengine.com/docs/6561/1598757
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import uuid
import wave
from typing import Any, Final, Iterator

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resource IDs — select 豆包 1.0 vs 2.0
# ---------------------------------------------------------------------------
RESOURCE_ID_1_0: Final[str] = "seed-tts-1.0"
RESOURCE_ID_2_0: Final[str] = "seed-tts-2.0"
DEFAULT_RESOURCE_ID: Final[str] = RESOURCE_ID_1_0

# ---------------------------------------------------------------------------
# Model identifiers for req_params.model (NOT the same as resource_id)
# ---------------------------------------------------------------------------
MODEL_1_0: Final[str] = "seed-tts-1.1"  # improved quality/latency for 1.0

# ---------------------------------------------------------------------------
# Default speakers per resource (must match the resource's voice suffix)
# ---------------------------------------------------------------------------
DEFAULT_SPEAKER_1_0: Final[str] = "zh_female_shuangkuaisisi_moon_bigtts"
DEFAULT_SPEAKER_2_0: Final[str] = "zh_female_shuangkuaisisi_uranus_bigtts"
DEFAULT_SPEAKER: Final[str] = DEFAULT_SPEAKER_1_0  # backward-compat alias

DEFAULT_ENDPOINT: Final[str] = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0

def default_speaker_for_resource(resource_id: str | None) -> str:
    """Return the default speaker voice for a given resource_id.

    - ``seed-tts-2.0`` → 2.0 default (``_uranus_bigtts``)
    - anything else (including None / 1.0) → 1.0 default (``_moon_bigtts``)
    """
    if resource_id == RESOURCE_ID_2_0:
        return DEFAULT_SPEAKER_2_0
    return DEFAULT_SPEAKER_1_0


# V3 response codes
CODE_AUDIO_CHUNK: Final[int] = 0
CODE_FINISH: Final[int] = 20000000

# PCM params for WAV packaging
PCM_SAMPLE_RATE: Final[int] = 24000
PCM_CHANNELS: Final[int] = 1
PCM_SAMPLE_WIDTH: Final[int] = 2  # 16-bit


class VolcEngineTTSError(Exception):
    """Raised when VolcEngine TTS synthesis fails."""


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _resolve_credentials() -> tuple[str, str, str]:
    """Return (app_id, access_key, resource_id) from environment.

    Supports new env vars (VOLCENGINE_TTS_APP_ID / ACCESS_KEY / RESOURCE_ID)
    with fallback to legacy names (VOLCENGINE_TTS_APPID / ACCESS_TOKEN).
    """
    app_id = (
        os.environ.get("VOLCENGINE_TTS_APP_ID", "").strip()
        or os.environ.get("VOLCENGINE_TTS_APPID", "").strip()
    )
    access_key = (
        os.environ.get("VOLCENGINE_TTS_ACCESS_KEY", "").strip()
        or os.environ.get("VOLCENGINE_TTS_ACCESS_TOKEN", "").strip()
    )
    resource_id = (
        os.environ.get("VOLCENGINE_TTS_RESOURCE_ID", "").strip()
        or DEFAULT_RESOURCE_ID
    )
    if not app_id or not access_key:
        raise VolcEngineTTSError(
            "VOLCENGINE_TTS_APP_ID and VOLCENGINE_TTS_ACCESS_KEY must be set "
            "(or legacy VOLCENGINE_TTS_APPID / VOLCENGINE_TTS_ACCESS_TOKEN)"
        )
    return app_id, access_key, resource_id


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------

def _build_headers(app_id: str, access_key: str, resource_id: str) -> dict[str, str]:
    """Build V3 request headers."""
    return {
        "X-Api-App-Id": app_id,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": uuid.uuid4().hex,
        "Content-Type": "application/json",
    }


def _explicit_language_for_target(target_language: str | None) -> str | None:
    """Map a dub target language to VolcEngine's ``audio_params.explicit_language``.

    Empirically (scripts/test_volcengine_explicit_language.py, 2026-04-15): zh voices
    synthesize English with or without this; en voices are English-bound. Setting it
    for an English target improves cross-language stability. Default (None / zh-CN)
    returns None → the key is omitted → byte-identical payload.
    """
    if not target_language:
        return None
    code = target_language.split("-")[0].lower()
    if code == "en":
        return "en-us"
    return None  # zh / unknown → omit (zh voices are natively multilingual)


def _build_payload(
    text: str,
    speaker: str,
    *,
    model: str | None = None,
    speech_rate: int = 0,
    target_language: str | None = None,
) -> dict[str, Any]:
    """Build V3 request body.

    Parameters
    ----------
    text:
        Text to synthesize.
    speaker:
        Voice ID (``req_params.speaker``).  Must be compatible with the
        ``resource_id`` set in the request headers.
    model:
        Optional model identifier (``req_params.model``).
        Only written to the payload when non-empty; omitting it lets the
        API use the default model for the selected resource.
    speech_rate:
        Integer in [-50, 100].  Positive speeds up, negative slows down.
        Validated 2026-04-15: duration tracks ``1 / (1 + speech_rate/100)``
        to within |err|<5% on both seed-tts-1.0 and 2.0.  Written to
        ``audio_params.speech_rate`` only when non-zero so that pre-Phase 2
        callers that don't pass a rate emit byte-identical payloads.
        Caller is responsible for the speed→speech_rate mapping (see
        ``services.tts.speed_decision.speed_to_volcengine_speech_rate``).
    """
    req_params: dict[str, Any] = {
        "speaker": speaker,
        "text": text,
        "audio_params": {
            "format": "pcm",
            "sample_rate": PCM_SAMPLE_RATE,
        },
    }
    if speech_rate:
        req_params["audio_params"]["speech_rate"] = int(speech_rate)
    _explicit_language = _explicit_language_for_target(target_language)
    if _explicit_language:
        req_params["audio_params"]["explicit_language"] = _explicit_language
    if model:
        req_params["model"] = model
    return {
        "user": {"uid": "aivideotrans"},
        "req_params": req_params,
    }


# ---------------------------------------------------------------------------
# Streaming response parsing
# ---------------------------------------------------------------------------

def _do_post(url: str, *, headers: dict | None = None, json: dict | None = None,
             stream: bool = False, timeout: float | None = None) -> requests.Response:
    """Thin wrapper around requests.post for testability."""
    return requests.post(url, headers=headers, json=json, stream=stream, timeout=timeout)


def _iter_chunk_events(response: Any) -> Iterator[dict[str, Any]]:
    """Parse streaming JSON lines from V3 chunked response."""
    for line in response.iter_lines(decode_unicode=False):
        if not line:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            logger.warning("[VolcEngine] skipping non-JSON line: %s", line[:80])


# ---------------------------------------------------------------------------
# PCM → WAV packaging
# ---------------------------------------------------------------------------

def _pcm_to_wav(
    pcm_bytes: bytes,
    *,
    sample_rate: int = PCM_SAMPLE_RATE,
    channels: int = PCM_CHANNELS,
    sample_width: int = PCM_SAMPLE_WIDTH,
) -> bytes:
    """Wrap raw PCM bytes in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def synthesize(
    text: str,
    voice_id: str | None = None,
    *,
    resource_id: str | None = None,
    model: str | None = None,
    speech_rate: int = 0,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    target_language: str | None = None,
) -> bytes:
    """Synthesize text to WAV audio using VolcEngine V3 Chunked API.

    Parameters
    ----------
    text:
        Text to synthesize (must not be empty).
    voice_id:
        Speaker voice ID (``req_params.speaker``).  When *None*, the
        default speaker for the effective ``resource_id`` is used
        (1.0 → ``_moon_bigtts``, 2.0 → ``_uranus_bigtts``).
    resource_id:
        Explicit resource ID for the ``X-Api-Resource-Id`` header.
        When *None*, falls back to the ``VOLCENGINE_TTS_RESOURCE_ID``
        environment variable, then to ``DEFAULT_RESOURCE_ID``.
    model:
        Optional model identifier written to ``req_params.model``.
        Omitted from the payload when *None* or empty.
    speech_rate:
        Integer in [-50, 100]; default 0 (no adjustment, byte-identical
        payload to pre-Phase 2). Positive speeds up, negative slows down.
        See ``_build_payload`` for the empirical linearity evidence.
    endpoint:
        V3 API endpoint URL.
    timeout_seconds:
        HTTP request timeout.

    Returns WAV bytes (PCM accumulated from stream, then packaged).
    """
    if not text or not text.strip():
        raise VolcEngineTTSError("Text must not be empty")

    app_id, access_key, env_resource_id = _resolve_credentials()
    effective_resource_id = resource_id or env_resource_id
    effective_voice_id = voice_id or default_speaker_for_resource(effective_resource_id)
    headers = _build_headers(app_id, access_key, effective_resource_id)
    payload = _build_payload(
        text.strip(),
        effective_voice_id,
        model=model,
        speech_rate=speech_rate,
        target_language=target_language,
    )

    log_id = ""
    pcm_chunks: list[bytes] = []
    finished = False

    try:
        response = _do_post(
            endpoint,
            headers=headers,
            json=payload,
            stream=True,
            timeout=timeout_seconds,
        )
        log_id = response.headers.get("X-Tt-Logid", "")

        for event in _iter_chunk_events(response):
            code = event.get("code", -1)
            message = event.get("message", "")

            if code == CODE_AUDIO_CHUNK:
                data_b64 = event.get("data", "")
                if data_b64:
                    pcm_chunks.append(base64.b64decode(data_b64))
                continue

            if code == CODE_FINISH:
                finished = True
                break

            # Any other positive code is an error (official demo pattern)
            if code > 0:
                raise VolcEngineTTSError(
                    f"VolcEngine TTS error: code={code}, message={message}, logid={log_id}"
                )

        response.close()

    except VolcEngineTTSError:
        raise
    except requests.RequestException as exc:
        raise VolcEngineTTSError(f"VolcEngine TTS request failed: {exc}") from exc
    except Exception as exc:
        raise VolcEngineTTSError(f"VolcEngine TTS unexpected error: {exc}") from exc

    if not finished:
        raise VolcEngineTTSError(
            f"VolcEngine TTS stream ended without finish event (logid={log_id})"
        )

    if not pcm_chunks:
        raise VolcEngineTTSError(
            f"VolcEngine TTS returned no audio data (logid={log_id})"
        )

    pcm_bytes = b"".join(pcm_chunks)
    wav_bytes = _pcm_to_wav(pcm_bytes)

    logger.info(
        "[VolcEngine] OK: voice=%s, resource=%s, model=%s, text=%s, pcm=%d, wav=%d, logid=%s",
        effective_voice_id, effective_resource_id, model or "(default)",
        text[:30], len(pcm_bytes), len(wav_bytes), log_id,
    )
    return wav_bytes
