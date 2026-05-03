"""Regression guards for admin job project directory deletion."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

import admin_settings  # noqa: E402


def test_remove_project_dir_refuses_unsafe_path(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    calls: list[Path] = []

    monkeypatch.setattr(admin_settings, "_is_safe_project_dir", lambda path: False)
    monkeypatch.setattr(admin_settings.shutil, "rmtree", lambda path: calls.append(path))

    assert admin_settings._remove_project_dir_if_safe(str(project_dir), job_id="job-1") is False
    assert calls == []
    assert project_dir.exists()


def test_remove_project_dir_deletes_only_after_safety_check(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    calls: list[Path] = []

    monkeypatch.setattr(admin_settings, "_is_safe_project_dir", lambda path: True)
    monkeypatch.setattr(admin_settings.shutil, "rmtree", lambda path: calls.append(path))

    assert admin_settings._remove_project_dir_if_safe(str(project_dir), job_id="job-1") is True
    assert calls == [project_dir]
