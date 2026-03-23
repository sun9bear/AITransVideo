from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib import error, request

from core.retry import build_retry_audit_payload, merge_retry_audit_payload, run_with_retry
from core.exceptions import (
    TranslationConfigurationError,
    TranslationError,
    TranslationProviderLineCountError,
    TranslationProviderOutputError,
    TranslationProviderResponseFormatError,
    TranslationProviderUnavailableError,
)
from core.models import SubtitleLine
from modules.translation.validators import validate_translated_line_count
from services import config_loader


@runtime_checkable
class TranslationProvider(Protocol):
    """Replaceable translation adapter boundary."""

    def translate_batch(self, lines: list[SubtitleLine]) -> list[str]:
        """Translate one batch into Chinese lines, preserving order."""


@dataclass(slots=True)
class RemoteTranslationProviderConfig:
    provider_name: str = "remote_translation"
    model_name: str = "placeholder_model"
    target_language: str = "zh-CN"


class RemoteTranslationProviderSkeleton:
    """Placeholder for a future real translation adapter."""

    def __init__(self, config: RemoteTranslationProviderConfig | None = None) -> None:
        self.config = config or RemoteTranslationProviderConfig()

    def translate_batch(self, lines: list[SubtitleLine]) -> list[str]:
        del lines
        raise TranslationError(
            f"{self.config.provider_name} skeleton is not connected in Sprint 2B."
        )


