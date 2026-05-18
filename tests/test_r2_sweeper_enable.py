"""CodeX P2-4 finding (2026-05-18): sweeper enable check must use
effective settings (post-startup-downgrade), not raw env.

Before fix: env=r2 + creds missing + push_enabled=true → startup downgrades
settings.download_redirect_backend to 'local', but sweeper reads env directly
and runs anyway → publish_failed flood.

After fix: sweeper respects the downgrade and stays idle when settings say 'local'.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
GATEWAY_DIR = REPO / "gateway"
SRC_DIR = REPO / "src"
for _p in (str(GATEWAY_DIR), str(SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Stub `database` so module-level `from database import async_session`
# in r2_artifact_sweeper.py doesn't try to read real env / engine.
_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)


def test_sweeper_disabled_when_settings_downgraded_to_local(monkeypatch):
    """Env says r2 + push enabled, but settings was downgraded to local at
    startup (creds missing) → sweeper must report disabled.

    CodeX P2-4: _is_enabled() was reading os.environ directly, bypassing the
    safety net applied by validate_r2_backend() at startup.
    """
    monkeypatch.setenv("AVT_DOWNLOAD_REDIRECT_BACKEND", "r2")
    monkeypatch.setenv("AVT_R2_PROACTIVE_PUSH_ENABLED", "true")
    # Force settings to reflect post-downgrade state (creds were missing at startup)
    from config import settings
    monkeypatch.setattr(settings, "download_redirect_backend", "local")

    from r2_artifact_sweeper import _is_enabled
    assert _is_enabled() is False, (
        "sweeper must NOT run when settings.download_redirect_backend was "
        "downgraded to 'local' at startup (CodeX P2-4)"
    )


def test_sweeper_enabled_when_settings_r2_and_push_enabled(monkeypatch):
    """Happy path regression: full r2 mode with valid creds → sweeper enabled."""
    monkeypatch.setenv("AVT_DOWNLOAD_REDIRECT_BACKEND", "r2")
    monkeypatch.setenv("AVT_R2_PROACTIVE_PUSH_ENABLED", "true")
    from config import settings
    monkeypatch.setattr(settings, "download_redirect_backend", "r2")

    from r2_artifact_sweeper import _is_enabled
    assert _is_enabled() is True


def test_sweeper_disabled_when_push_flag_off(monkeypatch):
    """Even with r2 mode, if push flag is off, sweeper is idle."""
    monkeypatch.setenv("AVT_R2_PROACTIVE_PUSH_ENABLED", "false")
    from config import settings
    monkeypatch.setattr(settings, "download_redirect_backend", "r2")

    from r2_artifact_sweeper import _is_enabled
    assert _is_enabled() is False


def test_sweeper_disabled_when_backend_local(monkeypatch):
    """Default local mode → sweeper idle."""
    monkeypatch.setenv("AVT_R2_PROACTIVE_PUSH_ENABLED", "true")
    from config import settings
    monkeypatch.setattr(settings, "download_redirect_backend", "local")

    from r2_artifact_sweeper import _is_enabled
    assert _is_enabled() is False
