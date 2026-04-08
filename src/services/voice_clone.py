from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
from urllib import error, request

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]

from services import config_loader


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUTODUB_LOCAL_CONFIG_PATH = PROJECT_ROOT / "autodub.local.json"
SUPPORTED_VOICE_CLONE_AUDIO_EXTENSIONS = (".wav", ".wave", ".mp3", ".m4a", ".flac", ".ogg")


class VoiceCloneError(Exception):
    """Base error for explicit voice clone commands."""


class VoiceCloneInputError(VoiceCloneError):
    """Raised when the source audio file is missing, invalid, or unsupported."""


class VoiceCloneConfigurationError(VoiceCloneError):
    """Raised when required voice clone configuration is missing or invalid."""


class VoiceCloneUploadError(VoiceCloneError):
    """Raised when the MiniMax file upload step fails."""


class VoiceCloneAPIError(VoiceCloneError):
    """Raised when the MiniMax voice_clone step fails."""


class VoiceCloneResponseFormatError(VoiceCloneError):
    """Raised when provider responses do not contain required identifiers."""


@dataclass(slots=True)
class VoiceCloneConfig:
    enabled: bool = True
    provider_name: str = "minimax_voice_clone"
    base_url: str | None = None
    base_url_source: str | None = None
    model_name: str | None = None
    model_name_source: str | None = None
    api_key: str | None = None
    api_key_source: str | None = None
    api_key_env_var: str = "AUTODUB_TTS_API_KEY"
    api_key_env_var_source: str | None = None
    timeout_seconds: float = 180.0
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    config_path: str | None = None
    config_file_error: str | None = None

    @classmethod
    def from_env(
        cls,
        prefix: str = "AUTODUB_TTS_",
        *,
        config_path: Path | None = None,
    ) -> "VoiceCloneConfig":
        with _override_config_loader_env_readers():
            config = config_loader.load_project_local_config(config_path or DEFAULT_AUTODUB_LOCAL_CONFIG_PATH)
            enabled, _ = config_loader.resolve_bool_value(
                env_keys=[f"{prefix}CLONE_ENABLED"],
                config=config,
                config_key_paths=(("voice_clone", "enabled"),),
                default=True,
            )
            provider_name, _ = config_loader.resolve_text_value(
                env_keys=[f"{prefix}CLONE_PROVIDER_NAME"],
                config=config,
                config_key_paths=(("voice_clone", "provider_name"),),
            )
            base_url, base_url_source = config_loader.resolve_text_value(
                env_keys=[
                    f"{prefix}CLONE_BASE_URL",
                    f"{prefix}BASE_URL",
                ],
                config=config,
                config_key_paths=(
                    ("voice_clone", "base_url"),
                    ("tts", "base_url"),
                ),
            )
            model_name, model_name_source = config_loader.resolve_text_value(
                env_keys=[
                    f"{prefix}CLONE_MODEL_NAME",
                    f"{prefix}MODEL_NAME",
                ],
                config=config,
                config_key_paths=(
                    ("voice_clone", "model_name"),
                    ("tts", "model_name"),
                ),
            )
            api_key_env_var, api_key_env_var_source = config_loader.resolve_text_value(
                env_keys=[f"{prefix}CLONE_API_KEY_ENV_VAR"],
                config=config,
                config_key_paths=(("voice_clone", "api_key_env_var"), ("tts", "api_key_env_var")),
            )
            resolved_api_key_env_var = api_key_env_var or "AUTODUB_TTS_API_KEY"
            api_key, api_key_source = config_loader.resolve_text_value(
                env_keys=[
                    f"{prefix}CLONE_API_KEY",
                    resolved_api_key_env_var,
                    f"{prefix}API_KEY",
                    "AUTODUB_TTS_API_KEY",
                ],
                config=config,
                config_key_paths=(
                    ("voice_clone", "api_key"),
                    ("tts", "api_key"),
                ),
            )
            timeout_seconds, _ = config_loader.resolve_float_value(
                env_keys=[f"{prefix}CLONE_TIMEOUT_SECONDS"],
                config=config,
                config_key_paths=(("voice_clone", "timeout_seconds"),),
                default=180.0,
            )
            max_retries, _ = config_loader.resolve_int_value(
                env_keys=[f"{prefix}CLONE_MAX_RETRIES"],
                config=config,
                config_key_paths=(("voice_clone", "max_retries"),),
                default=2,
            )
            retry_backoff_seconds, _ = config_loader.resolve_float_value(
                env_keys=[f"{prefix}CLONE_RETRY_BACKOFF_SECONDS"],
                config=config,
                config_key_paths=(("voice_clone", "retry_backoff_seconds"),),
                default=1.0,
            )
        return cls(
            enabled=enabled,
            provider_name=provider_name or "minimax_voice_clone",
            base_url=base_url,
            base_url_source=base_url_source,
            model_name=model_name,
            model_name_source=model_name_source,
            api_key=api_key,
            api_key_source=api_key_source,
            api_key_env_var=resolved_api_key_env_var,
            api_key_env_var_source=api_key_env_var_source,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            config_path=str(config.path) if config.payload is not None or config.error is not None else None,
            config_file_error=config.error,
        )

    def resolved_api_key(self) -> str | None:
        if self.api_key is not None:
            return self.api_key
        with _override_config_loader_env_readers():
            resolved_api_key, resolved_api_key_source = config_loader.resolve_env_text_value(
                [self.api_key_env_var, "AUTODUB_TTS_API_KEY"]
            )
        if resolved_api_key is not None:
            self.api_key_source = self.api_key_source or resolved_api_key_source
        return resolved_api_key

    def validate(self) -> None:
        if self.config_file_error is not None:
            raise VoiceCloneConfigurationError(self.config_file_error)
        if not self.enabled:
            raise VoiceCloneConfigurationError(
                "Voice clone is disabled. Set AUTODUB_TTS_CLONE_ENABLED=true."
            )
        if self.base_url is None:
            raise VoiceCloneConfigurationError(
                "Voice clone base_url is required. "
                "Set AUTODUB_TTS_CLONE_BASE_URL or AUTODUB_TTS_BASE_URL."
            )
        if self.resolved_api_key() is None:
            raise VoiceCloneConfigurationError(
                "Voice clone API key is required. "
                "Set AUTODUB_TTS_CLONE_API_KEY or AUTODUB_TTS_API_KEY."
            )
        if self.timeout_seconds <= 0:
            raise VoiceCloneConfigurationError("Voice clone timeout_seconds must be positive.")
        if self.max_retries < 0:
            raise VoiceCloneConfigurationError("Voice clone max_retries must be >= 0.")
        if self.retry_backoff_seconds < 0:
            raise VoiceCloneConfigurationError(
                "Voice clone retry_backoff_seconds must be >= 0."
            )

    def build_diagnostic_summary(self) -> dict[str, object]:
        resolved_api_key = self.resolved_api_key()
        api_key_source = self.api_key_source or ("direct_config" if self.api_key is not None else None)
        return {
            "base_url_present": self.base_url is not None,
            "base_url_source": self.base_url_source or ("direct_config" if self.base_url is not None else None),
            "model_name_present": self.model_name is not None,
            "model_name_source": self.model_name_source or ("direct_config" if self.model_name is not None else None),
            "api_key_present": resolved_api_key is not None,
            "api_key_source": api_key_source,
            "api_key_source_type": _classify_api_key_source_type(
                api_key_source,
                resolved_api_key_present=resolved_api_key is not None,
                api_key_env_var=self.api_key_env_var,
                api_key_env_var_source=self.api_key_env_var_source,
                direct_env_keys=("AUTODUB_TTS_CLONE_API_KEY", "AUTODUB_TTS_API_KEY"),
            ),
            "config_path": self.config_path,
            "config_file_error": self.config_file_error,
            "config_source": _summarize_config_source(
                self.base_url_source,
                self.api_key_source,
                self.model_name_source,
            ),
        }