@dataclass(slots=True)
class RealTranslationProviderConfig:
    enabled: bool = False
    provider_name: str = "openai_compatible"
    provider_name_source: str | None = None
    model_name: str | None = None
    model_name_source: str | None = None
    target_language: str = "zh-CN"
    target_language_source: str | None = None
    base_url: str | None = None
    base_url_source: str | None = None
    api_key: str | None = None
    api_key_source: str | None = None
    api_key_env_var: str = "AUTODUB_TRANSLATION_API_KEY"
    api_key_env_var_source: str | None = None
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5
    fallback_to_mock: bool = False
    runtime_fallback_to_mock: bool = False
    api_protocol: str = "chat_completions_v1"
    provider_variant: str = "openai_compatible_translation_v2"
    config_path: str | None = None
    config_file_error: str | None = None

    @classmethod
    def from_env(
        cls,
        prefix: str = "AUTODUB_TRANSLATION_",
        *,
        config_path: Path | None = None,
    ) -> "RealTranslationProviderConfig":
        config = config_loader.load_project_local_config(config_path)
        enabled, _ = config_loader.resolve_bool_value(
            env_keys=[f"{prefix}ENABLED"],
            config=config,
            config_key_paths=(("translation", "enabled"),),
            default=False,
        )
        provider_name, provider_name_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}PROVIDER_NAME"],
            config=config,
            config_key_paths=(("translation", "provider_name"),),
        )
        model_name, model_name_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}MODEL_NAME"],
            config=config,
            config_key_paths=(("translation", "model_name"),),
        )
        target_language, target_language_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}TARGET_LANGUAGE"],
            config=config,
            config_key_paths=(("translation", "target_language"),),
        )
        base_url, base_url_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}BASE_URL"],
            config=config,
            config_key_paths=(("translation", "base_url"),),
        )
        api_key_env_var, api_key_env_var_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}API_KEY_ENV_VAR"],
            config=config,
            config_key_paths=(("translation", "api_key_env_var"),),
        )
        resolved_api_key_env_var = api_key_env_var or "AUTODUB_TRANSLATION_API_KEY"
        api_key, api_key_source = config_loader.resolve_text_value(
            env_keys=[
                f"{prefix}API_KEY",
                resolved_api_key_env_var,
                "AUTODUB_TRANSLATION_API_KEY",
            ],
            config=config,
            config_key_paths=(("translation", "api_key"),),
        )
        timeout_seconds, _ = config_loader.resolve_float_value(
            env_keys=[f"{prefix}TIMEOUT_SECONDS"],
            config=config,
            config_key_paths=(("translation", "timeout_seconds"),),
            default=30.0,
        )
        max_retries, _ = config_loader.resolve_int_value(
            env_keys=[f"{prefix}MAX_RETRIES"],
            config=config,
            config_key_paths=(("translation", "max_retries"),),
            default=2,
        )
        retry_backoff_seconds, _ = config_loader.resolve_float_value(
            env_keys=[f"{prefix}RETRY_BACKOFF_SECONDS"],
            config=config,
            config_key_paths=(("translation", "retry_backoff_seconds"),),
            default=0.5,
        )
        fallback_to_mock, _ = config_loader.resolve_bool_value(
            env_keys=[f"{prefix}FALLBACK_TO_MOCK"],
            config=config,
            config_key_paths=(("translation", "fallback_to_mock"),),
            default=False,
        )
        runtime_fallback_to_mock, _ = config_loader.resolve_bool_value(
            env_keys=[f"{prefix}RUNTIME_FALLBACK_TO_MOCK"],
            config=config,
            config_key_paths=(("translation", "runtime_fallback_to_mock"),),
            default=False,
        )
        api_protocol, _ = config_loader.resolve_text_value(
            env_keys=[f"{prefix}API_PROTOCOL"],
            config=config,
            config_key_paths=(("translation", "api_protocol"),),
        )
        provider_variant, _ = config_loader.resolve_text_value(
            env_keys=[f"{prefix}PROVIDER_VARIANT"],
            config=config,
            config_key_paths=(("translation", "provider_variant"),),
        )
        return cls(
            enabled=enabled,
            provider_name=provider_name or "openai_compatible",
            provider_name_source=provider_name_source,
            model_name=model_name,
            model_name_source=model_name_source,
            target_language=target_language or "zh-CN",
            target_language_source=target_language_source,
            base_url=base_url,
            base_url_source=base_url_source,
            api_key=api_key,
            api_key_source=api_key_source,
            api_key_env_var=resolved_api_key_env_var,
            api_key_env_var_source=api_key_env_var_source,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            fallback_to_mock=fallback_to_mock,
            runtime_fallback_to_mock=runtime_fallback_to_mock,
            api_protocol=api_protocol or "chat_completions_v1",
            provider_variant=provider_variant or "openai_compatible_translation_v2",
            config_path=str(config.path) if config.payload is not None or config.error is not None else None,
            config_file_error=config.error,
        )

    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        resolved_api_key, resolved_api_key_source = config_loader.resolve_env_text_value(
            [self.api_key_env_var, "AUTODUB_TRANSLATION_API_KEY"]
        )
        if resolved_api_key is not None:
            self.api_key_source = self.api_key_source or resolved_api_key_source
        return resolved_api_key

    def validate(self) -> None:
        if self.config_file_error is not None:
            raise TranslationConfigurationError(self.config_file_error)
        if not self.enabled:
            raise TranslationConfigurationError("Real translation provider is disabled.")
        if not self.provider_name.strip():
            raise TranslationConfigurationError("Translation provider_name is required.")
        if not self.model_name or not self.model_name.strip():
            raise TranslationConfigurationError("Translation model_name is required for real provider mode.")
        if not self.base_url or not self.base_url.strip():
            raise TranslationConfigurationError("Translation base_url is required for real provider mode.")
        if not self.resolved_api_key():
            raise TranslationConfigurationError(
                f"Translation API key is required via config.api_key or env {self.api_key_env_var}."
            )
        if not self.provider_variant.strip():
            raise TranslationConfigurationError("Translation provider_variant is required for real provider mode.")
        if self.timeout_seconds <= 0:
            raise TranslationConfigurationError("Translation timeout_seconds must be positive.")
        if self.max_retries < 0:
            raise TranslationConfigurationError("Translation max_retries must be non-negative.")
        if self.retry_backoff_seconds < 0:
            raise TranslationConfigurationError("Translation retry_backoff_seconds must be non-negative.")

    def build_diagnostic_summary(self) -> dict[str, object]:
        resolved_api_key = self.resolved_api_key()
        return {
            "provider_name_source": self.provider_name_source,
            "model_name_present": self.model_name is not None,
            "model_name_source": self.model_name_source,
            "target_language_source": self.target_language_source,
            "base_url_present": self.base_url is not None,
            "base_url_source": self.base_url_source,
            "api_key_present": resolved_api_key is not None,
            "api_key_source": self.api_key_source,
            "api_key_source_type": _classify_api_key_source_type(
                self.api_key_source,
                resolved_api_key_present=resolved_api_key is not None,
                api_key_env_var=self.api_key_env_var,
                api_key_env_var_source=self.api_key_env_var_source,
                direct_env_keys=("AUTODUB_TRANSLATION_API_KEY",),
            ),
            "config_path": self.config_path,
            "config_file_error": self.config_file_error,
            "config_source": _summarize_config_source(
                self.base_url_source,
                self.api_key_source,
                self.model_name_source,
                self.provider_name_source,
            ),
        }


