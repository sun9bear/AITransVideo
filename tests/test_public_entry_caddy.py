from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from services.public_entry_caddy import (
    CaddyPublicEntryError,
    build_caddy_public_entry_plan,
    build_caddy_run_command,
    build_caddy_validate_command,
    run_caddy_public_entry,
    _wait_for_public_entry_readiness,
    preflight_caddy_public_entry,
    render_caddyfile,
    validate_caddy_configuration,
    validate_caddy_auth_environment,
    write_caddyfile,
)
from services.remote_workbench_runtime import load_remote_workbench_runtime_config


class _FakeCaddyProcess:
    def __init__(self, *, poll_values: list[int | None], wait_returncode: int = 0) -> None:
        self._poll_values = list(poll_values)
        self._last_poll_value = self._poll_values[-1] if self._poll_values else None
        self._wait_returncode = wait_returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        if self._poll_values:
            self._last_poll_value = self._poll_values.pop(0)
        return self._last_poll_value

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self._wait_returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_build_caddy_public_entry_plan_and_render_caddyfile(tmp_path: Path) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    fake_caddy_path = tmp_path / "bin" / "caddy.exe"
    fake_caddy_path.parent.mkdir(parents=True, exist_ok=True)
    fake_caddy_path.write_bytes(b"")
    config_path.write_text(
        json.dumps(
            {
                "job_api": {
                    "host": "127.0.0.1",
                    "port": 8877,
                },
                "public_entry": {
                    "enabled": True,
                    "provider": "caddy",
                    "site_host": "workbench.example.com",
                    "https_url": "https://workbench.example.com",
                    "executable_path": str(fake_caddy_path),
                    "basic_auth_username_env": "AUTODUB_PUBLIC_ENTRY_USERNAME",
                    "basic_auth_password_hash_env": "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH",
                    "generated_caddyfile_path": "runtime_logs/public-entry.Caddyfile",
                    "access_log_path": "runtime_logs/public-entry.access.log",
                    "auth_mode": "basic_auth",
                },
                "runtime_logs": {
                    "directory": "runtime_logs",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    runtime_config = load_remote_workbench_runtime_config(config_path)
    plan = build_caddy_public_entry_plan(runtime_config)
    rendered = render_caddyfile(plan)
    caddyfile_path = write_caddyfile(plan)
    command = build_caddy_run_command(plan)

    assert plan.site_host == "workbench.example.com"
    assert plan.https_url == "https://workbench.example.com"
    assert plan.gateway_upstream == "127.0.0.1:8880"
    assert plan.frontend_upstream == "127.0.0.1:3000"
    assert "admin off" in rendered
    assert "Auth is handled by Gateway" in rendered
    assert "reverse_proxy 127.0.0.1:8880" in rendered  # Gateway for API routes
    assert "reverse_proxy 127.0.0.1:3000" in rendered  # Next.js for page routes
    assert "Strict-Transport-Security" in rendered
    assert "public-entry.access.log" in rendered
    assert caddyfile_path.exists()
    assert command[:2] == [str(fake_caddy_path.resolve(strict=False)), "run"]


def test_validate_caddy_auth_environment_requires_both_variables(tmp_path: Path) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    fake_caddy_path = tmp_path / "bin" / "caddy.exe"
    fake_caddy_path.parent.mkdir(parents=True, exist_ok=True)
    fake_caddy_path.write_bytes(b"")
    config_path.write_text(
        json.dumps(
            {
                "public_entry": {
                    "enabled": True,
                    "provider": "caddy",
                    "site_host": "workbench.example.com",
                    "https_url": "https://workbench.example.com",
                    "executable_path": str(fake_caddy_path),
                    "basic_auth_username_env": "AUTODUB_PUBLIC_ENTRY_USERNAME",
                    "basic_auth_password_hash_env": "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_config = load_remote_workbench_runtime_config(config_path)
    plan = build_caddy_public_entry_plan(runtime_config)

    with pytest.raises(CaddyPublicEntryError, match="AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH"):
        validate_caddy_auth_environment(
            plan,
            env={
                "AUTODUB_PUBLIC_ENTRY_USERNAME": "demo-user",
            },
        )


def test_validate_caddy_auth_environment_includes_remediation_steps(tmp_path: Path) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    fake_caddy_path = tmp_path / "bin" / "caddy.exe"
    fake_caddy_path.parent.mkdir(parents=True, exist_ok=True)
    fake_caddy_path.write_bytes(b"")
    config_path.write_text(
        json.dumps(
            {
                "public_entry": {
                    "enabled": True,
                    "provider": "caddy",
                    "site_host": "workbench.example.com",
                    "https_url": "https://workbench.example.com",
                    "executable_path": str(fake_caddy_path),
                    "basic_auth_username_env": "AUTODUB_PUBLIC_ENTRY_USERNAME",
                    "basic_auth_password_hash_env": "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_config = load_remote_workbench_runtime_config(config_path)
    plan = build_caddy_public_entry_plan(runtime_config)

    with pytest.raises(CaddyPublicEntryError) as exc_info:
        validate_caddy_auth_environment(plan, env={})

    message = str(exc_info.value)
    assert "AUTODUB_PUBLIC_ENTRY_USERNAME" in message
    assert "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH" in message
    assert "caddy hash-password --plaintext" in message


def test_build_caddy_public_entry_plan_reports_missing_caddy_actionably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    config_path.write_text(
        json.dumps(
            {
                "public_entry": {
                    "enabled": True,
                    "provider": "caddy",
                    "site_host": "workbench.example.com",
                    "https_url": "https://workbench.example.com",
                    "basic_auth_username_env": "AUTODUB_PUBLIC_ENTRY_USERNAME",
                    "basic_auth_password_hash_env": "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_config = load_remote_workbench_runtime_config(config_path)
    monkeypatch.setattr("services.public_entry_caddy.shutil.which", lambda _: None)

    with pytest.raises(CaddyPublicEntryError) as exc_info:
        build_caddy_public_entry_plan(runtime_config)

    message = str(exc_info.value)
    assert "Install caddy.exe" in message
    assert "public_entry.executable_path" in message


def test_preflight_caddy_public_entry_writes_caddyfile_and_runs_validate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    fake_caddy_path = tmp_path / "bin" / "caddy.exe"
    fake_caddy_path.parent.mkdir(parents=True, exist_ok=True)
    fake_caddy_path.write_bytes(b"")
    config_path.write_text(
        json.dumps(
            {
                "public_entry": {
                    "enabled": True,
                    "provider": "caddy",
                    "site_host": "workbench.example.com",
                    "https_url": "https://workbench.example.com",
                    "executable_path": str(fake_caddy_path),
                    "basic_auth_username_env": "AUTODUB_PUBLIC_ENTRY_USERNAME",
                    "basic_auth_password_hash_env": "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH",
                    "generated_caddyfile_path": "runtime_logs/public-entry.Caddyfile",
                    "access_log_path": "runtime_logs/public-entry.access.log",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_config = load_remote_workbench_runtime_config(config_path)
    captured_commands: list[list[str]] = []
    monkeypatch.setenv("AUTODUB_PUBLIC_ENTRY_USERNAME", "demo-user")
    monkeypatch.setenv("AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH", "$2a$14$demo")

    def _fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured_commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("services.public_entry_caddy.subprocess.run", _fake_run)

    plan = preflight_caddy_public_entry(
        runtime_config,
    )

    assert plan.generated_caddyfile_path.exists()
    assert captured_commands == [build_caddy_validate_command(plan)]


def test_write_caddyfile_keeps_basic_auth_account_line_well_formed(tmp_path: Path) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    fake_caddy_path = tmp_path / "bin" / "caddy.exe"
    fake_caddy_path.parent.mkdir(parents=True, exist_ok=True)
    fake_caddy_path.write_bytes(b"")
    config_path.write_text(
        json.dumps(
            {
                "public_entry": {
                    "enabled": True,
                    "provider": "caddy",
                    "site_host": "workbench.example.com",
                    "https_url": "https://workbench.example.com",
                    "executable_path": str(fake_caddy_path),
                    "basic_auth_username_env": "AUTODUB_PUBLIC_ENTRY_USERNAME",
                    "basic_auth_password_hash_env": "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    runtime_config = load_remote_workbench_runtime_config(config_path)
    plan = build_caddy_public_entry_plan(runtime_config)
    caddyfile_path = write_caddyfile(plan)
    rendered = caddyfile_path.read_text(encoding="utf-8")

    # Auth is now handled by Gateway, not Caddy basic_auth
    assert "Auth is handled by Gateway" in rendered
    assert "reverse_proxy" in rendered


def test_validate_caddy_configuration_reports_command_and_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    fake_caddy_path = tmp_path / "bin" / "caddy.exe"
    fake_caddy_path.parent.mkdir(parents=True, exist_ok=True)
    fake_caddy_path.write_bytes(b"")
    config_path.write_text(
        json.dumps(
            {
                "public_entry": {
                    "enabled": True,
                    "provider": "caddy",
                    "site_host": "workbench.example.com",
                    "https_url": "https://workbench.example.com",
                    "executable_path": str(fake_caddy_path),
                    "basic_auth_username_env": "AUTODUB_PUBLIC_ENTRY_USERNAME",
                    "basic_auth_password_hash_env": "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_config = load_remote_workbench_runtime_config(config_path)
    plan = build_caddy_public_entry_plan(runtime_config)
    write_caddyfile(plan)

    def _fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "invalid basic_auth stanza")

    monkeypatch.setattr("services.public_entry_caddy.subprocess.run", _fake_run)

    with pytest.raises(CaddyPublicEntryError) as exc_info:
        validate_caddy_configuration(plan)

    message = str(exc_info.value)
    assert "Caddy validate failed for" in message
    assert "invalid basic_auth stanza" in message
    assert "validate --config" in message


def test_wait_for_public_entry_readiness_requires_live_process_and_listener(tmp_path: Path) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    fake_caddy_path = tmp_path / "bin" / "caddy.exe"
    fake_caddy_path.parent.mkdir(parents=True, exist_ok=True)
    fake_caddy_path.write_bytes(b"")
    config_path.write_text(
        json.dumps(
            {
                "public_entry": {
                    "enabled": True,
                    "provider": "caddy",
                    "site_host": "localhost:8443",
                    "https_url": "https://localhost:8443",
                    "executable_path": str(fake_caddy_path),
                    "basic_auth_username_env": "AUTODUB_PUBLIC_ENTRY_USERNAME",
                    "basic_auth_password_hash_env": "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_config = load_remote_workbench_runtime_config(config_path)
    plan = build_caddy_public_entry_plan(runtime_config)
    process = _FakeCaddyProcess(poll_values=[None, None], wait_returncode=0)
    probe_calls: list[tuple[str, int]] = []

    def _fake_probe(host: str, port: int) -> bool:
        probe_calls.append((host, port))
        return len(probe_calls) >= 2

    _wait_for_public_entry_readiness(
        plan,
        process,
        timeout_seconds=0.5,
        probe_fn=_fake_probe,
        sleep_fn=lambda _: None,
    )

    assert any(port == 8443 for _host, port in probe_calls)
    assert process.terminated is False


def test_wait_for_public_entry_readiness_fails_when_caddy_exits_early(tmp_path: Path) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    fake_caddy_path = tmp_path / "bin" / "caddy.exe"
    fake_caddy_path.parent.mkdir(parents=True, exist_ok=True)
    fake_caddy_path.write_bytes(b"")
    config_path.write_text(
        json.dumps(
            {
                "public_entry": {
                    "enabled": True,
                    "provider": "caddy",
                    "site_host": "localhost:8443",
                    "https_url": "https://localhost:8443",
                    "executable_path": str(fake_caddy_path),
                    "basic_auth_username_env": "AUTODUB_PUBLIC_ENTRY_USERNAME",
                    "basic_auth_password_hash_env": "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_config = load_remote_workbench_runtime_config(config_path)
    plan = build_caddy_public_entry_plan(runtime_config)
    process = _FakeCaddyProcess(poll_values=[None, 1], wait_returncode=1)

    with pytest.raises(CaddyPublicEntryError, match="Expected listener on port 8443"):
        _wait_for_public_entry_readiness(
            plan,
            process,
            timeout_seconds=0.5,
            probe_fn=lambda *_args: False,
            sleep_fn=lambda _: None,
        )


def test_run_caddy_public_entry_waits_for_readiness_before_printing_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    fake_caddy_path = tmp_path / "bin" / "caddy.exe"
    fake_caddy_path.parent.mkdir(parents=True, exist_ok=True)
    fake_caddy_path.write_bytes(b"")
    config_path.write_text(
        json.dumps(
            {
                "public_entry": {
                    "enabled": True,
                    "provider": "caddy",
                    "site_host": "localhost:8443",
                    "https_url": "https://localhost:8443",
                    "executable_path": str(fake_caddy_path),
                    "basic_auth_username_env": "AUTODUB_PUBLIC_ENTRY_USERNAME",
                    "basic_auth_password_hash_env": "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_config = load_remote_workbench_runtime_config(config_path)
    plan = build_caddy_public_entry_plan(runtime_config)
    fake_process = _FakeCaddyProcess(poll_values=[None], wait_returncode=7)
    events: list[str] = []
    captured_commands: list[list[str]] = []

    monkeypatch.setattr("services.public_entry_caddy.preflight_caddy_public_entry", lambda _config: plan)

    def _fake_popen(command: list[str], **_: object) -> _FakeCaddyProcess:
        captured_commands.append(list(command))
        return fake_process

    monkeypatch.setattr("services.public_entry_caddy.subprocess.Popen", _fake_popen)

    def _fake_wait(plan_arg, process_arg, **_kwargs):
        assert plan_arg == plan
        assert process_arg is fake_process
        events.append("wait_ready")

    monkeypatch.setattr("services.public_entry_caddy._wait_for_public_entry_readiness", _fake_wait)
    monkeypatch.setattr("builtins.print", lambda message: events.append(str(message)))

    result = run_caddy_public_entry(runtime_config)

    assert result == 7
    assert captured_commands == [build_caddy_run_command(plan)]
    assert events[0] == "wait_ready"
    assert any(event == "Public entry started at https://localhost:8443" for event in events[1:])


def test_build_caddy_public_entry_plan_rejects_disabled_public_entry(tmp_path: Path) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    config_path.write_text(
        json.dumps(
            {
                "public_entry": {
                    "enabled": False,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_config = load_remote_workbench_runtime_config(config_path)

    with pytest.raises(CaddyPublicEntryError, match="enabled is false"):
        build_caddy_public_entry_plan(runtime_config)