@dataclass(slots=True)
class VoiceCloneResult:
    speaker_id: str
    speaker_name: str
    source_audio_path: str
    uploaded_file_id: str
    voice_id: str
    provider_name: str
    model_name: str | None


class MiniMaxVoiceCloneClient:
    """Minimal explicit MiniMax voice clone client for manual registry management."""

    def __init__(self, config: VoiceCloneConfig) -> None:
        self.config = config
        self.config.validate()

    def create_voice_clone(
        self,
        *,
        speaker_id: str,
        speaker_name: str,
        source_audio_path: Path,
        need_noise_reduction: bool = False,
    ) -> VoiceCloneResult:
        normalized_speaker_id = _normalize_required_text(speaker_id, field_name="speaker_id")
        normalized_speaker_name = _normalize_required_text(speaker_name, field_name="speaker_name")
        validated_source_audio_path = self._validate_source_audio_path(source_audio_path)

        uploaded_file_id = self._upload_source_audio(validated_source_audio_path)
        cloned_voice_id = self._clone_voice(
            file_id=uploaded_file_id,
            speaker_id=normalized_speaker_id,
            speaker_name=normalized_speaker_name,
            need_noise_reduction=need_noise_reduction,
        )
        return VoiceCloneResult(
            speaker_id=normalized_speaker_id,
            speaker_name=normalized_speaker_name,
            source_audio_path=str(validated_source_audio_path),
            uploaded_file_id=uploaded_file_id,
            voice_id=cloned_voice_id,
            provider_name=self.config.provider_name,
            model_name=self.config.model_name,
        )

    def _validate_source_audio_path(self, source_audio_path: Path) -> Path:
        resolved_path = source_audio_path.expanduser().resolve(strict=False)
        if not resolved_path.exists():
            raise VoiceCloneInputError(f"source audio file not found: {resolved_path}")
        if not resolved_path.is_file():
            raise VoiceCloneInputError(f"source audio path must be a file: {resolved_path}")
        if resolved_path.suffix.lower() not in SUPPORTED_VOICE_CLONE_AUDIO_EXTENSIONS:
            supported_extensions = ", ".join(SUPPORTED_VOICE_CLONE_AUDIO_EXTENSIONS)
            raise VoiceCloneInputError(
                "unsupported source audio format. "
                f"Expected one of: {supported_extensions}. Got: {resolved_path.suffix or '<no extension>'}"
            )
        if resolved_path.stat().st_size <= 0:
            raise VoiceCloneInputError(f"source audio file is empty: {resolved_path}")
        return resolved_path

    def _upload_source_audio(self, source_audio_path: Path) -> str:
        endpoint = self._build_endpoint("files/upload")
        file_name = source_audio_path.name
        file_bytes = source_audio_path.read_bytes()
        body_bytes, content_type = self._build_upload_multipart_body(
            file_name=file_name,
            file_bytes=file_bytes,
        )
        response_payload = self._post_binary_request(
            endpoint=endpoint,
            body=body_bytes,
            content_type=content_type,
            failure_error_type="upload",
        )
        file_payload = response_payload.get("file")
        if isinstance(file_payload, dict):
            strict_file_id = _extract_first_string(
                file_payload,
                key_paths=(
                    ("file_id",),
                ),
            )
            if strict_file_id is not None:
                return strict_file_id

        file_id = _extract_first_string(
            response_payload,
            key_paths=(
                ("data", "file", "file_id"),
                ("data", "file_id"),
            ),
        )
        if file_id is None:
            top_level_keys = sorted(str(key) for key in response_payload.keys())
            base_resp_payload = response_payload.get("base_resp")
            file_keys: list[str] | None = None
            if isinstance(file_payload, dict):
                file_keys = sorted(str(key) for key in file_payload.keys())
            file_object_preview = _build_object_preview(file_payload)
            base_resp_status_code: object = None
            base_resp_status_msg: object = None
            if isinstance(base_resp_payload, dict):
                base_resp_status_code = base_resp_payload.get("status_code")
                base_resp_status_msg = base_resp_payload.get("status_msg")
            raise VoiceCloneResponseFormatError(
                "upload response missing file.file_id. "
                f"top_level_keys={top_level_keys}. "
                f"file_keys={file_keys}. "
                f"file_object_preview={file_object_preview}. "
                f"base_resp_status_code={base_resp_status_code}. "
                f"base_resp_status_msg={base_resp_status_msg}."
            )
        return file_id

    def _clone_voice(
        self,
        *,
        file_id: str,
        speaker_id: str,
        speaker_name: str,
        need_noise_reduction: bool = False,
    ) -> str:
        endpoint = self._build_endpoint("voice_clone")
        del speaker_name
        requested_voice_id = _build_requested_voice_id(speaker_id)
        payload: dict[str, object] = {
            "file_id": _coerce_clone_request_file_id(file_id),
            "voice_id": requested_voice_id,
        }
        if need_noise_reduction:
            payload["need_noise_reduction"] = True
        response_payload = self._post_json_request(
            endpoint=endpoint,
            payload=payload,
            failure_error_type="clone",
        )
        top_level_keys = sorted(str(key) for key in response_payload.keys())
        base_resp_payload = response_payload.get("base_resp")
        base_resp_status_code, base_resp_status_msg = _parse_base_resp_status(base_resp_payload)
        if base_resp_status_code != 0:
            raise VoiceCloneAPIError(
                "voice_clone returned non-success base_resp. "
                f"top_level_keys={top_level_keys}. "
                f"base_resp_status_code={base_resp_status_code}. "
                f"base_resp_status_msg={base_resp_status_msg}."
            )
        return requested_voice_id

    def _post_binary_request(
        self,
        *,
        endpoint: str,
        body: bytes,
        content_type: str,
        failure_error_type: str,
    ) -> dict[str, object]:
        headers = {
            "Authorization": f"Bearer {self.config.resolved_api_key()}",
            "Content-Type": content_type,
        }
        request_object = request.Request(endpoint, data=body, headers=headers, method="POST")
        response_body = self._execute_http_request(
            request_object=request_object,
            failure_error_type=failure_error_type,
        )
        return self._parse_json_response(response_body=response_body, failure_error_type=failure_error_type)

    def _post_json_request(
        self,
        *,
        endpoint: str,
        payload: dict[str, object],
        failure_error_type: str,
    ) -> dict[str, object]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.resolved_api_key()}",
            "Content-Type": "application/json",
        }
        request_object = request.Request(endpoint, data=body, headers=headers, method="POST")
        response_body = self._execute_http_request(
            request_object=request_object,
            failure_error_type=failure_error_type,
        )
        return self._parse_json_response(response_body=response_body, failure_error_type=failure_error_type)

    def _execute_http_request(
        self,
        *,
        request_object: request.Request,
        failure_error_type: str,
    ) -> bytes:
        error_cls: type[VoiceCloneError] = VoiceCloneUploadError
        if failure_error_type == "clone":
            error_cls = VoiceCloneAPIError
        max_attempts = max(1, int(self.config.max_retries) + 1)
        for attempt_index in range(max_attempts):
            attempt_number = attempt_index + 1
            try:
                with request.urlopen(request_object, timeout=self.config.timeout_seconds) as response:
                    return response.read()
            except error.HTTPError as exc:
                response_excerpt = _read_http_error_excerpt(exc)
                if attempt_number < max_attempts and _should_retry_http_status(exc.code):
                    self._log_retry_attempt(
                        failure_error_type=failure_error_type,
                        reason=f"http {exc.code}",
                        next_attempt_number=attempt_number + 1,
                        max_attempts=max_attempts,
                        delay_seconds=self._retry_delay_seconds(attempt_index),
                    )
                    self._sleep_before_retry(attempt_index)
                    continue
                raise error_cls(
                    f"{failure_error_type} HTTP failure: status={exc.code}. "
                    f"response={response_excerpt}"
                    f"{_build_retry_failure_suffix(attempt_number)}"
                ) from exc
            except error.URLError as exc:
                if attempt_number < max_attempts:
                    self._log_retry_attempt(
                        failure_error_type=failure_error_type,
                        reason=f"network {exc.reason}",
                        next_attempt_number=attempt_number + 1,
                        max_attempts=max_attempts,
                        delay_seconds=self._retry_delay_seconds(attempt_index),
                    )
                    self._sleep_before_retry(attempt_index)
                    continue
                raise error_cls(
                    f"{failure_error_type} network failure: reason={exc.reason}"
                    f"{_build_retry_failure_suffix(attempt_number)}"
                ) from exc
            except TimeoutError as exc:
                if attempt_number < max_attempts:
                    self._log_retry_attempt(
                        failure_error_type=failure_error_type,
                        reason="timeout",
                        next_attempt_number=attempt_number + 1,
                        max_attempts=max_attempts,
                        delay_seconds=self._retry_delay_seconds(attempt_index),
                    )
                    self._sleep_before_retry(attempt_index)
                    continue
                raise error_cls(
                    f"{failure_error_type} timeout failure"
                    f"{_build_retry_failure_suffix(attempt_number)}"
                ) from exc
            except OSError as exc:
                if attempt_number < max_attempts and _is_retryable_os_error(exc):
                    self._log_retry_attempt(
                        failure_error_type=failure_error_type,
                        reason=f"oserror {exc}",
                        next_attempt_number=attempt_number + 1,
                        max_attempts=max_attempts,
                        delay_seconds=self._retry_delay_seconds(attempt_index),
                    )
                    self._sleep_before_retry(attempt_index)
                    continue
                raise error_cls(
                    f"{failure_error_type} local I/O failure: {exc}"
                    f"{_build_retry_failure_suffix(attempt_number)}"
                ) from exc
        raise error_cls(f"{failure_error_type} request exhausted retries")

    def _sleep_before_retry(self, attempt_index: int) -> None:
        delay_seconds = self._retry_delay_seconds(attempt_index)
        if delay_seconds <= 0:
            return
        time.sleep(delay_seconds)

    def _retry_delay_seconds(self, attempt_index: int) -> float:
        return float(self.config.retry_backoff_seconds) * (2**attempt_index)

    def _log_retry_attempt(
        self,
        *,
        failure_error_type: str,
        reason: str,
        next_attempt_number: int,
        max_attempts: int,
        delay_seconds: float,
    ) -> None:
        print(
            f"[S2] voice clone {failure_error_type} failed ({reason}); "
            f"retrying {next_attempt_number}/{max_attempts} in {delay_seconds:.1f}s..."
        )

    def _parse_json_response(self, *, response_body: bytes, failure_error_type: str) -> dict[str, object]:
        try:
            loaded = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VoiceCloneResponseFormatError(
                f"{failure_error_type} response is not valid JSON."
            ) from exc
        if not isinstance(loaded, dict):
            raise VoiceCloneResponseFormatError(
                f"{failure_error_type} response must be a JSON object."
            )
        return loaded

    def _build_upload_multipart_body(self, *, file_name: str, file_bytes: bytes) -> tuple[bytes, str]:
        boundary = "----AutoDubMiniMaxVoiceCloneBoundary"
        purpose_part = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="purpose"\r\n\r\n'
            "voice_clone\r\n"
        ).encode("utf-8")
        file_header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        closing = f"\r\n--{boundary}--\r\n".encode("utf-8")
        payload = purpose_part + file_header + file_bytes + closing
        return payload, f"multipart/form-data; boundary={boundary}"

    def _build_endpoint(self, path_suffix: str) -> str:
        assert self.config.base_url is not None
        raw_base_url = self.config.base_url.strip()
        if "?" in raw_base_url:
            raw_base_url = raw_base_url.split("?", 1)[0]
        normalized_base_url = raw_base_url.rstrip("/")
        if normalized_base_url.endswith(f"/v1/{path_suffix}"):
            return normalized_base_url
        if normalized_base_url.endswith("/v1"):
            return f"{normalized_base_url}/{path_suffix}"
        return f"{normalized_base_url}/v1/{path_suffix}"


