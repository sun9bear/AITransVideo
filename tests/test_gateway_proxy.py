"""Tests for gateway proxy header injection (X-User-Id for Web UI routes).

Verifies that both /api/* and /web-ui-api/* proxy routes inject x-user-id
when an authenticated user is present, and that /job-api/* does NOT.

Uses importlib to load gateway/main.py under a unique module name,
avoiding collision with the project-root main.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Stub database before importing gateway modules
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

# Load gateway/main.py under a non-conflicting module name
_gw_main_path = Path(__file__).resolve().parent.parent / "gateway" / "main.py"
_spec = importlib.util.spec_from_file_location("gateway_main", str(_gw_main_path))
gw = importlib.util.module_from_spec(_spec)
sys.modules["gateway_main"] = gw
_spec.loader.exec_module(gw)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user(user_id="42"):
    return SimpleNamespace(
        id=user_id, email="u@test.com", display_name="Test",
        role="user", plan_code="free",
        free_jobs_quota_total=5, free_jobs_quota_used=0,
    )


# ===================================================================
# _user_id_headers helper
# ===================================================================

class TestUserIdHeaders:
    def test_returns_header_for_authenticated_user(self):
        result = gw._user_id_headers(_make_user("42"))
        assert result == {"x-user-id": "42"}

    def test_returns_none_for_no_user(self):
        assert gw._user_id_headers(None) is None


# ===================================================================
# proxy_web_ui (/api/*) injects x-user-id
# ===================================================================

class TestProxyWebUiHeaderInjection:
    def test_api_route_injects_user_id(self):
        """GET /api/upload-video → upstream receives x-user-id header."""
        captured_kwargs = {}

        async def fake_proxy(**kwargs):
            captured_kwargs.update(kwargs)
            from fastapi import Response as FR
            return FR(content=b'{"ok":true}', status_code=200)

        user = _make_user("99")
        with patch.object(gw, "proxy_request", side_effect=fake_proxy):
            _run(gw.proxy_web_ui(MagicMock(), "api/upload-video", user))

        assert captured_kwargs["extra_headers"] == {"x-user-id": "99"}
        assert captured_kwargs["strip_prefix"] == ""

    def test_api_route_no_user_no_header(self):
        captured_kwargs = {}

        async def fake_proxy(**kwargs):
            captured_kwargs.update(kwargs)
            from fastapi import Response as FR
            return FR(content=b'{"ok":true}', status_code=200)

        with patch.object(gw, "proxy_request", side_effect=fake_proxy):
            _run(gw.proxy_web_ui(MagicMock(), "api/state", None))

        assert captured_kwargs["extra_headers"] is None


# ===================================================================
# proxy_web_ui_legacy (/web-ui-api/*) injects x-user-id
# ===================================================================

class TestProxyWebUiLegacyHeaderInjection:
    def test_legacy_route_injects_user_id(self):
        """POST /web-ui-api/api/upload-video → upstream receives x-user-id."""
        captured_kwargs = {}

        async def fake_proxy(**kwargs):
            captured_kwargs.update(kwargs)
            from fastapi import Response as FR
            return FR(content=b'{"ok":true}', status_code=200)

        user = _make_user("77")
        with patch.object(gw, "proxy_request", side_effect=fake_proxy):
            _run(gw.proxy_web_ui_legacy(MagicMock(), "api/upload-video", user))

        assert captured_kwargs["extra_headers"] == {"x-user-id": "77"}
        assert captured_kwargs["strip_prefix"] == "/web-ui-api"

    def test_legacy_route_no_user_no_header(self):
        captured_kwargs = {}

        async def fake_proxy(**kwargs):
            captured_kwargs.update(kwargs)
            from fastapi import Response as FR
            return FR(content=b'{"ok":true}', status_code=200)

        with patch.object(gw, "proxy_request", side_effect=fake_proxy):
            _run(gw.proxy_web_ui_legacy(MagicMock(), "api/state", None))

        assert captured_kwargs["extra_headers"] is None


# ===================================================================
# /job-api/* does NOT get x-user-id injected
# ===================================================================

class TestJobApiNoHeaderInjection:
    def test_job_api_other_does_not_inject_user_id(self):
        """PUT /job-api/something → no x-user-id header."""
        captured_kwargs = {}

        async def fake_proxy(**kwargs):
            captured_kwargs.update(kwargs)
            from fastapi import Response as FR
            return FR(content=b'{"ok":true}', status_code=200)

        with patch.object(gw, "proxy_request", side_effect=fake_proxy):
            _run(gw.proxy_job_api_other(MagicMock(), "something", _make_user()))

        assert "extra_headers" not in captured_kwargs or captured_kwargs.get("extra_headers") is None
