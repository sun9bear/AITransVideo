import base64
from dataclasses import dataclass, field
import json
from pathlib import Path
import string
from http.client import IncompleteRead
from typing import Any, Protocol, runtime_checkable
from urllib import error, request

from core.retry import build_retry_audit_payload, merge_retry_audit_payload, run_with_retry
from core.exceptions import (
    TTSConfigurationError,
    TTSInvalidAudioPayloadError,
    TTSError,
    TTSOutputFileWriteError,
    TTSProviderNetworkError,
    TTSProviderOutputError,
    TTSProviderResponseFormatError,
    TTSProviderTimeoutError,
    TTSProviderUnavailableError,
)
from core.models import SemanticBlock
from services import config_loader
from services.voice_registry import VoiceRegistry, VoiceResolver


@runtime_checkable
class TTSProvider(Protocol):
    """Replaceable TTS adapter boundary."""

    def synthesize(self, block: SemanticBlock) -> str:
        """Create an audio file path for the given semantic block."""


@dataclass(slots=True)
class RemoteTTSProviderConfig:
    provider_name: str = "remote_tts"
    voice_name: str = "placeholder_voice"


class RemoteTTSProviderSkeleton:
    """Placeholder for a future real TTS adapter."""

    def __init__(self, config: RemoteTTSProviderConfig | None = None) -> None:
        self.config = config or RemoteTTSProviderConfig()

    def synthesize(self, block: SemanticBlock) -> str:
        del block
        raise TTSError(
            f"{self.config.provider_name} skeleton is not connected in Sprint 2B."
        )


