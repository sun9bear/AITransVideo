from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from typing import Callable
from urllib.parse import urlparse

from services.remote_workbench_runtime import RemoteWorkbenchRuntimeConfig


class CaddyPublicEntryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CaddyPublicEntryPlan:
    executable_path: Path
    site_host: str
    https_url: str
    upstream: str
    generated_caddyfile_path: Path
    access_log_path: Path
    basic_auth_username_env: str
    basic_auth_password_hash_env: str


def build_caddy_public_entry_plan(runtime_config: RemoteWorkbenchRuntimeConfig) -> CaddyPublicEntryPlan:
    public_entry = runtime_config.public_entry
    if not public_entry.enabled:
        raise CaddyPublicEntryError("public_entry.enabled is false in remote_workbench.local.json")
    if public_entry.provider != "caddy":
        raise CaddyPublicEntryError(
            f"Unsupported public entry provider for Phase P2: {public_entry.provider!r}"
        )
    site_host = _require_text(public_entry.site_host, "public_entry.site_host")
    https_url = _require_text(public_entry.https_url, "public_entry.https_url")
    username_env = _require_text(
        public_entry.basic_auth_username_env,
        "public_entry.basic_auth_username_env",
    )
    password_hash_env = _require_text(
        public_entry.basic_auth_password_hash_env,
        "public_entry.basic_auth_password_hash_env",
    )
    executable_path = _resolve_caddy_executable(public_entry.executable_path)

    generated_caddyfile_path = public_entry.generated_caddyfile_path.resolve(strict=False)
    access_log_path = public_entry.access_log_path.resolve(strict=False)
    generated_caddyfile_path.parent.mkdir(parents=True, exist_ok=True)
    access_log_path.parent.mkdir(parents=True, exist_ok=True)

    return CaddyPublicEntryPlan(
        executable_path=executable_path,
        site_host=site_host,
        https_url=https_url,
        upstream=f"{runtime_config.web_ui.host}:{runtime_config.web_ui.port}",
        generated_caddyfile_path=generated_caddyfile_path,
        access_log_path=access_log_path,
        basic_auth_username_env=username_env,
        basic_auth_password_hash_env=password_hash_env,
    )


def render_caddyfile(plan: CaddyPublicEntryPlan) -> str:
    access_log_path = plan.access_log_path.resolve(strict=False).as_posix()
    username_placeholder = _build_caddy_env_placeholder(plan.basic_auth_username_env)
    password_hash_placeholder = _build_caddy_env_placeholder(plan.basic_auth_password_hash_env)
    return "\n".join(
        [
            "{",
            "    admin off",
            "}",
            "",
            f"{plan.site_host} {{",
            "    encode zstd gzip",
            "",
            "    log {",
            f"        output file {access_log_path} {{",
            "            roll_keep 10",
            "            roll_keep_for 168h",
            "        }",
            "        format json",
            "    }",
            "",
            "    # Auth is handled by Gateway (session-based), not Caddy basic_auth",
            "",
            "    header {",
            '        Strict-Transport-Security "max-age=31536000; includeSubDomains"',
            '        X-Content-Type-Options "nosniff"',
            '        X-Frame-Options "DENY"',
            '        Referrer-Policy "strict-origin-when-cross-origin"',
            "    }",
            "",
            f"    reverse_proxy {plan.upstream}",
            "}",
            "",
        ]
    )


def write_caddyfile(plan: CaddyPublicEntryPlan) -> Path:
    rendered = render_caddyfile(plan)
    plan.generated_caddyfile_path.parent.mkdir(parents=True, exist_ok=True)
    plan.generated_caddyfile_path.write_text(rendered, encoding="utf-8")
    return plan.generated_caddyfile_path


def build_caddy_run_command(plan: CaddyPublicEntryPlan) -> list[str]:
    return [
        str(plan.executable_path),
        "run",
        "--config",
        str(plan.generated_caddyfile_path),
        "--adapter",
        "caddyfile",
    ]


def build_caddy_validate_command(plan: CaddyPublicEntryPlan) -> list[str]:
    return [
        str(plan.executable_path),
        "validate",
        "--config",
        str(plan.generated_caddyfile_path),
        "--adapter",
        "caddyfile",
    ]


def validate_caddy_auth_environment(
    plan: CaddyPublicEntryPlan,
    *,
    env: dict[str, str] | None = None,
) -> None:
    resolved_env = env or dict(os.environ)
    missing_names = [
        env_name
        for env_name in (plan.basic_auth_username_env, plan.basic_auth_password_hash_env)
        if not str(resolved_env.get(env_name) or "").strip()
    ]
    if missing_names:
        remediation_steps: list[str] = []
        if plan.basic_auth_username_env in missing_names:
            remediation_steps.append(
                f"set {plan.basic_auth_username_env} to the Basic Auth username"
            )
        if plan.basic_auth_password_hash_env in missing_names:
            remediation_steps.append(
                "set "
                f"{plan.basic_auth_password_hash_env} "
                'to the hash generated by `caddy hash-password --plaintext "<strong-password>"`'
            )
        raise CaddyPublicEntryError(
            "Missing required public-entry environment variables: "
            + ", ".join(missing_names)
            + ". "
            + "; ".join(remediation_steps)
            + "."
        )


def prepare_caddy_public_entry(runtime_config: RemoteWorkbenchRuntimeConfig) -> CaddyPublicEntryPlan:
    plan = build_caddy_public_entry_plan(runtime_config)
    validate_caddy_auth_environment(plan)
    write_caddyfile(plan)
    return plan


