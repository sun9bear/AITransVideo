"""MiMo-V2-TTS provider — alternative to MiniMax TTS.

API: POST https://api.xiaomimimo.com/v1/chat/completions
Auth: Bearer token via MIMO_API_KEY env var
RPM limit: 100 (vs MiniMax's 20)
Available voices: mimo_default, default_zh, default_en
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib import error, request

try:
    import requests as _requests_lib
except ImportError:  # pragma: no cover
    _requests_lib = None  # type: ignore[assignment]


DEFAULT_MIMO_BASE_URL = "https://api.xiaomimimo.com/v1/chat/completions"
DEFAULT_MIMO_MODEL = "mimo-v2-tts"
DEFAULT_MIMO_VOICE = "default_zh"
DEFAULT_MIMO_AUDIO_FORMAT = "wav"
DEFAULT_MIMO_TIMEOUT_SECONDS = 60
DEFAULT_MIMO_MAX_RETRIES = 5
DEFAULT_MIMO_RETRY_BACKOFF_SECONDS = 3.0
DEFAULT_MIMO_RPM = 100

# Valid MiMo voice IDs
MIMO_VOICES = {"mimo_default", "default_zh", "default_en"}


class MiMoTTSError(Exception):
    """Raised when MiMo TTS synthesis fails."""
    pass


def synthesize(
    text: str,
    voice_id: str = DEFAULT_MIMO_VOICE,
    *,
    api_key: str | None = None,
    endpoint: str = DEFAULT_MIMO_BASE_URL,
    model: str = DEFAULT_MIMO_MODEL,
    audio_format: str = DEFAULT_MIMO_AUDIO_FORMAT,
    timeout_seconds: float = DEFAULT_MIMO_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MIMO_MAX_RETRIES,
    retry_backoff_seconds: float = DEFAULT_MIMO_RETRY_BACKOFF_SECONDS,
) -> bytes:
    """Synthesize text to audio bytes via MiMo-V2-TTS.

    Args:
        text: The text to synthesize.
        voice_id: One of mimo_default, default_zh, default_en.
        api_key: API key. Falls back to MIMO_API_KEY env var.
        endpoint: API endpoint URL.
        model: Model name.
        audio_format: Output audio format (wav).
        timeout_seconds: HTTP request timeout.
        max_retries: Number of retries on transient failure.
        retry_backoff_seconds: Base backoff between retries.

    Returns:
        Raw audio bytes (WAV).

    Raises:
        MiMoTTSError: On any synthesis failure.
    """
    if not text or not text.strip():
        raise MiMoTTSError("Text to synthesize is empty.")

    resolved_key = api_key or os.environ.get("MIMO_API_KEY")
    if not resolved_key:
        raise MiMoTTSError(
            "MiMo API key is required. Set MIMO_API_KEY env var or pass api_key."
        )

    if voice_id not in MIMO_VOICES:
        print(f"[MiMo-TTS] Warning: unknown voice_id '{voice_id}', using '{DEFAULT_MIMO_VOICE}'")
        voice_id = DEFAULT_MIMO_VOICE

    payload = {
        "model": model,
        "messages": [{"role": "assistant", "content": text}],
        "modalities": ["audio"],
        "audio": {"voice": voice_id, "format": audio_format},
    }

    response_data = _post_json(
        endpoint=endpoint,
        api_key=resolved_key,
        payload=payload,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )

    # Extract base64 audio from response: choices[0].message.audio.data
    choices = response_data.get("choices")
    if not isinstance(choices, list) or len(choices) == 0:
        raise MiMoTTSError("MiMo TTS response missing choices array.")

    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise MiMoTTSError("MiMo TTS response missing choices[0].message.")

    audio_obj = message.get("audio")
    if not isinstance(audio_obj, dict):
        raise MiMoTTSError("MiMo TTS response missing choices[0].message.audio.")

    audio_b64 = audio_obj.get("data")
    if not audio_b64 or not isinstance(audio_b64, str):
        raise MiMoTTSError("MiMo TTS response missing audio.data (base64).")

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception as exc:
        raise MiMoTTSError("MiMo TTS audio payload is not valid base64.") from exc

    if len(audio_bytes) < 44:
        raise MiMoTTSError(
            f"MiMo TTS returned suspiciously small audio ({len(audio_bytes)} bytes)."
        )

    return audio_bytes


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post_json(
    *,
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    max_retries: int,
    retry_backoff_seconds: float,
) -> dict[str, Any]:
    """POST JSON to MiMo API with retry + exponential backoff."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: MiMoTTSError | None = None

    for attempt in range(max_retries + 1):
        try:
            return _do_post(endpoint, headers, payload, timeout_seconds)
        except MiMoTTSError as exc:
            last_error = exc
            if not _is_retryable(exc):
                raise
            if attempt < max_retries:
                wait = min(retry_backoff_seconds * (2 ** attempt), 60.0)
                print(
                    f"[MiMo-TTS] 请求失败，{wait:g}s 后重试"
                    f"（{attempt + 1}/{max_retries}）：{exc}"
                )
                time.sleep(wait)

    if last_error is not None:
        raise last_error
    raise MiMoTTSError("MiMo TTS request failed: unknown error")


def _do_post(
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Single HTTP POST attempt."""
    # Prefer requests library if available
    if _requests_lib is not None:
        resp = _requests_lib.post(
            endpoint, headers=headers, json=payload, timeout=timeout_seconds,
        )
        if resp.status_code != 200:
            raise MiMoTTSError(
                f"MiMo TTS HTTP error: status_code={resp.status_code}"
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise MiMoTTSError("MiMo TTS response is not valid JSON.") from exc
        if not isinstance(data, dict):
            raise MiMoTTSError("MiMo TTS response JSON must be an object.")
        return data

    # Fallback: urllib
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(endpoint, data=body_bytes, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read()
            status = getattr(resp, "status", None) or resp.getcode()
    except error.HTTPError as exc:
        raise MiMoTTSError(f"MiMo TTS HTTP error: status_code={exc.code}") from exc
    except error.URLError as exc:
        raise MiMoTTSError(f"MiMo TTS request failed: {exc.reason}") from exc
    except OSError as exc:
        raise MiMoTTSError(f"MiMo TTS request failed: {exc}") from exc

    if status != 200:
        raise MiMoTTSError(f"MiMo TTS HTTP error: status_code={status}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MiMoTTSError("MiMo TTS response is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise MiMoTTSError("MiMo TTS response JSON must be an object.")
    return data


def _is_retryable(exc: MiMoTTSError) -> bool:
    msg = str(exc)
    return (
        "request failed" in msg
        or "status_code=408" in msg
        or "status_code=429" in msg
        or "status_code=5" in msg
        or "not valid JSON" in msg
    )