@dataclass(slots=True)
class RealTTSProviderConfig:
    enabled: bool = False
    provider_name: str = "openai_compatible_tts"
    provider_name_source: str | None = None
    tts_provider: str | None = None
    tts_provider_source: str | None = None
    platform: str | None = None
    platform_source: str | None = None
    model_name: str | None = None
    model_name_source: str | None = None
    base_url: str | None = None
    base_url_source: str | None = None
    api_key: str | None = None
    api_key_source: str | None = None
    api_key_env_var: str = "AUTODUB_TTS_API_KEY"
    api_key_env_var_source: str | None = None
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5
    voice_name: str = "alloy"
    voice_name_source: str | None = None
    voice_id: str | None = None
    voice_id_source: str | None = None
    voice_registry_path: str | None = None
    voice_registry_path_source: str | None = None
    audio_format: str = "wav"
    fallback_to_mock: bool = False
    api_protocol: str = "audio_speech_v1"
    api_protocol_source: str | None = None
    config_path: str | None = None
    config_file_error: str | None = None

    @classmethod
    def from_env(
        cls,
        prefix: str = "AUTODUB_TTS_",
        *,
        config_path: Path | None = None,
    ) -> "RealTTSProviderConfig":
        config = config_loader.load_project_local_config(config_path)
        enabled, _ = config_loader.resolve_bool_value(
            env_keys=[f"{prefix}ENABLED"],
            config=config,
            config_key_paths=(("tts", "enabled"),),
            default=False,
        )
        provider_name, provider_name_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}PROVIDER_NAME"],
            config=config,
            config_key_paths=(("tts", "provider_name"),),
        )
        tts_provider, tts_provider_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}TTS_PROVIDER"],
            config=config,
            config_key_paths=(("tts", "tts_provider"), ("voice_registry", "tts_provider")),
        )
        platform, platform_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}PLATFORM"],
            config=config,
            config_key_paths=(("tts", "platform"), ("voice_registry", "platform")),
        )
        model_name, model_name_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}MODEL_NAME"],
            config=config,
            config_key_paths=(("tts", "model_name"),),
        )
        base_url, base_url_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}BASE_URL"],
            config=config,
            config_key_paths=(("tts", "base_url"),),
        )
        api_key_env_var, api_key_env_var_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}API_KEY_ENV_VAR"],
            config=config,
            config_key_paths=(("tts", "api_key_env_var"),),
        )
        resolved_api_key_env_var = api_key_env_var or "AUTODUB_TTS_API_KEY"
        api_key, api_key_source = config_loader.resolve_text_value(
            env_keys=[
                f"{prefix}API_KEY",
                resolved_api_key_env_var,
                "AUTODUB_TTS_API_KEY",
            ],
            config=config,
            config_key_paths=(("tts", "api_key"),),
        )
        timeout_seconds, _ = config_loader.resolve_float_value(
            env_keys=[f"{prefix}TIMEOUT_SECONDS"],
            config=config,
            config_key_paths=(("tts", "timeout_seconds"),),
            default=30.0,
        )
        max_retries, _ = config_loader.resolve_int_value(
            env_keys=[f"{prefix}MAX_RETRIES"],
            config=config,
            config_key_paths=(("tts", "max_retries"),),
            default=2,
        )
        retry_backoff_seconds, _ = config_loader.resolve_float_value(
            env_keys=[f"{prefix}RETRY_BACKOFF_SECONDS"],
            config=config,
            config_key_paths=(("tts", "retry_backoff_seconds"),),
            default=0.5,
        )
        voice_name, voice_name_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}VOICE_NAME"],
            config=config,
            config_key_paths=(("tts", "voice_name"),),
        )
        voice_id, voice_id_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}VOICE_ID"],
            config=config,
            config_key_paths=(("tts", "voice_id"),),
        )
        voice_registry_path, voice_registry_path_source = config_loader.resolve_path_value(
            env_keys=[f"{prefix}VOICE_REGISTRY_PATH"],
            config=config,
            config_key_paths=(
                ("tts", "voice_registry_path"),
                ("voice_registry", "registry_path"),
                ("paths", "voice_registry_path"),
            ),
        )
        audio_format, _ = config_loader.resolve_text_value(
            env_keys=[f"{prefix}AUDIO_FORMAT"],
            config=config,
            config_key_paths=(("tts", "audio_format"),),
        )
        fallback_to_mock, _ = config_loader.resolve_bool_value(
            env_keys=[f"{prefix}FALLBACK_TO_MOCK"],
            config=config,
            config_key_paths=(("tts", "fallback_to_mock"),),
            default=False,
        )
        api_protocol, api_protocol_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}API_PROTOCOL"],
            config=config,
            config_key_paths=(("tts", "api_protocol"),),
        )
        return cls(
            enabled=enabled,
            provider_name=provider_name or "openai_compatible_tts",
            provider_name_source=provider_name_source,
            tts_provider=_read_optional_env_text(tts_provider),
            tts_provider_source=tts_provider_source,
            platform=_read_optional_env_text(platform),
            platform_source=platform_source,
            model_name=model_name,
            model_name_source=model_name_source,
            base_url=base_url,
            base_url_source=base_url_source,
            api_key=api_key,
            api_key_source=api_key_source,
            api_key_env_var=resolved_api_key_env_var,
            api_key_env_var_source=api_key_env_var_source,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            voice_name=voice_name or "alloy",
            voice_name_source=voice_name_source,
            voice_id=_read_optional_env_text(voice_id),
            voice_id_source=voice_id_source,
            voice_registry_path=_read_optional_env_text(voice_registry_path),
            voice_registry_path_source=voice_registry_path_source,
            audio_format=audio_format or "wav",
            fallback_to_mock=fallback_to_mock,
            api_protocol=api_protocol or "audio_speech_v1",
            api_protocol_source=api_protocol_source,
            config_path=str(config.path) if config.payload is not None or config.error is not None else None,
            config_file_error=config.error,
        )

    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        resolved_api_key, resolved_api_key_source = config_loader.resolve_env_text_value(
            [self.api_key_env_var, "AUTODUB_TTS_API_KEY"]
        )
        if resolved_api_key is not None:
            self.api_key_source = self.api_key_source or resolved_api_key_source
        return resolved_api_key

    def validate(self) -> None:
        normalized_protocol = self.normalized_api_protocol()
        if self.config_file_error is not None:
            raise TTSConfigurationError(self.config_file_error)
        if not self.enabled:
            raise TTSConfigurationError("Real TTS provider is disabled.")
        if not self.provider_name.strip():
            raise TTSConfigurationError("TTS provider_name is required.")
        if not self.model_name or not self.model_name.strip():
            raise TTSConfigurationError("TTS model_name is required for real provider mode.")
        if not self.base_url or not self.base_url.strip():
            raise TTSConfigurationError("TTS base_url is required for real provider mode.")
        if not self.resolved_api_key():
            raise TTSConfigurationError(
                f"TTS API key is required via config.api_key or env {self.api_key_env_var}."
            )
        if self.timeout_seconds <= 0:
            raise TTSConfigurationError("TTS timeout_seconds must be positive.")
        if self.max_retries < 0:
            raise TTSConfigurationError("TTS max_retries must be non-negative.")
        if self.retry_backoff_seconds < 0:
            raise TTSConfigurationError("TTS retry_backoff_seconds must be non-negative.")
        if self.audio_format.strip().lower() != "wav":
            raise TTSConfigurationError("TTS audio_format must be wav for the current audio pipeline.")
        if normalized_protocol == "audio_speech_v1" and not self.voice_name.strip():
            raise TTSConfigurationError("TTS voice_name is required for audio_speech_v1 real provider mode.")
        if (
            normalized_protocol == "minimax_t2a_v2"
            and not self.resolved_voice_id()
            and not self.resolved_voice_registry_path()
        ):
            raise TTSConfigurationError(
                "TTS voice resolution is required for minimax_t2a_v2 real provider mode. "
                "Set AUTODUB_TTS_VOICE_ID or AUTODUB_TTS_VOICE_REGISTRY_PATH."
            )
        if normalized_protocol not in {"audio_speech_v1", "minimax_t2a_v2"}:
            raise TTSConfigurationError(f"Unsupported TTS api_protocol: {self.api_protocol}")

    def normalized_api_protocol(self) -> str:
        return self.api_protocol.strip().lower() or "audio_speech_v1"

    def resolved_tts_provider(self) -> str:
        resolved_tts_provider = _read_optional_env_text(self.tts_provider) or _read_optional_env_text(
            self.provider_name
        )
        return (resolved_tts_provider or "openai_compatible_tts").lower()

    def resolved_platform(self) -> str | None:
        resolved_platform = _read_optional_env_text(self.platform)
        if resolved_platform is not None:
            return resolved_platform.lower()
        if self.resolved_tts_provider() == "minimax_tts":
            return "minimax_domestic"
        return None

    def resolved_voice_id(self) -> str | None:
        return _read_optional_env_text(self.voice_id)

    def resolved_voice_registry_path(self) -> str | None:
        return _read_optional_env_text(self.voice_registry_path)

    def resolved_voice_reference(self) -> str:
        return self.resolved_voice_id() or self.voice_name

    def build_diagnostic_summary(self) -> dict[str, object]:
        resolved_api_key = self.resolved_api_key()
        return {
            "provider_name_source": self.provider_name_source,
            "tts_provider": self.resolved_tts_provider(),
            "tts_provider_source": self.tts_provider_source or self.provider_name_source,
            "platform": self.resolved_platform(),
            "platform_source": self.platform_source,
            "model_name_present": self.model_name is not None,
            "model_name_source": self.model_name_source,
            "base_url_present": self.base_url is not None,
            "base_url_source": self.base_url_source,
            "api_key_present": resolved_api_key is not None,
            "api_key_source": self.api_key_source,
            "api_key_source_type": _classify_api_key_source_type(
                self.api_key_source,
                resolved_api_key_present=resolved_api_key is not None,
                api_key_env_var=self.api_key_env_var,
                api_key_env_var_source=self.api_key_env_var_source,
                direct_env_keys=("AUTODUB_TTS_API_KEY",),
            ),
            "voice_id_present": self.resolved_voice_id() is not None,
            "voice_id_source": self.voice_id_source,
            "voice_registry_path_present": self.resolved_voice_registry_path() is not None,
            "voice_registry_path_source": self.voice_registry_path_source,
            "api_protocol_source": self.api_protocol_source,
            "config_path": self.config_path,
            "config_file_error": self.config_file_error,
            "config_source": _summarize_config_source(
                self.base_url_source,
                self.api_key_source,
                self.model_name_source,
                self.voice_id_source,
                self.voice_registry_path_source,
                self.tts_provider_source,
                self.platform_source,
            ),
        }