def preflight_caddy_public_entry(runtime_config: RemoteWorkbenchRuntimeConfig) -> CaddyPublicEntryPlan:
    plan = build_caddy_public_entry_plan(runtime_config)
    validate_caddy_auth_environment(plan)
    write_caddyfile(plan)
    validate_caddy_configuration(plan)
    return plan


def validate_caddy_configuration(plan: CaddyPublicEntryPlan) -> None:
    command = build_caddy_validate_command(plan)
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise CaddyPublicEntryError(
            f"Failed to execute Caddy validate command at {plan.executable_path}: {exc}"
        ) from exc
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "").strip() or "no output"
    raise CaddyPublicEntryError(
        f"Caddy validate failed for {plan.generated_caddyfile_path}. "
        f"Command: {_format_command(command)}. Detail: {detail}"
    )


def check_caddy_public_entry(runtime_config: RemoteWorkbenchRuntimeConfig) -> int:
    plan = preflight_caddy_public_entry(runtime_config)
    print(f"Public entry preflight passed for {plan.https_url}")
    print(f"Caddy executable: {plan.executable_path}")
    print(f"Generated Caddyfile: {plan.generated_caddyfile_path}")
    print(f"Access log: {plan.access_log_path}")
    print(f"Reverse proxy upstream: {plan.upstream}")
    return 0


def run_caddy_public_entry(runtime_config: RemoteWorkbenchRuntimeConfig) -> int:
    plan = preflight_caddy_public_entry(runtime_config)
    command = build_caddy_run_command(plan)
    try:
        process = subprocess.Popen(command)
    except OSError as exc:
        raise CaddyPublicEntryError(f"Failed to start Caddy from {plan.executable_path}: {exc}") from exc
    try:
        _wait_for_public_entry_readiness(plan, process)
    except Exception:
        _terminate_caddy_process(process)
        raise
    print(f"Public entry started at {plan.https_url}")
    print(f"Caddy executable: {plan.executable_path}")
    print(f"Generated Caddyfile: {plan.generated_caddyfile_path}")
    print(f"Access log: {plan.access_log_path}")
    print(f"Reverse proxy upstream: {plan.upstream}")
    return int(process.wait())


def _require_text(value: str | None, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise CaddyPublicEntryError(f"{field_name} is required for the Phase P2 Caddy public entry.")
    return normalized


def _resolve_caddy_executable(configured_path: Path | None) -> Path:
    if configured_path is not None:
        if not configured_path.exists() or not configured_path.is_file():
            raise CaddyPublicEntryError(
                f"Caddy executable was not found at configured path: {configured_path}. "
                "Install caddy.exe and either add it to PATH or set "
                "public_entry.executable_path to the absolute caddy.exe path."
            )
        return configured_path.resolve(strict=False)
    detected_path = shutil.which("caddy.exe") or shutil.which("caddy")
    if detected_path is None:
        raise CaddyPublicEntryError(
            "Caddy executable was not found. Install caddy.exe, add it to PATH, or set "
            "public_entry.executable_path in remote_workbench.local.json."
        )
    return Path(detected_path).resolve(strict=False)


def _format_command(command: list[str]) -> str:
    return " ".join(str(part) for part in command)


def _build_caddy_env_placeholder(env_name: str) -> str:
    return "{$" + env_name + "}"


def _resolve_public_entry_listen_port(plan: CaddyPublicEntryPlan) -> int:
    parsed_https_url = urlparse(plan.https_url)
    if parsed_https_url.port is not None:
        return parsed_https_url.port
    parsed_site_host = urlparse(f"https://{plan.site_host}")
    if parsed_site_host.port is not None:
        return parsed_site_host.port
    return 443


def _build_public_entry_probe_hosts(plan: CaddyPublicEntryPlan) -> tuple[str, ...]:
    parsed_https_url = urlparse(plan.https_url)
    parsed_site_host = urlparse(f"https://{plan.site_host}")
    configured_host = parsed_https_url.hostname or parsed_site_host.hostname or ""
    probe_hosts: list[str] = []
    if configured_host in {"localhost", "127.0.0.1", "::1"}:
        probe_hosts.append(configured_host)
    probe_hosts.extend(["127.0.0.1", "localhost"])
    deduplicated_hosts: list[str] = []
    for host in probe_hosts:
        if host not in deduplicated_hosts:
            deduplicated_hosts.append(host)
    return tuple(deduplicated_hosts)


def _probe_public_entry_listener(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _wait_for_public_entry_readiness(
    plan: CaddyPublicEntryPlan,
    process: subprocess.Popen[object],
    *,
    timeout_seconds: float = 10.0,
    probe_fn: Callable[[str, int], bool] = _probe_public_entry_listener,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    port = _resolve_public_entry_listen_port(plan)
    probe_hosts = _build_public_entry_probe_hosts(plan)
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            raise CaddyPublicEntryError(
                "Caddy exited before public-entry startup was healthy. "
                f"Expected listener on port {port}, return code: {returncode}."
            )
        if any(probe_fn(host, port) for host in probe_hosts):
            return
        sleep_fn(0.2)

    raise CaddyPublicEntryError(
        "Caddy did not establish a listening public entry before startup confirmation. "
        f"Expected listener on port {port}."
    )


def _terminate_caddy_process(process: subprocess.Popen[object]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


__all__ = [
    "CaddyPublicEntryError",
    "CaddyPublicEntryPlan",
    "build_caddy_public_entry_plan",
    "build_caddy_run_command",
    "build_caddy_validate_command",
    "check_caddy_public_entry",
    "preflight_caddy_public_entry",
    "prepare_caddy_public_entry",
    "render_caddyfile",
    "run_caddy_public_entry",
    "validate_caddy_configuration",
    "validate_caddy_auth_environment",
    "write_caddyfile",
]
