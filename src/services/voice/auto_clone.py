from __future__ import annotations

import json
from pathlib import Path
import time
from urllib import error, request

from services.tts.tts_generator import DEFAULT_TIMEOUT_SECONDS
from services.voice_clone import (
    MiniMaxVoiceCloneClient,
    VoiceCloneConfig,
    _build_ascii_identifier_fragment,
)
from services.voice_registry import VoiceRegistry

try:
    import requests
except ImportError:  # pragma: no cover - depends on local environment
    requests = None  # type: ignore[assignment]


DEFAULT_CLONE_PROVIDER = "minimax_voice_clone"
DEFAULT_TTS_PROVIDER = "minimax_tts"
DEFAULT_PLATFORM = "minimax_domestic"
DEFAULT_READY_TEST_TEXT = "这是一段测试语音。"
DEFAULT_READY_TEST_MODEL = "speech-2.8-turbo"


class AutoCloneError(Exception):
    pass


class AutoVoiceCloner:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.minimaxi.com",
        *,
        timeout_seconds: float = 180.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
    ):
        self.clone_config = VoiceCloneConfig(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )

    def clone_voice(
        self,
        sample_path: str,
        speaker_name: str,
    ) -> str:
        try:
            clone_client = MiniMaxVoiceCloneClient(self.clone_config)
            result = clone_client.create_voice_clone(
                speaker_id=_build_speaker_id(speaker_name),
                speaker_name=speaker_name,
                source_audio_path=Path(sample_path),
            )
        except Exception as exc:
            raise AutoCloneError(f"自动克隆失败：{exc}") from exc
        return result.voice_id

    def register_voice(
        self,
        voice_id: str,
        speaker_name: str,
        sample_path: str,
        voice_registry_path: str,
    ) -> None:
        try:
            registry = VoiceRegistry(voice_registry_path)
            registry_data = registry.load()
            speaker_id = _find_existing_speaker_id(registry_data, speaker_name) or _build_speaker_id(speaker_name)
            registry.register_voice(
                speaker_id,
                speaker_name=speaker_name,
                voice_id=voice_id,
                voice_type="cloned",
                provider=DEFAULT_CLONE_PROVIDER,
                tts_provider=DEFAULT_TTS_PROVIDER,
                platform=DEFAULT_PLATFORM,
                label=f"{speaker_name} Auto Clone",
                source_audio_path=sample_path,
                notes="自动提取，来自视频音频",
                set_default=True,
            )
        except Exception as exc:
            raise AutoCloneError(f"写入音色库失败：{exc}") from exc

    def wait_until_ready(
        self,
        voice_id: str,
        max_wait_seconds: int = 300,
        poll_interval_seconds: int = 15,
    ) -> bool:
        elapsed_seconds = 0
        while elapsed_seconds <= max_wait_seconds:
            if self._probe_voice_ready(voice_id):
                return True
            print(f"[S2] 等待音色就绪... ({elapsed_seconds}s/{max_wait_seconds}s)")
            if elapsed_seconds >= max_wait_seconds:
                break
            time.sleep(poll_interval_seconds)
            elapsed_seconds += poll_interval_seconds
        return False

    def _probe_voice_ready(self, voice_id: str) -> bool:
        endpoint = _build_tts_endpoint(self.clone_config.base_url or "https://api.minimaxi.com")
        payload = {
            "model": DEFAULT_READY_TEST_MODEL,
            "text": DEFAULT_READY_TEST_TEXT,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": 1.0,
                "vol": 1.0,
            },
            "audio_setting": {
                "format": "wav",
                "sample_rate": 24000,
            },
        }

        try:
            response_payload = _post_json(
                endpoint=endpoint,
                api_key=self.clone_config.resolved_api_key() or "",
                payload=payload,
            )
        except Exception:
            return False

        base_resp = response_payload.get("base_resp")
        if not isinstance(base_resp, dict):
            return False
        return int(base_resp.get("status_code", -1)) == 0


def _find_existing_speaker_id(registry_data: dict[str, object], speaker_name: str) -> str | None:
    speakers_section = registry_data.get("speakers", {})
    if not isinstance(speakers_section, dict):
        return None

    normalized_name = speaker_name.strip().casefold()
    for speaker_id, speaker_payload in speakers_section.items():
        if not isinstance(speaker_payload, dict):
            continue
        existing_name = str(speaker_payload.get("speaker_name", "")).strip().casefold()
        if existing_name and existing_name == normalized_name:
            return str(speaker_id).strip() or None
    return None


def _build_speaker_id(speaker_name: str) -> str:
    normalized = _build_ascii_identifier_fragment(speaker_name, fallback="auto")
    return f"speaker_{normalized}"


def _build_tts_endpoint(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1/t2a_v2"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/t2a_v2"
    return f"{normalized}/v1/t2a_v2"


def _post_json(
    *,
    endpoint: str,
    api_key: str,
    payload: dict[str, object],
) -> dict[str, object]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if requests is not None:
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        if int(getattr(response, "status_code", 0)) != 200:
            raise AutoCloneError(f"TTS探活失败：HTTP {getattr(response, 'status_code', 0)}")
        loaded = response.json()
        if not isinstance(loaded, dict):
            raise AutoCloneError("TTS探活返回不是JSON对象。")
        return loaded

    request_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_obj = request.Request(endpoint, data=request_payload, headers=headers, method="POST")
    try:
        with request.urlopen(request_obj, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            if int(getattr(response, "status", response.getcode())) != 200:
                raise AutoCloneError(f"TTS探活失败：HTTP {response.getcode()}")
            body = response.read()
    except error.HTTPError as exc:
        raise AutoCloneError(f"TTS探活失败：HTTP {exc.code}") from exc
    except error.URLError as exc:
        raise AutoCloneError(f"TTS探活失败：{exc.reason}") from exc
    except OSError as exc:
        raise AutoCloneError(f"TTS探活失败：{exc}") from exc

    loaded = json.loads(body.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise AutoCloneError("TTS探活返回不是JSON对象。")
    return loaded