@dataclass(slots=True)
class TTSProviderSelectionConfig:
    mode: str = "mock"
    real: RealTTSProviderConfig = field(default_factory=RealTTSProviderConfig)
    mode_source: str | None = None

    @classmethod
    def from_env(
        cls,
        prefix: str = "AUTODUB_TTS_",
        *,
        config_path: Path | None = None,
    ) -> "TTSProviderSelectionConfig":
        config = config_loader.load_project_local_config(config_path)
        mode, mode_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}MODE"],
            config=config,
            config_key_paths=(("tts", "mode"),),
        )
        return cls(
            mode=(mode or "mock").strip().lower() or "mock",
            real=RealTTSProviderConfig.from_env(prefix=prefix, config_path=config_path),
            mode_source=mode_source,
        )


@dataclass(slots=True)
class TTSProviderBinding:
    provider: TTSProvider
    provider_name: str
    model_name: str | None
    voice_name: str
    mode: str
    fallback_applied: bool = False
    fallback_reason: str | None = None
    fallback_stage: str | None = None
    version_context: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ResolvedTTSVoice:
    resolved: bool
    voice_name: str
    source: str
    voice_id: str | None = None
    voice_type: str | None = None
    provider: str | None = None
    tts_provider: str | None = None
    platform: str | None = None
    label: str | None = None


