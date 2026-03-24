from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib import error, request

from pydub import AudioSegment

from services.gemini.translator import DubbingSegment
from utils.atomic_io import atomic_write_bytes, is_valid_output
from services.tts.rate_limiter import RateLimiter

try:
    import requests
except ImportError:  # pragma: no cover - depends on local environment
    requests = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUTODUB_LOCAL_CONFIG_PATH = PROJECT_ROOT / "autodub.local.json"
DEFAULT_BASE_URL = "https://api.minimaxi.com"
DEFAULT_MODEL = "speech-2.8-turbo"
DEFAULT_AUDIO_FORMAT = "wav"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5


class TTSGenerationError(Exception):
    pass


@dataclass(slots=True)
class TTSConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    speed: float = 1.0
    vol: float = 1.0
    audio_format: str = DEFAULT_AUDIO_FORMAT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS


@dataclass(slots=True)
class TTSResult:
    segment_id: int
    audio_path: str
    duration_ms: int
    voice_id: str


class TTSGenerator:
    def __init__(self, config: TTSConfig):
        normalized_api_key = _normalize_optional_text(config.api_key)
        if normalized_api_key is None:
            raise TTSGenerationError("TTS api_key is required.")

        self.config = TTSConfig(
            api_key=normalized_api_key,
            base_url=_normalize_optional_text(config.base_url) or DEFAULT_BASE_URL,
            model=_normalize_optional_text(config.model) or DEFAULT_MODEL,
            speed=float(config.speed),
            vol=float(config.vol),
            audio_format=_normalize_optional_text(config.audio_format) or DEFAULT_AUDIO_FORMAT,
            timeout_seconds=max(1.0, float(config.timeout_seconds)),
            max_retries=max(0, int(config.max_retries)),
            retry_backoff_seconds=max(0.0, float(config.retry_backoff_seconds)),
        )

    def generate_all(
        self,
        segments: list[DubbingSegment],
        output_dir: str,
    ) -> list[TTSResult]:
        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)

        results: list[TTSResult] = []
        total_segments = len(segments)
        rate_limiter = RateLimiter(rpm=20)
        for index, segment in enumerate(segments, start=1):
            output_path = output_root / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"
            if is_valid_output(str(output_path)):
                print(f"[TTS] 跳过已完成段 {index}/{total_segments}")
                duration_ms = len(AudioSegment.from_wav(output_path))
                result = TTSResult(
                    segment_id=segment.segment_id,
                    audio_path=str(output_path.resolve(strict=False)),
                    duration_ms=duration_ms,
                    voice_id=segment.voice_id,
                )
                segment.tts_audio_path = result.audio_path
                segment.actual_duration_ms = result.duration_ms
                if segment.target_duration_ms > 0:
                    segment.alignment_ratio = result.duration_ms / segment.target_duration_ms
                else:
                    segment.alignment_ratio = 0.0
                results.append(result)
                if total_segments > 0 and (index % 5 == 0 or index == total_segments):
                    print(f"[S4] TTS进度：{index}/{total_segments} 段")
                continue
            rate_limiter.wait()
            result = self._generate_one(segment, str(output_root))
            segment.tts_audio_path = result.audio_path
            segment.actual_duration_ms = result.duration_ms
            if segment.target_duration_ms > 0:
                segment.alignment_ratio = result.duration_ms / segment.target_duration_ms
            else:
                segment.alignment_ratio = 0.0
            results.append(result)
            if total_segments > 0 and (index % 5 == 0 or index == total_segments):
                print(f"[S4] TTS进度：{index}/{total_segments} 段")
        return results

    def _generate_one(
        self,
        segment: DubbingSegment,
        output_dir: str,
    ) -> TTSResult:
        output_root = Path(output_dir).resolve(strict=False)
        output_root.mkdir(parents=True, exist_ok=True)

        endpoint = _build_tts_endpoint(self.config.base_url)
        tts_text = _normalize_optional_text(segment.tts_cn_text) or _normalize_optional_text(segment.cn_text)
        if tts_text is None:
            raise TTSGenerationError("segment.tts_cn_text or segment.cn_text is required.")
        payload = {
            "model": self.config.model,
            "text": tts_text,
            "voice_setting": {
                "voice_id": segment.voice_id,
                "speed": self.config.speed,
                "vol": self.config.vol,
            },
            "audio_setting": {
                "format": self.config.audio_format,
                "sample_rate": 24000,
            },
        }

        response_payload = _post_json(
            endpoint=endpoint,
            api_key=self.config.api_key,
            payload=payload,
            timeout_seconds=self.config.timeout_seconds,
            max_retries=self.config.max_retries,
            retry_backoff_seconds=self.config.retry_backoff_seconds,
        )
        base_resp = response_payload.get("base_resp")
        if not isinstance(base_resp, dict):
            raise TTSGenerationError("MiniMax TTS response is missing base_resp.")
        status_code = _coerce_int(base_resp.get("status_code"), default=-1)
        status_msg = _normalize_optional_text(base_resp.get("status_msg")) or "unknown error"
        if status_code != 0:
            raise TTSGenerationError(
                f"MiniMax TTS business error: status_code={status_code} status_msg={status_msg}"
            )

        data = response_payload.get("data")
        if not isinstance(data, dict):
            raise TTSGenerationError("MiniMax TTS response is missing data.")
        audio_hex = _normalize_optional_text(data.get("audio"))
        if audio_hex is None:
            raise TTSGenerationError("MiniMax TTS response is missing data.audio.")
        try:
            audio_bytes = bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise TTSGenerationError("MiniMax TTS audio payload is not valid hex.") from exc

        output_path = output_root / f"segment_{segment.segment_id:03d}_{segment.speaker_id}.wav"
        try:
            atomic_write_bytes(str(output_path), audio_bytes)
            duration_ms = len(AudioSegment.from_wav(output_path))
        except OSError as exc:
            raise TTSGenerationError(f"Failed to write or read TTS audio output: {output_path}") from exc
        except Exception as exc:
            raise TTSGenerationError(f"Failed to decode generated wav audio: {output_path}") from exc

        return TTSResult(
            segment_id=segment.segment_id,
            audio_path=str(output_path.resolve(strict=False)),
            duration_ms=duration_ms,
            voice_id=segment.voice_id,
        )


