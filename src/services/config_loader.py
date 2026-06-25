from dataclasses import dataclass
import copy
import json
import os
from pathlib import Path
import sys
import tempfile

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]

from utils.atomic_io import atomic_write_json as _atomic_write_json_helper


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUTODUB_LOCAL_CONFIG_PATH = PROJECT_ROOT / "autodub.local.json"
DEFAULT_PROJECT_LOCAL_CONFIG_TEMPLATE: dict[str, object] = {
    "youtube": {
        "cookies_from_browser": None,
        "cookie_file": None,
        "max_retries": 2,
        "retry_backoff_seconds": 1.5,
    },
    "paths": {
        "voice_bank_root": "voice_bank",
        "voice_source_audio_root": "voice_bank/source_audio",
        "voice_registry_path": "voice_registry.json",
        "voice_verification_root": "voice_bank/verification_audio",
    },
    "translation": {
        "enabled": False,
        "mode": "mock",
        "provider_name": "openai_compatible",
        "base_url": None,
        "model_name": None,
        "target_language": "zh-CN",
        "api_key": None,
        "api_key_env_var": "AUTODUB_TRANSLATION_API_KEY",
        "timeout_seconds": 30.0,
        "max_retries": 2,
        "retry_backoff_seconds": 0.5,
        "fallback_to_mock": False,
        "runtime_fallback_to_mock": False,
        "api_protocol": "chat_completions_v1",
        "provider_variant": "openai_compatible_translation_v2",
    },
    "tts": {
        "enabled": False,
        "mode": "mock",
        "provider_name": "openai_compatible_tts",
        "tts_provider": None,
        "platform": None,
        "base_url": None,
        "model_name": None,
        "api_key": None,
        "api_key_env_var": "AUTODUB_TTS_API_KEY",
        "timeout_seconds": 30.0,
        "max_retries": 2,
        "retry_backoff_seconds": 0.5,
        "voice_name": "alloy",
        "voice_id": None,
        "voice_registry_path": "voice_registry.json",
        "audio_format": "wav",
        "fallback_to_mock": False,
        "api_protocol": "audio_speech_v1",
    },
    "voice_clone": {
        "enabled": False,
        "provider_name": "minimax_voice_clone",
        "base_url": None,
        "model_name": None,
        "api_key": None,
        "api_key_env_var": "AUTODUB_TTS_API_KEY",
        "timeout_seconds": 180.0,
        "max_retries": 2,
        "retry_backoff_seconds": 1.0,
    },
    "voice_registry": {
        "registry_path": "voice_registry.json",
        "provider_name": "minimax_tts",
        "tts_provider": None,
        "platform": None,
    },
    "prompts": {
        "s2_infer": None,
        "s3_translate": None,
        "s5_rewrite": None,
    },
}
EDITABLE_PROJECT_LOCAL_CONFIG_SECTIONS = tuple(DEFAULT_PROJECT_LOCAL_CONFIG_TEMPLATE.keys())


@dataclass(frozen=True, slots=True)
class ProjectLocalConfig:
    path: Path
    payload: dict[str, object] | None
    error: str | None = None

    def get_section(self, section_name: str) -> dict[str, object]:
        if not isinstance(self.payload, dict):
            return {}
        section_payload = self.payload.get(section_name)
        if isinstance(section_payload, dict):
            return dict(section_payload)
        return {}


def build_default_project_local_config_payload() -> dict[str, object]:
    return copy.deepcopy(DEFAULT_PROJECT_LOCAL_CONFIG_TEMPLATE)


def load_project_local_config(config_path: Path | None = None) -> ProjectLocalConfig:
    resolved_path = (config_path or DEFAULT_AUTODUB_LOCAL_CONFIG_PATH).expanduser().resolve(strict=False)
    if not resolved_path.exists():
        return ProjectLocalConfig(path=resolved_path, payload=None, error=None)
    try:
        loaded = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ProjectLocalConfig(
            path=resolved_path,
            payload=None,
            error=f"Failed to load local config file {resolved_path}: {exc}",
        )
    if not isinstance(loaded, dict):
        return ProjectLocalConfig(
            path=resolved_path,
            payload=None,
            error=f"Local config file {resolved_path} must contain a top-level JSON object.",
        )
    return ProjectLocalConfig(path=resolved_path, payload=loaded, error=None)