class OpenAICompatibleTTSProvider:
    """Minimal real TTS adapter for OpenAI-compatible speech endpoints."""

    def __init__(self, output_dir: str, config: RealTTSProviderConfig) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.config.validate()
        self.voice_resolver = _build_voice_resolver(self.config)
        self._retry_report = build_retry_audit_payload()

    def synthesize(self, block: SemanticBlock) -> str:
        clean_text = block.get_preferred_cn_text_for_tts().strip()
        if not clean_text:
            self._retry_report = build_retry_audit_payload()
            raise TTSProviderOutputError("Cannot synthesize empty block text.")
        resolved_voice = self.resolve_block_voice(block)
        if not resolved_voice.resolved:
            self._retry_report = build_retry_audit_payload()
            raise TTSConfigurationError(
                "No TTS voice could be resolved for "
                f"speaker_id={block.speaker_id}. Checked speaker voice registry and AUTODUB_TTS_VOICE_ID."
            )

        try:
            synthesized_path, retry_report = run_with_retry(
                lambda: self._synthesize_once(block, resolved_voice),
                classify_tts_error,
                max_retries=self.config.max_retries,
                backoff_seconds=self.config.retry_backoff_seconds,
                should_retry_error=_is_tts_retry_error,
            )
        except Exception as exc:
            self._retry_report = merge_retry_audit_payload(
                self._retry_report,
                getattr(exc, "retry_report", None),
            )
            raise

        self._retry_report = merge_retry_audit_payload(self._retry_report, retry_report)
        return synthesized_path

    def get_cache_context(self) -> dict[str, object]:
        normalized_protocol = self.config.normalized_api_protocol()
        context = {
            "provider_mode": "real",
            "api_protocol": normalized_protocol,
            "tts_provider": self.config.resolved_tts_provider(),
            "provider_variant": (
                "minimax_tts_v1" if normalized_protocol == "minimax_t2a_v2" else "openai_compatible_tts_v1"
            ),
            "audio_format": self.config.audio_format,
            "voice_id": self.config.resolved_voice_id(),
            "voice_registry_path": self.config.resolved_voice_registry_path(),
            "voice_resolution_strategy": (
                "speaker_registry_then_env_fallback"
                if self.config.resolved_voice_registry_path()
                else "env_voice_id_only"
            )
            if normalized_protocol == "minimax_t2a_v2"
            else "provider_default",
            "config_source": self.config.build_diagnostic_summary()["config_source"],
        }
        if self.config.resolved_platform() is not None:
            context["platform"] = self.config.resolved_platform()
        return context

    def get_block_runtime_context(self, block: SemanticBlock) -> dict[str, object]:
        resolved_voice = self.resolve_block_voice(block)
        version_context = self.get_cache_context()
        if resolved_voice.voice_id is not None:
            version_context["voice_id"] = resolved_voice.voice_id
        return {
            "voice_name": resolved_voice.voice_name,
            "resolved_voice_id": resolved_voice.voice_id,
            "voice_resolution_source": resolved_voice.source,
            "version_context": version_context,
        }

    def get_retry_report(self) -> dict[str, object]:
        return dict(self._retry_report)

    def reset_retry_report(self) -> None:
        self._retry_report = build_retry_audit_payload()

    def resolve_block_voice(self, block: SemanticBlock) -> ResolvedTTSVoice:
        normalized_protocol = self.config.normalized_api_protocol()
        if normalized_protocol != "minimax_t2a_v2":
            return ResolvedTTSVoice(
                resolved=True,
                voice_name=self.config.voice_name,
                source="provider_default",
                voice_id=self.config.resolved_voice_id(),
                tts_provider=self.config.resolved_tts_provider(),
                platform=self.config.resolved_platform(),
            )

        if self.voice_resolver is not None and block.speaker_id.strip():
            resolution = self.voice_resolver.resolve(
                block.speaker_id,
                tts_provider=self.config.resolved_tts_provider(),
                platform=self.config.resolved_platform(),
            )
            if resolution.resolved and resolution.voice_id:
                return ResolvedTTSVoice(
                    resolved=True,
                    voice_name=resolution.voice_id,
                    voice_id=resolution.voice_id,
                    source=resolution.source,
                    voice_type=resolution.voice_type,
                    provider=resolution.provider,
                    tts_provider=resolution.tts_provider,
                    platform=resolution.platform,
                    label=resolution.label,
                )

        resolved_voice_id = self.config.resolved_voice_id()
        if resolved_voice_id is not None:
            return ResolvedTTSVoice(
                resolved=True,
                voice_name=resolved_voice_id,
                voice_id=resolved_voice_id,
                source="env_fallback",
                tts_provider=self.config.resolved_tts_provider(),
                platform=self.config.resolved_platform(),
            )

        return ResolvedTTSVoice(
            resolved=False,
            voice_name=self.config.voice_name,
            voice_id=None,
            source="unresolved",
        )

    def _synthesize_once(self, block: SemanticBlock, resolved_voice: ResolvedTTSVoice) -> str:
        response_content, content_type = self._post_tts_request(
            self._build_request_payload(block, resolved_voice)
        )
        wav_bytes = self._extract_wav_bytes(response_content, content_type)
        if not wav_bytes.startswith(b"RIFF"):
            raise TTSInvalidAudioPayloadError("TTS provider output is not a wav payload.")

        output_path = self.output_dir / f"{block.block_id}_r{block.rewrite_count}.wav"
        try:
            output_path.write_bytes(wav_bytes)
        except OSError as exc:
            raise TTSOutputFileWriteError(f"Failed to write TTS audio output: {output_path}") from exc
        return str(output_path)

    def _build_request_payload(
        self,
        block: SemanticBlock,
        resolved_voice: ResolvedTTSVoice,
    ) -> dict[str, object]:
        clean_text = block.get_preferred_cn_text_for_tts().strip()
        normalized_protocol = self.config.normalized_api_protocol()
        if normalized_protocol == "minimax_t2a_v2":
            return {
                "model": self.config.model_name,
                "text": clean_text,
                "stream": False,
                "output_format": "hex",
                "voice_setting": {
                    "voice_id": resolved_voice.voice_id,
                },
                "audio_setting": {
                    "format": self.config.audio_format,
                    "channel": 1,
                },
                "metadata": {
                    "block_id": block.block_id,
                    "speaker_id": block.speaker_id,
                    "speaker_name": block.speaker_name,
                },
            }
        return {
            "model": self.config.model_name,
            "voice": resolved_voice.voice_name,
            "input": clean_text,
            "format": self.config.audio_format,
            "metadata": {
                "block_id": block.block_id,
                "speaker_id": block.speaker_id,
                "speaker_name": block.speaker_name,
            },
        }

    def _post_tts_request(self, payload: dict[str, object]) -> tuple[bytes, str]:
        endpoint = self._build_endpoint()
        serialized_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.resolved_api_key()}",
            "Content-Type": "application/json",
        }
        request_obj = request.Request(endpoint, data=serialized_payload, headers=headers, method="POST")

        try:
            with request.urlopen(request_obj, timeout=self.config.timeout_seconds) as response:
                try:
                    response_body = response.read()
                except IncompleteRead as exc:
                    response_body = exc.partial
                content_type = response.headers.get("Content-Type", "")
        except error.HTTPError as exc:
            raise TTSProviderUnavailableError(
                f"TTS provider HTTP error: status={exc.code} provider={self.config.provider_name}"
            ) from exc
        except error.URLError as exc:
            if _looks_like_timeout_error(exc.reason):
                raise TTSProviderTimeoutError(
                    f"TTS provider timeout: provider={self.config.provider_name}"
                ) from exc
            raise TTSProviderNetworkError(
                f"TTS provider network-like failure: provider={self.config.provider_name} reason={exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise TTSProviderTimeoutError(
                f"TTS provider timeout: provider={self.config.provider_name}"
            ) from exc
        except OSError as exc:
            if _looks_like_timeout_error(exc):
                raise TTSProviderTimeoutError(
                    f"TTS provider timeout: provider={self.config.provider_name}"
                ) from exc
            raise TTSProviderNetworkError(
                f"TTS provider network-like failure: provider={self.config.provider_name}"
            ) from exc

        return response_body, content_type

    def _build_endpoint(self) -> str:
        assert self.config.base_url is not None
        normalized_protocol = self.config.normalized_api_protocol()
        raw_base_url = self.config.base_url.strip()
        if normalized_protocol == "audio_speech_v1":
            return f"{raw_base_url.rstrip('/')}/audio/speech"
        if normalized_protocol == "minimax_t2a_v2":
            if raw_base_url.rstrip("/").endswith("/v1/t2a_v2"):
                return raw_base_url.rstrip("/")
            if raw_base_url.rstrip("/").endswith("/v1"):
                return _append_path_segment(raw_base_url, "t2a_v2")
            return _append_path_segment(_append_path_segment(raw_base_url, "v1"), "t2a_v2")
        raise TTSConfigurationError(f"Unsupported TTS api_protocol: {self.config.api_protocol}")

    def _extract_wav_bytes(self, response_content: bytes, content_type: str) -> bytes:
        normalized_content_type = content_type.lower()
        if "application/json" in normalized_content_type or response_content.startswith(b"{"):
            return self._extract_wav_bytes_from_json(response_content)
        return response_content

    def _extract_wav_bytes_from_json(self, response_content: bytes) -> bytes:
        try:
            loaded = json.loads(response_content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TTSProviderResponseFormatError("TTS provider JSON response is invalid.") from exc
        if not isinstance(loaded, dict):
            raise TTSProviderResponseFormatError("TTS provider JSON response must be an object.")

        prefer_hex_audio = self.config.normalized_api_protocol() == "minimax_t2a_v2"
        for key in ("audio_base64", "audio", "data"):
            candidate = loaded.get(key)
            decoded = self._decode_audio_candidate(
                candidate,
                field_name=key,
                prefer_hex_audio=prefer_hex_audio,
            )
            if decoded is not None:
                return decoded
        raise TTSInvalidAudioPayloadError("TTS provider JSON response does not contain audio payload.")

    def _decode_audio_candidate(
        self,
        candidate: object,
        *,
        field_name: str | None = None,
        prefer_hex_audio: bool = False,
    ) -> bytes | None:
        if isinstance(candidate, str) and candidate.strip():
            normalized_candidate = candidate.strip()
            if prefer_hex_audio and field_name == "audio":
                try:
                    return bytes.fromhex(normalized_candidate)
                except ValueError as exc:
                    raise TTSInvalidAudioPayloadError("TTS provider audio payload hex is invalid.") from exc
            if _looks_like_hex_audio_string(normalized_candidate):
                try:
                    return bytes.fromhex(normalized_candidate)
                except ValueError as exc:
                    raise TTSInvalidAudioPayloadError("TTS provider audio payload hex is invalid.") from exc
            try:
                return base64.b64decode(normalized_candidate)
            except (ValueError, OSError) as exc:
                raise TTSInvalidAudioPayloadError("TTS provider audio payload is not valid base64.") from exc
        if isinstance(candidate, dict):
            for key in ("audio_base64", "audio", "b64_json", "content", "data"):
                nested = candidate.get(key)
                decoded = self._decode_audio_candidate(
                    nested,
                    field_name=key,
                    prefer_hex_audio=prefer_hex_audio,
                )
                if decoded is not None:
                    return decoded
        return None


def resolve_tts_provider(
    selection: TTSProviderSelectionConfig,
    mock_provider: TTSProvider,
    output_dir: str,
) -> TTSProviderBinding:
    mode = selection.mode.strip().lower()
    if mode == "mock":
        return TTSProviderBinding(
            provider=mock_provider,
            provider_name="mock_tts",
            model_name=None,
            voice_name=selection.real.resolved_voice_reference(),
            mode="mock",
            version_context=_get_provider_cache_context(mock_provider),
        )
    if mode != "real":
        raise TTSConfigurationError(f"Unsupported TTS mode: {selection.mode}")

    try:
        provider = OpenAICompatibleTTSProvider(output_dir=output_dir, config=selection.real)
    except TTSConfigurationError as exc:
        if not selection.real.fallback_to_mock:
            raise
        return TTSProviderBinding(
            provider=mock_provider,
            provider_name="mock_tts",
            model_name=None,
            voice_name=selection.real.resolved_voice_reference(),
            mode="mock_fallback",
            fallback_applied=True,
            fallback_reason=str(exc),
            fallback_stage="configuration",
            version_context=_get_provider_cache_context(mock_provider),
        )

    # Sprint 5B keeps TTS runtime failures as clear stage failures; runtime fallback is not implemented.
    return TTSProviderBinding(
        provider=provider,
        provider_name=selection.real.provider_name,
        model_name=selection.real.model_name,
        voice_name=selection.real.resolved_voice_reference(),
        mode="real",
        version_context=_get_provider_cache_context(provider),
    )


def classify_tts_error(exc: Exception) -> dict[str, object]:
    if isinstance(exc, TTSConfigurationError):
        return {"error_type": "configuration_error", "retry_candidate": False}
    if isinstance(exc, TTSProviderTimeoutError):
        return {"error_type": "provider_timeout", "retry_candidate": True}
    if isinstance(exc, TTSProviderNetworkError):
        return {"error_type": "provider_network_error", "retry_candidate": True}
    if isinstance(exc, TTSProviderUnavailableError):
        return {"error_type": "provider_unavailable", "retry_candidate": False}
    if isinstance(exc, TTSProviderResponseFormatError):
        return {"error_type": "invalid_provider_response_format", "retry_candidate": False}
    if isinstance(exc, TTSInvalidAudioPayloadError):
        return {"error_type": "invalid_audio_payload", "retry_candidate": False}
    if isinstance(exc, TTSOutputFileWriteError):
        return {"error_type": "output_file_write_failure", "retry_candidate": False}
    if isinstance(exc, TTSProviderOutputError):
        return {"error_type": "invalid_provider_output", "retry_candidate": False}
    return {"error_type": "tts_error", "retry_candidate": False}


def build_tts_block_runtime_context(
    provider: TTSProvider,
    block: SemanticBlock,
    *,
    default_voice_name: str,
    default_version_context: dict[str, object],
) -> dict[str, object]:
    context_getter = getattr(provider, "get_block_runtime_context", None)
    if callable(context_getter):
        context = context_getter(block)
        if isinstance(context, dict):
            version_context = context.get("version_context")
            resolved_voice_name = _read_optional_context_text(context.get("voice_name")) or default_voice_name
            resolved_voice_id = _read_optional_context_text(context.get("resolved_voice_id"))
            voice_resolution_source = _read_optional_context_text(context.get("voice_resolution_source"))
            merged_version_context = (
                dict(version_context)
                if isinstance(version_context, dict)
                else dict(default_version_context)
            )
            if resolved_voice_id is not None and "voice_id" not in merged_version_context:
                merged_version_context["voice_id"] = resolved_voice_id
            return {
                "voice_name": resolved_voice_name,
                "resolved_voice_id": resolved_voice_id,
                "voice_resolution_source": voice_resolution_source,
                "version_context": merged_version_context,
            }

    resolved_voice_id = _read_optional_context_text(default_version_context.get("voice_id"))
    voice_resolution_source = _read_optional_context_text(default_version_context.get("voice_resolution_source"))
    if voice_resolution_source is None and resolved_voice_id is not None:
        voice_resolution_source = "env_fallback"
    return {
        "voice_name": default_voice_name,
        "resolved_voice_id": resolved_voice_id,
        "voice_resolution_source": voice_resolution_source,
        "version_context": dict(default_version_context),
    }


def _get_provider_cache_context(provider: TTSProvider) -> dict[str, object]:
    context_getter = getattr(provider, "get_cache_context", None)
    if callable(context_getter):
        context = context_getter()
        if isinstance(context, dict):
            return context
    return {}


def _summarize_config_source(*sources: str | None) -> str:
    source_families = {
        family
        for family in (config_loader.summarize_source_family(source) for source in sources)
        if family is not None and family != "default"
    }
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


def _parse_env_bool(raw_value: str | None, default: bool) -> bool:
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_env_float(raw_value: str | None, default: float) -> float:
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


def _parse_env_int(raw_value: str | None, default: int) -> int:
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _read_optional_env_text(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    stripped = raw_value.strip()
    return stripped or None


def _read_optional_context_text(raw_value: object) -> str | None:
    if not isinstance(raw_value, str):
        return None
    stripped = raw_value.strip()
    return stripped or None


def _append_path_segment(base_url: str, segment: str) -> str:
    if "?" not in base_url:
        return base_url if base_url.rstrip("/").endswith(f"/{segment}") else f"{base_url.rstrip('/')}/{segment}"
    base_path, query = base_url.split("?", 1)
    resolved_base_path = (
        base_path if base_path.rstrip("/").endswith(f"/{segment}") else f"{base_path.rstrip('/')}/{segment}"
    )
    return f"{resolved_base_path}?{query}"


def _looks_like_hex_audio_string(candidate: str) -> bool:
    if len(candidate) < 8 or len(candidate) % 2 != 0:
        return False
    return all(character in string.hexdigits for character in candidate)


def _looks_like_timeout_error(reason: object) -> bool:
    if isinstance(reason, TimeoutError):
        return True
    normalized_message = str(reason).strip().lower()
    if not normalized_message:
        return False
    return "timed out" in normalized_message or "timeout" in normalized_message


def _is_tts_retry_error(exc: Exception, error_info: dict[str, object]) -> bool:
    del exc
    return error_info.get("error_type") in {"provider_timeout", "provider_network_error"}


def _build_voice_resolver(config: RealTTSProviderConfig) -> VoiceResolver | None:
    registry_path = config.resolved_voice_registry_path()
    if registry_path is None:
        return None
    return VoiceResolver(VoiceRegistry(registry_path))