def _parse_base_resp_status(base_resp_payload: object) -> tuple[int | None, str | None]:
    if not isinstance(base_resp_payload, dict):
        return None, None
    status_code = _parse_status_code(base_resp_payload.get("status_code"))
    status_msg_raw = base_resp_payload.get("status_msg")
    status_msg = str(status_msg_raw).strip() if status_msg_raw is not None else None
    if status_msg == "":
        status_msg = None
    return status_code, status_msg


def _parse_status_code(raw_status_code: object) -> int | None:
    if isinstance(raw_status_code, bool):
        return None
    if isinstance(raw_status_code, int):
        return raw_status_code
    if isinstance(raw_status_code, str):
        normalized = raw_status_code.strip()
        if normalized and normalized.lstrip("+-").isdigit():
            try:
                return int(normalized)
            except ValueError:
                return None
    return None


def _build_requested_voice_id(speaker_id: str) -> str:
    safe_speaker_id = _build_ascii_identifier_fragment(speaker_id, fallback="speaker")
    return f"vt_{safe_speaker_id}_{int(time.time() * 1000)}"


def _build_ascii_identifier_fragment(value: str, *, fallback: str) -> str:
    normalized_value = str(value).strip()
    transliterated = (
        unicodedata.normalize("NFKD", normalized_value).encode("ascii", "ignore").decode("ascii")
    )
    collapsed = re.sub(r"[^0-9A-Za-z]+", "_", transliterated).strip("_").lower()
    if collapsed:
        return collapsed
    safe_fallback = re.sub(r"[^0-9A-Za-z]+", "_", str(fallback)).strip("_").lower() or "id"
    digest = hashlib.sha1(normalized_value.encode("utf-8")).hexdigest()[:8]
    return f"{safe_fallback}_{digest}"


