"""Tests for gateway auth/me, entitlements, and admin role check.

These tests import the REAL gateway handler functions and test them directly.
Gateway modules have a deep import chain (database -> asyncpg, config ->
pydantic_settings) that fires at module load time. We stub only the
infrastructure layer (database engine) so the business logic modules
load cleanly and can be tested against real code.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub gateway infrastructure so business-logic modules can be imported
# without a live PostgreSQL connection or asyncpg driver.
# ---------------------------------------------------------------------------
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

# Provide a fake database module before anything imports it
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

# Now import the real gateway business-logic modules
from auth import me_handler  # noqa: E402
from entitlements import get_entitlements  # noqa: E402
from admin_settings import _is_admin  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(
    *,
    role: str = "user",
    plan_code: str = "free",
    email: str = "test@example.com",
    display_name: str = "Test User",
    free_jobs_quota_total: int = 5,
    free_jobs_quota_used: int = 0,
) -> SimpleNamespace:
    """Build a mock that satisfies the attribute access patterns in gateway handlers."""
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        email=email,
        display_name=display_name,
        role=role,
        plan_code=plan_code,
        free_jobs_quota_total=free_jobs_quota_total,
        free_jobs_quota_used=free_jobs_quota_used,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===================================================================
# /auth/me — tests against real gateway/auth.py::me_handler
# ===================================================================

class TestAuthMe:
    def test_logged_in_user_returns_role(self):
        result = _run(me_handler(_make_user(role="user")))
        assert result["user"]["role"] == "user"
        assert result["user"]["email"] == "test@example.com"
        assert result["user"]["display_name"] == "Test User"
        assert "id" in result["user"]
        assert "created_at" in result["user"]

    def test_logged_in_admin_returns_admin_role(self):
        result = _run(me_handler(_make_user(role="admin")))
        assert result["user"]["role"] == "admin"

    def test_not_logged_in_returns_null_user(self):
        result = _run(me_handler(None))
        assert result["user"] is None


# ===================================================================
# /api/me/entitlements — tests against real gateway/entitlements.py::get_entitlements
# ===================================================================

class TestEntitlements:
    def test_free_user(self):
        user = _make_user(plan_code="free", free_jobs_quota_total=5, free_jobs_quota_used=2)
        result = _run(get_entitlements(user))

        assert result["role"] == "user"
        assert result["plan_code"] == "free"
        limits = result["limits"]
        assert limits["max_duration_minutes"] == 10
        assert limits["max_concurrent_jobs"] == 1
        assert limits["allowed_service_modes"] == ["express"]
        assert limits["free_jobs_quota_total"] == 5
        assert limits["free_jobs_quota_used"] == 2
        assert limits["free_jobs_quota_remaining"] == 3
        assert result["ui"]["show_admin_badge"] is False
        assert result["ui"]["allow_upgrade"] is True

    def test_plus_user(self):
        result = _run(get_entitlements(_make_user(plan_code="plus")))

        assert result["plan_code"] == "plus"
        limits = result["limits"]
        assert limits["max_duration_minutes"] == 45
        assert limits["max_concurrent_jobs"] == 3
        assert "express" in limits["allowed_service_modes"]
        assert "studio" in limits["allowed_service_modes"]
        assert limits["free_jobs_quota_total"] is None
        assert limits["free_jobs_quota_used"] is None
        assert limits["free_jobs_quota_remaining"] is None
        assert result["ui"]["allow_upgrade"] is True

    def test_pro_user(self):
        result = _run(get_entitlements(_make_user(plan_code="pro")))

        assert result["plan_code"] == "pro"
        limits = result["limits"]
        assert limits["max_duration_minutes"] == 180
        assert limits["max_concurrent_jobs"] == 5
        assert "studio" in limits["allowed_service_modes"]
        assert result["ui"]["allow_upgrade"] is False

    def test_admin_user(self):
        result = _run(get_entitlements(_make_user(role="admin", plan_code="free")))

        assert result["role"] == "admin"
        limits = result["limits"]
        assert limits["max_duration_minutes"] is None
        assert limits["max_concurrent_jobs"] is None
        assert "express" in limits["allowed_service_modes"]
        assert "studio" in limits["allowed_service_modes"]
        assert limits["free_jobs_quota_total"] is None
        assert limits["free_jobs_quota_used"] is None
        assert limits["free_jobs_quota_remaining"] is None
        assert result["ui"]["show_admin_badge"] is True
        assert result["ui"]["allow_upgrade"] is False

    def test_unauthenticated_raises_401(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _run(get_entitlements(None))
        assert exc_info.value.status_code == 401


# ===================================================================
# _is_admin — tests against real gateway/admin_settings.py::_is_admin
# ===================================================================

class TestAdminRoleCheck:
    def test_admin_role_grants_access(self):
        assert _is_admin(_make_user(role="admin", email="anyone@example.com")) is True

    def test_user_role_denies_access(self):
        assert _is_admin(_make_user(role="user")) is False

    def test_email_admin_without_role_is_denied(self):
        """email='admin' alone must NOT grant admin access."""
        assert _is_admin(_make_user(role="user", email="admin")) is False

    def test_display_name_admin_without_role_is_denied(self):
        """display_name='Admin' must NOT grant admin access."""
        assert _is_admin(_make_user(role="user", display_name="Admin")) is False