def build_editable_project_local_config_payload(
    config: ProjectLocalConfig | None = None,
) -> dict[str, object]:
    base_payload = build_default_project_local_config_payload()
    if config is None or config.error is not None or not isinstance(config.payload, dict):
        return base_payload

    merged_payload: dict[str, object] = copy.deepcopy(config.payload)
    for section_name, default_section in DEFAULT_PROJECT_LOCAL_CONFIG_TEMPLATE.items():
        existing_section = merged_payload.get(section_name)
        if isinstance(default_section, dict) and isinstance(existing_section, dict):
            merged_payload[section_name] = _deep_merge_dicts(default_section, existing_section)
            continue
        if section_name not in merged_payload:
            merged_payload[section_name] = copy.deepcopy(default_section)
    return merged_payload


def save_project_local_config_sections(
    section_overrides: dict[str, object],
    *,
    config_path: Path | None = None,
) -> ProjectLocalConfig:
    if not isinstance(section_overrides, dict):
        raise ValueError("section_overrides must be a dict keyed by config section name.")

    loaded_config = load_project_local_config(config_path)
    config_payload = build_editable_project_local_config_payload(loaded_config)
    for section_name, section_value in section_overrides.items():
        normalized_section_name = str(section_name).strip()
        if not normalized_section_name:
            raise ValueError("config section name cannot be empty.")
        if not isinstance(section_value, dict):
            raise ValueError(f"config section {normalized_section_name} must be a JSON object.")
        template_value = DEFAULT_PROJECT_LOCAL_CONFIG_TEMPLATE.get(normalized_section_name)
        if isinstance(template_value, dict):
            existing_section = config_payload.get(normalized_section_name)
            merged_existing_section = (
                _deep_merge_dicts(template_value, existing_section)
                if isinstance(existing_section, dict)
                else _deep_merge_dicts(template_value, {})
            )
            config_payload[normalized_section_name] = _deep_merge_dicts(
                merged_existing_section,
                section_value,
            )
            continue
        config_payload[normalized_section_name] = copy.deepcopy(section_value)

    resolved_path = loaded_config.path
    _write_json_atomically(resolved_path, config_payload)
    return load_project_local_config(resolved_path)


def resolve_env_text_value(candidate_keys: list[str]) -> tuple[str | None, str | None]:
    for scope_name, reader in iter_env_readers():
        for key in candidate_keys:
            normalized_key = key.strip()
            if not normalized_key:
                continue
            resolved_value = _read_optional_text(reader(normalized_key))
            if resolved_value is not None:
                return resolved_value, f"{scope_name}:{normalized_key}"
    return None, None


def resolve_text_value(
    *,
    env_keys: list[str] | None = None,
    config: ProjectLocalConfig | None = None,
    config_key_paths: tuple[tuple[str, ...], ...] = (),
) -> tuple[str | None, str | None]:
    if env_keys:
        resolved_env_value, resolved_env_source = resolve_env_text_value(env_keys)
        if resolved_env_value is not None:
            return resolved_env_value, resolved_env_source
    if config is None or config.error is not None or not isinstance(config.payload, dict):
        return None, None
    for key_path in config_key_paths:
        candidate = _read_nested_mapping_value(config.payload, key_path)
        if isinstance(candidate, str):
            normalized = _read_optional_text(candidate)
            if normalized is not None:
                return normalized, _build_config_source(config.path, key_path)
    return None, None


def resolve_bool_value(
    *,
    env_keys: list[str] | None = None,
    config: ProjectLocalConfig | None = None,
    config_key_paths: tuple[tuple[str, ...], ...] = (),
    default: bool,
) -> tuple[bool, str | None]:
    if env_keys:
        raw_env_value, env_source = resolve_env_text_value(env_keys)
        if raw_env_value is not None:
            normalized = raw_env_value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True, env_source
            if normalized in {"0", "false", "no", "off"}:
                return False, env_source
            return default, env_source
    config_value, config_source = _resolve_config_raw_value(config, config_key_paths)
    if isinstance(config_value, bool):
        return config_value, config_source
    if isinstance(config_value, str):
        normalized = config_value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True, config_source
        if normalized in {"0", "false", "no", "off"}:
            return False, config_source
        return default, config_source
    return default, None


def resolve_float_value(
    *,
    env_keys: list[str] | None = None,
    config: ProjectLocalConfig | None = None,
    config_key_paths: tuple[tuple[str, ...], ...] = (),
    default: float,
) -> tuple[float, str | None]:
    if env_keys:
        raw_env_value, env_source = resolve_env_text_value(env_keys)
        if raw_env_value is not None:
            try:
                return float(raw_env_value), env_source
            except ValueError:
                return default, env_source
    config_value, config_source = _resolve_config_raw_value(config, config_key_paths)
    if isinstance(config_value, bool):
        return default, config_source
    if isinstance(config_value, (int, float)):
        return float(config_value), config_source
    if isinstance(config_value, str):
        try:
            return float(config_value), config_source
        except ValueError:
            return default, config_source
    return default, None