def load_tts_config() -> TTSConfig:
    config_path = DEFAULT_AUTODUB_LOCAL_CONFIG_PATH.resolve(strict=False)
    payload: dict[str, object] = {}

    if config_path.exists():
        try:
            loaded_payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TTSGenerationError(f"Failed to load TTS config from {config_path}") from exc
        if not isinstance(loaded_payload, dict):
            raise TTSGenerationError(f"TTS config file must contain a top-level JSON object: {config_path}")
        payload = loaded_payload

    section = payload.get("tts", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise TTSGenerationError("tts config section must be a JSON object.")

    api_key_env_var = _normalize_optional_text(section.get("api_key_env_var")) or "AUTODUB_TTS_API_KEY"
    api_key = _normalize_optional_text(section.get("api_key"))
    if api_key is None:
        api_key = _normalize_optional_text(os.getenv(api_key_env_var))
    if api_key is None:
        raise TTSGenerationError(
            f"TTS API key is required via autodub.local.json or env {api_key_env_var}."
        )

    return TTSConfig(
        api_key=api_key,
        base_url=_normalize_optional_text(section.get("base_url")) or DEFAULT_BASE_URL,
        model=_normalize_optional_text(section.get("model_name")) or DEFAULT_MODEL,
        speed=_coerce_float(section.get("speed"), default=1.0),
        vol=_coerce_float(section.get("vol"), default=1.0),
        audio_format=_normalize_optional_text(section.get("audio_format")) or DEFAULT_AUDIO_FORMAT,
        timeout_seconds=_coerce_float(section.get("timeout_seconds"), default=DEFAULT_TIMEOUT_SECONDS),
        max_retries=_coerce_int(section.get("max_retries"), default=DEFAULT_MAX_RETRIES),
        retry_backoff_seconds=_coerce_float(
            section.get("retry_backoff_seconds"),
            default=DEFAULT_RETRY_BACKOFF_SECONDS,
        ),
    )


def _post_json(
    *,
    endpoint: str,
    api_key: str,
    payload: dict[str, object],
    timeout_seconds: float,
    max_retries: int,
    retry_backoff_seconds: float,
) -> dict[str, object]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: TTSGenerationError | None = None
    for attempt in range(max_retries + 1):
        try:
            if requests is not None:
                response = requests.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=timeout_seconds,
                )
                status_code = _coerce_int(getattr(response, "status_code", None), default=0)
                if status_code != 200:
                    if _is_retryable_http_status(status_code):
                        raise TTSGenerationError(f"MiniMax TTS HTTP error: status_code={status_code}")
                    raise TTSGenerationError(f"MiniMax TTS HTTP error: status_code={status_code}")
                try:
                    loaded = response.json()
                except Exception as exc:
                    raise TTSGenerationError("MiniMax TTS response is not valid JSON.") from exc
                if not isinstance(loaded, dict):
                    raise TTSGenerationError("MiniMax TTS response JSON must be an object.")
                return loaded

            serialized_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_obj = request.Request(endpoint, data=serialized_payload, headers=headers, method="POST")
            with request.urlopen(request_obj, timeout=timeout_seconds) as response:
                body = response.read()
                status_code = _coerce_int(getattr(response, "status", None), default=response.getcode())
            if status_code != 200:
                if _is_retryable_http_status(status_code):
                    raise TTSGenerationError(f"MiniMax TTS HTTP error: status_code={status_code}")
                raise TTSGenerationError(f"MiniMax TTS HTTP error: status_code={status_code}")
            try:
                loaded = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TTSGenerationError("MiniMax TTS response is not valid JSON.") from exc
            if not isinstance(loaded, dict):
                raise TTSGenerationError("MiniMax TTS response JSON must be an object.")
            return loaded
        except error.HTTPError as exc:
            if _is_retryable_http_status(exc.code):
                last_error = TTSGenerationError(f"MiniMax TTS HTTP error: status_code={exc.code}")
            else:
                raise TTSGenerationError(f"MiniMax TTS HTTP error: status_code={exc.code}") from exc
        except error.URLError as exc:
            last_error = TTSGenerationError(f"MiniMax TTS request failed: {exc.reason}")
        except OSError as exc:
            last_error = TTSGenerationError(f"MiniMax TTS request failed: {exc}")
        except TTSGenerationError as exc:
            if not _is_retryable_tts_error(exc):
                raise
            last_error = exc
        except Exception as exc:
            last_error = TTSGenerationError(f"MiniMax TTS request failed: {exc}")

        if attempt < max_retries and last_error is not None:
            wait_seconds = retry_backoff_seconds * (2 ** attempt)
            print(
                f"[S4] MiniMax请求失败，{wait_seconds:g}秒后重试（{attempt + 1}/{max_retries}）：{last_error}"
            )
            time.sleep(wait_seconds)
        elif last_error is not None:
            raise last_error

    raise TTSGenerationError("MiniMax TTS request failed: unknown error")


def _build_tts_endpoint(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1/t2a_v2"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/t2a_v2"
    return f"{normalized}/v1/t2a_v2"


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or 500 <= status_code < 600


def _is_retryable_tts_error(error_obj: TTSGenerationError) -> bool:
    message = str(error_obj)
    return (
        "request failed" in message
        or "HTTP error: status_code=408" in message
        or "HTTP error: status_code=409" in message
        or "HTTP error: status_code=425" in message
        or "HTTP error: status_code=429" in message
        or "HTTP error: status_code=5" in message
        or "response is not valid JSON" in message
    )


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _coerce_float(value: object, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
