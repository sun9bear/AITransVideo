from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.remote_workbench_runtime import (
    RemoteWorkbenchRuntimeConfigError,
    load_remote_workbench_runtime_config,
)


def test_load_remote_workbench_runtime_config_uses_defaults_and_relative_logs(tmp_path: Path) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    config_path.write_text(
        json.dumps(
            {
                "control_panel": {
                    "enabled": True,
                },
                "public_entry": {
                    "enabled": True,
                    "provider": "caddy",
                    "site_host": "example.com",
                    "https_url": "https://example.com",
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

    config = load_remote_workbench_runtime_config(config_path)

    assert config.path == config_path.resolve(strict=False)
    assert config.web_ui.host == "127.0.0.1"
    assert config.web_ui.port == 8876
    assert config.job_api.host == "127.0.0.1"
    assert config.job_api.port == 8877
    assert config.job_api_base_url == "http://127.0.0.1:8877"
    assert config.control_panel.enabled is True
    assert config.control_panel.binding.host == "127.0.0.1"
    assert config.control_panel.binding.port == 8765
    assert config.public_entry.enabled is True
    assert config.public_entry.provider == "caddy"
    assert config.public_entry.site_host == "example.com"
    assert config.public_entry.https_url == "https://example.com"
    assert config.public_entry.basic_auth_username_env == "AUTODUB_PUBLIC_ENTRY_USERNAME"
    assert config.public_entry.basic_auth_password_hash_env == "AUTODUB_PUBLIC_ENTRY_PASSWORD_HASH"
    assert config.public_entry.generated_caddyfile_path == (
        tmp_path / "runtime_logs" / "public-entry.Caddyfile"
    ).resolve(strict=False)
    assert config.public_entry.access_log_path == (
        tmp_path / "runtime_logs" / "public-entry.access.log"
    ).resolve(strict=False)
    assert config.runtime_logs_dir == (tmp_path / "runtime_logs").resolve(strict=False)


def test_load_remote_workbench_runtime_config_rejects_non_local_bindings(tmp_path: Path) -> None:
    config_path = tmp_path / "remote_workbench.local.json"
    config_path.write_text(
        json.dumps(
            {
                "web_ui": {
                    "host": "0.0.0.0",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(RemoteWorkbenchRuntimeConfigError, match="localhost"):
        load_remote_workbench_runtime_config(config_path)