@dataclass(slots=True)
class TranslationProviderSelectionConfig:
    mode: str = "mock"
    real: RealTranslationProviderConfig = field(default_factory=RealTranslationProviderConfig)
    mode_source: str | None = None

    @classmethod
    def from_env(
        cls,
        prefix: str = "AUTODUB_TRANSLATION_",
        *,
        config_path: Path | None = None,
    ) -> "TranslationProviderSelectionConfig":
        config = config_loader.load_project_local_config(config_path)
        mode, mode_source = config_loader.resolve_text_value(
            env_keys=[f"{prefix}MODE"],
            config=config,
            config_key_paths=(("translation", "mode"),),
        )
        return cls(
            mode=(mode or "mock").strip().lower() or "mock",
            real=RealTranslationProviderConfig.from_env(prefix=prefix, config_path=config_path),
            mode_source=mode_source,
        )


@dataclass(slots=True)
class TranslationProviderBinding:
    provider: TranslationProvider
    provider_name: str
    model_name: str | None
    target_language: str
    mode: str
    fallback_applied: bool = False
    fallback_reason: str | None = None
    fallback_stage: str | None = None
    runtime_fallback_enabled: bool = False
    fallback_provider: TranslationProvider | None = None
    fallback_from: str | None = None
    fallback_to: str | None = None
    version_context: dict[str, object] = field(default_factory=dict)


