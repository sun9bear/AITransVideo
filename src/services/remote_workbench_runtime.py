from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import urlparse

from services.control_panel import CONTROL_PANEL_DEFAULT_PORT
from services.jobs.api import JOB_API_DEFAULT_HOST, JOB_API_DEFAULT_PORT


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REMOTE_WORKBENCH_CONFIG_PATH = PROJECT_ROOT / "remote_workbench.local.json"
DEFAULT_RUNTIME_LOGS_DIR_NAME = "runtime_logs"
DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 8880
DEFAULT_FRONTEND_HOST = "127.0.0.1"
DEFAULT_FRONTEND_PORT = 3000
_ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "localhost"}


class RemoteWorkbenchRuntimeConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ServiceBinding:
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class ControlPanelRuntimeConfig:
    enabled: bool
    binding: ServiceBinding


@dataclass(frozen=True, slots=True)
class PublicEntryRuntimeConfig:
    enabled: bool
    provider: str
    site_host: str | None
    https_url: str | None
    executable_path: Path | None
    basic_auth_username_env: str | None
    basic_auth_password_hash_env: str | None
    generated_caddyfile_path: Path
    access_log_path: Path
    auth_mode: str | None
    notes: str | None


@dataclass(frozen=True, slots=True)
class RemoteWorkbenchRuntimeConfig:
    path: Path
    job_api: ServiceBinding
    gateway: ServiceBinding
    frontend: ServiceBinding
    control_panel: ControlPanelRuntimeConfig
    public_entry: PublicEntryRuntimeConfig
    runtime_logs_dir: Path

    @property
    def job_api_base_url(self) -> str:
        return self.job_api.base_url


def load_remote_workbench_runtime_config(
    config_path: Path | str | None = None,
) -> RemoteWorkbenchRuntimeConfig:
    resolved_path = Path(config_path or DEFAULT_REMOTE_WORKBENCH_CONFIG_PATH).expanduser().resolve(strict=False)
    payload = _load_config_payload(resolved_path)
    config_root = resolved_path.parent

    job_api_binding = _load_service_binding(
        payload.get("job_api"),
        default_host=JOB_API_DEFAULT_HOST,
        default_port=JOB_API_DEFAULT_PORT,
        section_name="job_api",
    )
    gateway_binding = _load_service_binding(
        payload.get("gateway"),
        default_host=DEFAULT_GATEWAY_HOST,
        default_port=DEFAULT_GATEWAY_PORT,
        section_name="gateway",
    )
    frontend_binding = _load_service_binding(
        payload.get("frontend"),
        default_host=DEFAULT_FRONTEND_HOST,
        default_port=DEFAULT_FRONTEND_PORT,
        section_name="frontend",
    )
    control_panel_binding = _load_service_binding(
        payload.get("control_panel"),
        default_host=JOB_API_DEFAULT_HOST,
        default_port=CONTROL_PANEL_DEFAULT_PORT,
        section_name="control_panel",
    )

    control_panel_section = _coerce_optional_dict(payload.get("control_panel"), "control_panel")
    public_entry_section = _coerce_optional_dict(payload.get("public_entry"), "public_entry")
    runtime_logs_section = _coerce_optional_dict(payload.get("runtime_logs"), "runtime_logs")

    runtime_logs_value = _normalize_optional_text(runtime_logs_section.get("directory")) or DEFAULT_RUNTIME_LOGS_DIR_NAME
    runtime_logs_dir = Path(runtime_logs_value).expanduser()
    if not runtime_logs_dir.is_absolute():
        runtime_logs_dir = (config_root / runtime_logs_dir).resolve(strict=False)
    else:
        runtime_logs_dir = runtime_logs_dir.resolve(strict=False)

    public_https_url = _normalize_optional_text(public_entry_section.get("https_url"))
    public_site_host = _normalize_optional_text(public_entry_section.get("site_host"))
    if public_site_host is None and public_https_url is not None:
        parsed_https_url = urlparse(public_https_url)
        public_site_host = _normalize_optional_text(parsed_https_url.hostname)
    if public_https_url is None and public_site_host is not None:
        public_https_url = f"https://{public_site_host}"

    return RemoteWorkbenchRuntimeConfig(
        path=resolved_path,
        job_api=job_api_binding,
        gateway=gateway_binding,
        frontend=frontend_binding,
        control_panel=ControlPanelRuntimeConfig(
            enabled=_coerce_bool(control_panel_section.get("enabled"), default=False, section_name="control_panel.enabled"),
            binding=control_panel_binding,
        ),
        public_entry=PublicEntryRuntimeConfig(
            enabled=_coerce_bool(public_entry_section.get("enabled"), default=False, section_name="public_entry.enabled"),
            provider=_normalize_optional_text(public_entry_section.get("provider")) or "caddy",
            site_host=public_site_host,
            https_url=public_https_url,
            executable_path=_resolve_optional_path(
                public_entry_section.get("executable_path"),
                config_root=config_root,
            ),
            basic_auth_username_env=_normalize_optional_text(public_entry_section.get("basic_auth_username_env")),
            basic_auth_password_hash_env=_normalize_optional_text(
                public_entry_section.get("basic_auth_password_hash_env")
            ),
            generated_caddyfile_path=_resolve_path_with_default(
                public_entry_section.get("generated_caddyfile_path"),
                default_path=runtime_logs_dir / "public-entry.Caddyfile",
                config_root=config_root,
            ),
            access_log_path=_resolve_path_with_default(
                public_entry_section.get("access_log_path"),
                default_path=runtime_logs_dir / "public-entry.access.log",
                config_root=config_root,
            ),
            auth_mode=_normalize_optional_text(public_entry_section.get("auth_mode")),
            notes=_normalize_optional_text(public_entry_section.get("notes")),
        ),
        runtime_logs_dir=runtime_logs_dir,
    )