def _should_retry_http_status(status_code: int) -> bool:
    return status_code in {408, 425, 429} or 500 <= status_code <= 599


def _is_retryable_os_error(exc: OSError) -> bool:
    normalized = str(exc).strip().lower()
    retryable_markers = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection broken",
        "network is unreachable",
    )
    return any(marker in normalized for marker in retryable_markers)


def _build_retry_failure_suffix(attempt_number: int) -> str:
    if attempt_number <= 1:
        return ""
    return f" after {attempt_number} attempts"


def _load_local_config_payload(config_path: Path) -> tuple[dict[str, object] | None, str | None]:
    if not config_path.exists():
        return None, None
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"Failed to load local config file {config_path}: {exc}"
    if not isinstance(loaded, dict):
        return None, f"Local config file {config_path} must contain a top-level JSON object."
    return loaded, None


def _resolve_local_config_text(
    payload: dict[str, object] | None,
    *,
    key_paths: tuple[tuple[str, ...], ...],
    config_path: Path,
) -> tuple[str | None, str | None]:
    for key_path in key_paths:
        candidate = _read_nested_mapping_value(payload, key_path)
        if isinstance(candidate, str):
            normalized = _read_optional_text(candidate)
            if normalized is not None:
                return normalized, f"config_file:{config_path}:{'.'.join(key_path)}"
    return None, None