class OpenAICompatibleTranslationProvider:
    """Minimal real translation adapter for OpenAI-compatible chat completion endpoints."""

    def __init__(self, config: RealTranslationProviderConfig) -> None:
        self.config = config
        self.config.validate()
        self._retry_report = build_retry_audit_payload()

    def translate_batch(self, lines: list[SubtitleLine]) -> list[str]:
        if not lines:
            self._retry_report = build_retry_audit_payload()
            return []

        try:
            translated_lines, retry_report = run_with_retry(
                lambda: self._translate_batch_once(lines),
                classify_translation_error,
                max_retries=self.config.max_retries,
                backoff_seconds=self.config.retry_backoff_seconds,
            )
        except Exception as exc:
            self._retry_report = merge_retry_audit_payload(
                self._retry_report,
                getattr(exc, "retry_report", None),
            )
            raise

        self._retry_report = merge_retry_audit_payload(self._retry_report, retry_report)
        return translated_lines

    def get_cache_context(self) -> dict[str, Any]:
        return {
            "provider_mode": "real",
            "api_protocol": self.config.api_protocol,
            "provider_variant": self.config.provider_variant,
            "request_contract": "subtitle_line_batch_v2",
            "output_contract": "translated_lines_line_level_v2",
            "line_count_policy": "strict_match_required",
            "parser_version": "openai_compatible_translation_parser_v2",
            "config_source": self.config.build_diagnostic_summary()["config_source"],
        }

    def get_retry_report(self) -> dict[str, object]:
        return dict(self._retry_report)

    def reset_retry_report(self) -> None:
        self._retry_report = build_retry_audit_payload()

    def _translate_batch_once(self, lines: list[SubtitleLine]) -> list[str]:
        response_payload = self._post_chat_completion(self._build_request_payload(lines))
        assistant_content = self._extract_assistant_content(response_payload)
        return self._parse_translation_list(assistant_content, expected_lines=lines)

    def _build_request_payload(self, lines: list[SubtitleLine]) -> dict[str, object]:
        source_lines = [
            {
                "index": line.index,
                "text": line.en_text.strip(),
            }
            for line in lines
        ]
        return {
            "model": self.config.model_name,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You translate subtitle line batches into target language text. "
                        "The input is a line batch, not a free-form document. "
                        "Preserve exact line count and original order. "
                        "Translate each line independently. "
                        "Return JSON only with key translated_lines. "
                        "Prefer translated_lines as an array of objects with keys index and text. "
                        "Do not summarize. Do not omit. Do not merge or split lines. "
                        "Do not add speaker names, speaker IDs, role prefixes, markdown, or explanations."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "target_language": self.config.target_language,
                            "line_count": len(source_lines),
                            "translated_lines_key": "translated_lines",
                            "input_contract": {
                                "type": "line_batch",
                                "preserve_line_order": True,
                                "preserve_line_count": True,
                            },
                            "output_contract": {
                                "preferred_item_shape": {"index": "same as input", "text": "translated line"},
                                "compatible_item_shape": "string",
                            },
                            "lines": source_lines,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }

    def _post_chat_completion(self, payload: dict[str, object]) -> dict[str, object]:
        endpoint = f"{self.config.base_url.rstrip('/')}/chat/completions"
        serialized_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.resolved_api_key()}",
            "Content-Type": "application/json",
        }
        request_obj = request.Request(endpoint, data=serialized_payload, headers=headers, method="POST")

        try:
            with request.urlopen(request_obj, timeout=self.config.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            raise TranslationProviderUnavailableError(
                f"Translation provider HTTP error: status={exc.code} provider={self.config.provider_name}"
            ) from exc
        except error.URLError as exc:
            raise TranslationProviderUnavailableError(
                f"Translation provider unavailable: provider={self.config.provider_name} reason={exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise TranslationProviderUnavailableError(
                f"Translation provider timeout: provider={self.config.provider_name}"
            ) from exc
        except OSError as exc:
            raise TranslationProviderUnavailableError(
                f"Translation provider network-like failure: provider={self.config.provider_name}"
            ) from exc

        try:
            loaded = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise TranslationProviderResponseFormatError("Translation provider returned non-JSON HTTP body.") from exc
        if not isinstance(loaded, dict):
            raise TranslationProviderResponseFormatError(
                "Translation provider HTTP body must decode to a JSON object."
            )
        return loaded

    def _extract_assistant_content(self, payload: dict[str, object]) -> str:
        choices = payload.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise TranslationProviderResponseFormatError("Translation provider response is missing choices.")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise TranslationProviderResponseFormatError("Translation provider choice payload is invalid.")
        message = first_choice.get("message", {})
        if not isinstance(message, dict):
            raise TranslationProviderResponseFormatError("Translation provider response is missing message content.")

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, dict):
            extracted_dict_text = self._extract_text_from_content_dict(content)
            if extracted_dict_text is not None:
                return extracted_dict_text
            return json.dumps(content, ensure_ascii=False)
        if isinstance(content, list):
            extracted_parts = self._extract_content_parts(content)
            if extracted_parts:
                return extracted_parts

        output_text = first_choice.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()
        raise TranslationProviderResponseFormatError("Translation provider returned empty message content.")

    def _parse_translation_list(
        self,
        content: str,
        *,
        expected_lines: list[SubtitleLine] | None = None,
    ) -> list[str]:
        loaded = self._load_json_content(content)
        translated_lines = self._locate_translated_lines(loaded)
        normalized_lines = self._normalize_translated_lines(translated_lines, expected_lines=expected_lines)
        if expected_lines is not None:
            validate_translated_line_count(expected_lines, normalized_lines)
        return normalized_lines

    def _extract_content_parts(self, content_parts: list[object]) -> str:
        extracted_texts: list[str] = []
        for part in content_parts:
            if isinstance(part, dict):
                text_value = part.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    extracted_texts.append(text_value.strip())
                    continue
                if isinstance(text_value, dict):
                    nested_value = text_value.get("value")
                    if isinstance(nested_value, str) and nested_value.strip():
                        extracted_texts.append(nested_value.strip())
                        continue
                if part.get("type") == "text":
                    nested_text = part.get("content")
                    if isinstance(nested_text, str) and nested_text.strip():
                        extracted_texts.append(nested_text.strip())
                        continue
                    if isinstance(nested_text, dict):
                        nested_value = nested_text.get("value")
                        if isinstance(nested_value, str) and nested_value.strip():
                            extracted_texts.append(nested_value.strip())
                            continue
                json_value = part.get("json")
                if isinstance(json_value, dict):
                    extracted_texts.append(json.dumps(json_value, ensure_ascii=False))
                    continue
        return "\n".join(extracted_texts).strip()

    def _load_json_content(self, content: str) -> object:
        normalized_content = _strip_outer_code_fence(content)
        if normalized_content[:1] not in {'{', '[', '"'}:
            raise TranslationProviderOutputError(
                "Translation provider content must be JSON only without explanatory prefix."
            )
        try:
            loaded = json.loads(normalized_content)
        except json.JSONDecodeError as exc:
            raise TranslationProviderOutputError("Translation provider content is not valid JSON.") from exc

        if isinstance(loaded, str):
            nested_content = _strip_outer_code_fence(loaded)
            try:
                loaded = json.loads(nested_content)
            except json.JSONDecodeError as exc:
                raise TranslationProviderOutputError(
                    "Translation provider content wraps JSON in an invalid string payload."
                ) from exc
        return loaded

    def _locate_translated_lines(self, loaded: object) -> object:
        if isinstance(loaded, list):
            return loaded
        if not isinstance(loaded, dict):
            raise TranslationProviderOutputError("Translation provider JSON content must be an object or list.")

        resolved_root = self._resolve_candidate_list(loaded)
        if resolved_root is not None:
            return resolved_root
        raise TranslationProviderOutputError("Translation provider output must contain translated_lines list.")

    def _resolve_candidate_list(self, candidate: object) -> object | None:
        if isinstance(candidate, list):
            return candidate
        if isinstance(candidate, dict):
            for key in ("translated_lines", "translations", "items", "lines"):
                nested_candidate = candidate.get(key)
                if isinstance(nested_candidate, list):
                    return nested_candidate
            for key in ("output", "result", "data", "payload", "response", "message", "content"):
                nested_candidate = candidate.get(key)
                if nested_candidate is candidate:
                    continue
                resolved = self._resolve_candidate_list(nested_candidate)
                if resolved is not None:
                    return resolved
        return None

    def _normalize_translated_lines(
        self,
        translated_lines: object,
        *,
        expected_lines: list[SubtitleLine] | None = None,
    ) -> list[str]:
        if not isinstance(translated_lines, list):
            raise TranslationProviderOutputError("Translation provider output must contain translated_lines list.")

        normalized_lines: list[str] = []
        observed_indices: list[int | None] = []

        for item in translated_lines:
            if isinstance(item, str):
                normalized_lines.append(item)
                observed_indices.append(None)
                continue
            if not isinstance(item, dict):
                raise TranslationProviderOutputError(
                    "Translation provider output lines must be strings or line objects."
                )

            text_value = self._extract_structured_line_text(item)
            if text_value is None:
                raise TranslationProviderOutputError(
                    "Translation provider structured line output must contain text."
                )
            normalized_lines.append(text_value)

            index_value = item.get("index", item.get("line_index"))
            if index_value is not None and not isinstance(index_value, int):
                raise TranslationProviderOutputError(
                    "Translation provider structured line indices must be integers."
                )
            observed_indices.append(index_value if isinstance(index_value, int) else None)

        if any(index is not None for index in observed_indices):
            if any(index is None for index in observed_indices):
                raise TranslationProviderOutputError(
                    "Translation provider structured line output must include index for every line."
                )
            expected_indices = [line.index for line in expected_lines] if expected_lines is not None else None
            normalized_indices = [int(index) for index in observed_indices if index is not None]
            if (
                expected_indices is not None
                and len(normalized_indices) == len(expected_indices)
                and normalized_indices != expected_indices
            ):
                raise TranslationProviderOutputError(
                    "Translation provider structured output must preserve input line indices and order."
                )

        return normalized_lines

    def _extract_text_from_content_dict(self, content: dict[str, object]) -> str | None:
        for key in ("text", "content", "output_text"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _extract_structured_line_text(self, item: dict[str, object]) -> str | None:
        for key in ("text", "translation", "translated_text", "content"):
            value = item.get(key)
            if isinstance(value, str):
                return value
        return None


def resolve_translation_provider(
    selection: TranslationProviderSelectionConfig,
    mock_provider: TranslationProvider,
) -> TranslationProviderBinding:
    mode = selection.mode.strip().lower()
    if mode == "mock":
        return TranslationProviderBinding(
            provider=mock_provider,
            provider_name="mock_translator",
            model_name=None,
            target_language=selection.real.target_language,
            mode="mock",
            version_context=_get_provider_cache_context(mock_provider),
        )
    if mode != "real":
        raise TranslationConfigurationError(f"Unsupported translation mode: {selection.mode}")

    try:
        provider = OpenAICompatibleTranslationProvider(selection.real)
    except TranslationConfigurationError as exc:
        if not selection.real.fallback_to_mock:
            raise
        return TranslationProviderBinding(
            provider=mock_provider,
            provider_name="mock_translator",
            model_name=None,
            target_language=selection.real.target_language,
            mode="mock_fallback",
            fallback_applied=True,
            fallback_reason=str(exc),
            fallback_stage="configuration",
            version_context=_get_provider_cache_context(mock_provider),
        )

    return TranslationProviderBinding(
        provider=provider,
        provider_name=selection.real.provider_name,
        model_name=selection.real.model_name,
        target_language=selection.real.target_language,
        mode="real",
        runtime_fallback_enabled=selection.real.runtime_fallback_to_mock,
        fallback_provider=mock_provider if selection.real.runtime_fallback_to_mock else None,
        fallback_from=selection.real.provider_name if selection.real.runtime_fallback_to_mock else None,
        fallback_to="mock_translator" if selection.real.runtime_fallback_to_mock else None,
        version_context=_get_provider_cache_context(provider),
    )


def classify_translation_error(exc: Exception) -> dict[str, object]:
    if isinstance(exc, TranslationConfigurationError):
        return {"error_type": "configuration_error", "retry_candidate": False}
    if isinstance(exc, TranslationProviderUnavailableError):
        return {"error_type": "provider_unavailable", "retry_candidate": True}
    if isinstance(exc, TranslationProviderLineCountError):
        return {"error_type": "provider_output_line_count_mismatch", "retry_candidate": False}
    if isinstance(exc, TranslationProviderResponseFormatError):
        return {"error_type": "invalid_provider_response_format", "retry_candidate": False}
    if isinstance(exc, TranslationProviderOutputError):
        return {"error_type": "invalid_provider_output", "retry_candidate": False}
    return {"error_type": "translation_error", "retry_candidate": False}


def _get_provider_cache_context(provider: TranslationProvider) -> dict[str, object]:
    context_getter = getattr(provider, "get_cache_context", None)
    if callable(context_getter):
        context = context_getter()
        if isinstance(context, dict):
            return context
    return {}


from services.provider_config_helpers import (
    classify_api_key_source_type as _classify_api_key_source_type,
    summarize_config_source as _summarize_config_source,
)


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


def _strip_outer_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return stripped