def _load_config_payload(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteWorkbenchRuntimeConfigError(
            f"Failed to load remote workbench config {config_path}: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise RemoteWorkbenchRuntimeConfigError(
            f"Remote workbench config {config_path} must contain a top-level JSON object."
        )
    return loaded


def _load_service_binding(
    section: object,
    *,
    default_host: str,
    default_port: int,
    section_name: str,
) -> ServiceBinding:
    resolved_section = _coerce_optional_dict(section, section_name)
    host = _normalize_optional_text(resolved_section.get("host")) or default_host
    port = _coerce_port(resolved_section.get("port"), default=default_port, section_name=f"{section_name}.port")
    normalized_host = host.strip()
    if normalized_host not in _ALLOWED_LOCAL_HOSTS:
        raise RemoteWorkbenchRuntimeConfigError(
            f"{section_name}.host must stay on localhost for the current remote workbench phases; got {normalized_host!r}."
        )
    return ServiceBinding(host=normalized_host, port=port)


def _coerce_optional_dict(section: object, section_name: str) -> dict[str, object]:
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise RemoteWorkbenchRuntimeConfigError(f"{section_name} must be a JSON object when provided.")
    return dict(section)


def _coerce_port(value: object, *, default: int, section_name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise RemoteWorkbenchRuntimeConfigError(f"{section_name} must be a positive integer.")
    if value <= 0:
        raise RemoteWorkbenchRuntimeConfigError(f"{section_name} must be a positive integer.")
    return value


def _coerce_bool(value: object, *, default: bool, section_name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise RemoteWorkbenchRuntimeConfigError(f"{section_name} must be a boolean when provided.")
    return value


def _normalize_optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized


def _resolve_optional_path(value: object, *, config_root: Path) -> Path | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    candidate_path = Path(normalized).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = (config_root / candidate_path).resolve(strict=False)
    else:
        candidate_path = candidate_path.resolve(strict=False)
    return candidate_path


def _resolve_path_with_default(value: object, *, default_path: Path, config_root: Path) -> Path:
    resolved_path = _resolve_optional_path(value, config_root=config_root)
    if resolved_path is not None:
        return resolved_path
    return default_path.resolve(strict=False)


__all__ = [
    "DEFAULT_REMOTE_WORKBENCH_CONFIG_PATH",
    "RemoteWorkbenchRuntimeConfig",
    "RemoteWorkbenchRuntimeConfigError",
    "ServiceBinding",
    "load_remote_workbench_runtime_config",
]