def _resolve_local_config_bool(
    payload: dict[str, object] | None,
    *,
    key_paths: tuple[tuple[str, ...], ...],
    default: bool,
) -> bool:
    for key_path in key_paths:
        candidate = _read_nested_mapping_value(payload, key_path)
        if isinstance(candidate, bool):
            return candidate
        if isinstance(candidate, str):
            normalized = candidate.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
    return default


def _resolve_local_config_float(
    payload: dict[str, object] | None,
    *,
    key_paths: tuple[tuple[str, ...], ...],
    default: float,
) -> float:
    for key_path in key_paths:
        candidate = _read_nested_mapping_value(payload, key_path)
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, (int, float)):
            return float(candidate)
        if isinstance(candidate, str):
            try:
                return float(candidate.strip())
            except ValueError:
                continue
    return default


def _read_nested_mapping_value(
    payload: dict[str, object] | None,
    key_path: tuple[str, ...],
) -> object | None:
    candidate: object = payload
    for key in key_path:
        if not isinstance(candidate, dict):
            return None
        candidate = candidate.get(key)
    return candidate


def _resolve_env_value(candidate_keys: list[str]) -> tuple[str | None, str | None]:
    for scope_name, reader in _iter_env_readers():
        for key in candidate_keys:
            resolved_value = _read_optional_text(reader(key))
            if resolved_value is not None:
                return resolved_value, f"{scope_name}:{key}"
    return None, None


