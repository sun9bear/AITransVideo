"""Tests for gateway/config.py — lazy DB URL resolution.

Guards the T3 design contract:
  1. resolve_database_url() is a pure function (no side effects).
  2. Module-level import does NOT raise in a clean env (no creds).
  3. Hardcoded 'avt:avt' fallback is refused.
"""
from __future__ import annotations

import sys
from pathlib import Path

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

import pytest

from config import GatewaySettings, resolve_database_url


def test_resolve_uses_explicit_url_first():
    s = GatewaySettings(database_url="postgresql+asyncpg://u:p@h/d", pg_password="")
    assert resolve_database_url(s) == "postgresql+asyncpg://u:p@h/d"


def test_resolve_uses_pg_password_when_no_explicit_url():
    s = GatewaySettings(database_url="", pg_password="secret!@#")
    out = resolve_database_url(s)
    assert out.startswith("postgresql+asyncpg://avt:")
    assert "secret" in out  # URL-encoded, but "secret" substring survives


def test_resolve_refuses_fallback():
    s = GatewaySettings(database_url="", pg_password="")
    with pytest.raises(RuntimeError, match="avt:avt"):
        resolve_database_url(s)


def test_config_module_imports_without_creds(monkeypatch):
    """Regression: importing gateway/config.py in a clean env must NOT raise.

    The whole point of T3's lazy design. If this test fails, someone put
    resolve_database_url call back at module scope.
    """
    monkeypatch.delenv("AVT_PG_PASSWORD", raising=False)
    monkeypatch.delenv("AVT_DATABASE_URL", raising=False)
    import importlib
    import config as cfg
    importlib.reload(cfg)  # reload is safe here — no side effects expected
    assert cfg.settings.database_url == ""  # unset, not populated