def resolve_int_value(
    *,
    env_keys: list[str] | None = None,
    config: ProjectLocalConfig | None = None,
    config_key_paths: tuple[tuple[str, ...], ...] = (),
    default: int,
) -> tuple[int, str | None]:
    if env_keys:
        raw_env_value, env_source = resolve_env_text_value(env_keys)
        if raw_env_value is not None:
            try:
                return int(raw_env_value), env_source
            except ValueError:
                return default, env_source
    config_value, config_source = _resolve_config_raw_value(config, config_key_paths)
    if isinstance(config_value, bool):
        return default, config_source
    if isinstance(config_value, int):
        return config_value, config_source
    if isinstance(config_value, float):
        return int(config_value), config_source
    if isinstance(config_value, str):
        try:
            return int(config_value), config_source
        except ValueError:
            return default, config_source
    return default, None


def resolve_path_value(
    *,
    env_keys: list[str] | None = None,
    config: ProjectLocalConfig | None = None,
    config_key_paths: tuple[tuple[str, ...], ...] = (),
) -> tuple[str | None, str | None]:
    resolved_value, resolved_source = resolve_text_value(
        env_keys=env_keys,
        config=config,
        config_key_paths=config_key_paths,
    )
    if resolved_value is None:
        return None, None
    if (
        resolved_source is not None
        and resolved_source.startswith("config_file:")
        and not Path(resolved_value).is_absolute()
        and config is not None
    ):
        return str((config.path.parent / resolved_value).resolve(strict=False)), resolved_source
    return resolved_value, resolved_source


def summarize_source_family(source: str | None) -> str | None:
    if source is None:
        return "default"
    if source.startswith("process:"):
        return "env"
    if source.startswith("user:") or source.startswith("machine:"):
        return "persisted_env"
    if source.startswith("config_file:"):
        return "autodub.local.json"
    return source


def iter_env_readers():
    yield "process", os.getenv
    if sys.platform.startswith("win"):
        yield "user", lambda key: _read_windows_persisted_env(key, hive="user")
        yield "machine", lambda key: _read_windows_persisted_env(key, hive="machine")


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


def _read_nested_mapping_value(payload: dict[str, object], key_path: tuple[str, ...]) -> object | None:
    candidate: object = payload
    for key in key_path:
        if not isinstance(candidate, dict):
            return None
        candidate = candidate.get(key)
    return candidate


def _resolve_config_raw_value(
    config: ProjectLocalConfig | None,
    key_paths: tuple[tuple[str, ...], ...],
) -> tuple[object | None, str | None]:
    if config is None or config.error is not None or not isinstance(config.payload, dict):
        return None, None
    for key_path in key_paths:
        candidate = _read_nested_mapping_value(config.payload, key_path)
        if candidate is not None:
            return candidate, _build_config_source(config.path, key_path)
    return None, None


def _read_optional_text(raw_value: object) -> str | None:
    if raw_value is None:
        return None
    normalized = str(raw_value).strip()
    return normalized or None


def _build_config_source(config_path: Path, key_path: tuple[str, ...]) -> str:
    return f"config_file:{config_path}:{'.'.join(key_path)}"


def _deep_merge_dicts(
    base_payload: dict[str, object],
    override_payload: dict[str, object],
) -> dict[str, object]:
    merged_payload = copy.deepcopy(base_payload)
    for key, value in override_payload.items():
        if isinstance(value, dict) and isinstance(merged_payload.get(key), dict):
            merged_payload[key] = _deep_merge_dicts(merged_payload[key], value)  # type: ignore[arg-type]
            continue
        merged_payload[key] = copy.deepcopy(value)
    return merged_payload


def _write_json_atomically(target_path: Path, payload: dict[str, object]) -> None:
    """DRY-02 收口（TU-04，spec 未枚举此第 7 处，迁移以满足 DRY-02 + DoD ≤2 指标）。

    委托 utils.atomic_io.atomic_write_json。原实现 json.dumps(indent=2,
    ensure_ascii=False) 无 sort_keys（插入顺序）+ 始终 fsync，故传 sort_keys=False、
    fsync=True（默认）→ 字节等价。
    """
    _atomic_write_json_helper(target_path, payload, fsync=True, sort_keys=False)