def _iter_env_readers():
    yield "process", os.getenv
    if sys.platform.startswith("win"):
        yield "user", lambda key: _read_windows_persisted_env(key, hive="user")
        yield "machine", lambda key: _read_windows_persisted_env(key, hive="machine")


class _override_config_loader_env_readers:
    def __enter__(self) -> "_override_config_loader_env_readers":
        self._original_iter_env_readers = config_loader.iter_env_readers
        config_loader.iter_env_readers = _iter_env_readers
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        config_loader.iter_env_readers = self._original_iter_env_readers
        del exc_type, exc, tb
        return False


def _read_windows_persisted_env(key: str, *, hive: str) -> str | None:
    if winreg is None:
        return None
    registry_root = winreg.HKEY_CURRENT_USER if hive == "user" else winreg.HKEY_LOCAL_MACHINE
    registry_path = (
        "Environment"
        if hive == "user"
        else r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    )
    try:
        with winreg.OpenKey(registry_root, registry_path) as env_key:
            value, _ = winreg.QueryValueEx(env_key, key)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return value if isinstance(value, str) else None


def _extract_first_string(
    payload: dict[str, object],
    *,
    key_paths: tuple[tuple[str, ...], ...],
) -> str | None:
    for key_path in key_paths:
        candidate: object = payload
        for key in key_path:
            if not isinstance(candidate, dict):
                candidate = None
                break
            candidate = candidate.get(key)
        if isinstance(candidate, str) or (
            isinstance(candidate, int) and not isinstance(candidate, bool)
        ):
            normalized = str(candidate).strip()
            if normalized:
                return normalized
    return None


def _coerce_clone_request_file_id(file_id: str) -> int | str:
    normalized = file_id.strip()
    if normalized.isdigit():
        try:
            return int(normalized)
        except ValueError:
            return normalized
    return normalized


def _summarize_config_source(*sources: str | None) -> str:
    source_families: set[str] = set()
    for source in sources:
        if source is None:
            continue
        if source.startswith("process:"):
            source_families.add("env")
            continue
        if source.startswith("user:") or source.startswith("machine:"):
            source_families.add("persisted_env")
            continue
        if source.startswith("config_file:"):
            source_families.add("autodub.local.json")
            continue
    if not source_families:
        return "default"
    if len(source_families) == 1:
        return next(iter(source_families))
    return "mixed"


def _classify_api_key_source_type(
    source: str | None,
    *,
    resolved_api_key_present: bool,
    api_key_env_var: str | None,
    api_key_env_var_source: str | None,
    direct_env_keys: tuple[str, ...],
) -> str:
    if not resolved_api_key_present:
        return "missing"
    if source is None:
        return "missing"
    if source == "direct_config" or source.startswith("config_file:"):
        return "legacy_file"
    if source.startswith("user:") or source.startswith("machine:"):
        return "persisted_env"
    if source.startswith("process:"):
        source_key = source.split(":", 1)[1]
        if (
            api_key_env_var_source is not None
            and api_key_env_var is not None
            and source_key == api_key_env_var.strip()
        ):
            return "api_key_env_var"
        normalized_direct_env_keys = {
            candidate.strip() for candidate in direct_env_keys if candidate.strip()
        }
        if source_key in normalized_direct_env_keys:
            return "direct_env"
        if api_key_env_var is not None and source_key == api_key_env_var.strip():
            return "api_key_env_var"
        return "direct_env"
    return "missing"


def _normalize_required_text(raw_value: str | None, *, field_name: str) -> str:
    normalized = _read_optional_text(raw_value)
    if normalized is None:
        raise VoiceCloneInputError(f"{field_name} is required")
    return normalized


def _parse_env_bool(raw_value: str | None, *, default: bool) -> bool:
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_env_float(raw_value: str | None, *, default: float) -> float:
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


def _read_optional_text(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    normalized = str(raw_value).strip()
    return normalized or None


def _read_http_error_excerpt(exc: error.HTTPError) -> str:
    try:
        body = exc.read()
    except OSError:
        return "<unreadable>"
    if not body:
        return "<empty>"
    try:
        decoded = body.decode("utf-8", errors="replace").strip()
    except Exception:
        return "<binary>"
    if not decoded:
        return "<empty>"
    if len(decoded) > 240:
        return f"{decoded[:240]}..."
    return decoded


def _build_object_preview(payload: object) -> str:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        encoded = repr(payload)
    if len(encoded) > 240:
        return f"{encoded[:240]}..."
    return encoded
