"""Tests for Phase 4: Admin user management and audit log.

Tests import real gateway modules. DB is stubbed at infrastructure level only.
"""
from __future__ import annotations

import asyncio
import sys
import types
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from admin_auth import _is_admin, _require_admin  # noqa: E402
from admin_settings import (  # noqa: E402
    list_users,
    update_user_entitlements,
    get_user_audit_log,
    UpdateEntitlementsRequest,
    VALID_ROLES,
    VALID_PLAN_CODES,
)
from models import AdminAuditLog, User  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user(*, role="admin", plan_code="free", email="admin@test.com",
               uid=None, quota_total=5, quota_used=0):
    return SimpleNamespace(
        id=uid or str(uuid.uuid4()),
        email=email, display_name="Test User", role=role,
        plan_code=plan_code, free_jobs_quota_total=quota_total,
        free_jobs_quota_used=quota_used,
        is_active=True, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        password_hash="xxx",
    )


def _mock_async_session():
    """Create a mock that works as `async with async_session() as db:`"""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, db


# ===================================================================
# Validation
# ===================================================================

class TestValidation:
    def test_valid_roles(self):
        assert VALID_ROLES == {"user", "admin"}

    def test_valid_plan_codes(self):
        assert VALID_PLAN_CODES == {"free", "plus", "pro"}

    def test_require_admin_rejects_non_admin(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _require_admin(_make_user(role="user"))
        assert exc_info.value.status_code == 403

    def test_require_admin_rejects_none(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _require_admin(None)
        assert exc_info.value.status_code == 401

    def test_require_admin_passes_admin(self):
        result = _require_admin(_make_user(role="admin"))
        assert result.role == "admin"


# ===================================================================
# GET /api/admin/users — real list_users function
# ===================================================================

class TestListUsers:
    def test_returns_user_list(self):
        admin = _make_user(role="admin")
        user1 = _make_user(role="user", email="a@t.com", uid="uid-1")
        user2 = _make_user(role="admin", email="b@t.com", uid="uid-2")

        ctx, db = _mock_async_session()

        # First query: users
        users_result = MagicMock()
        users_scalars = MagicMock()
        users_scalars.all.return_value = [user1, user2]
        users_result.scalars.return_value = users_scalars

        # Second query: active job counts
        active_result = MagicMock()
        active_result.all.return_value = [("uid-1", 2)]

        # Third query: total job counts
        total_result = MagicMock()
        total_result.all.return_value = [("uid-1", 5), ("uid-2", 3)]

        call_n = {"n": 0}
        async def smart_execute(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1: return users_result
            if call_n["n"] == 2: return active_result
            return total_result
        db.execute = smart_execute

        with patch("admin_settings.async_session", return_value=ctx):
            result = _run(list_users(admin))

        assert len(result["users"]) == 2
        u1 = next(u for u in result["users"] if u["email"] == "a@t.com")
        assert u1["role"] == "user"
        assert u1["active_jobs"] == 2
        assert u1["total_jobs"] == 5
        u2 = next(u for u in result["users"] if u["email"] == "b@t.com")
        assert u2["total_jobs"] == 3

    def test_rejects_non_admin(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _run(list_users(_make_user(role="user")))
        assert exc_info.value.status_code == 403


# ===================================================================
# PATCH /api/admin/users/{id}/entitlements — real function
# ===================================================================

class TestUpdateEntitlements:
    def test_update_role(self):
        admin = _make_user(role="admin", uid="admin-1")
        target = _make_user(role="user", email="target@t.com", uid="target-1")

        ctx, db = _mock_async_session()
        # select User by id → target
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = target
        # admin count query → 2 admins (safe to demote)
        count_result = MagicMock()
        count_result.scalar.return_value = 2

        call_n = {"n": 0}
        async def smart_execute(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1: return user_result
            return count_result
        db.execute = smart_execute

        body = UpdateEntitlementsRequest(role="admin")

        with patch("admin_settings.async_session", return_value=ctx):
            result = _run(update_user_entitlements("target-1", body, admin))

        assert result["updated"] is True
        assert result["user"]["role"] == "admin"
        assert any(c["field"] == "role" for c in result["changes"])
        # Verify audit log was written
        db.add.assert_called()

    def test_update_plan_code(self):
        admin = _make_user(role="admin", uid="admin-1")
        target = _make_user(role="user", plan_code="free", uid="target-1")

        ctx, db = _mock_async_session()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = target
        db.execute = AsyncMock(return_value=user_result)

        body = UpdateEntitlementsRequest(plan_code="plus")

        with patch("admin_settings.async_session", return_value=ctx):
            result = _run(update_user_entitlements("target-1", body, admin))

        assert result["updated"] is True
        assert result["user"]["plan_code"] == "plus"

    def test_no_change_returns_not_updated(self):
        admin = _make_user(role="admin")
        target = _make_user(role="user", plan_code="free")

        ctx, db = _mock_async_session()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = target
        db.execute = AsyncMock(return_value=user_result)

        body = UpdateEntitlementsRequest(role="user", plan_code="free")

        with patch("admin_settings.async_session", return_value=ctx):
            result = _run(update_user_entitlements(str(target.id), body, admin))

        assert result["updated"] is False

    def test_user_not_found_returns_404(self):
        from fastapi import HTTPException
        admin = _make_user(role="admin")

        ctx, db = _mock_async_session()
        none_result = MagicMock()
        none_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=none_result)

        body = UpdateEntitlementsRequest(role="admin")

        with patch("admin_settings.async_session", return_value=ctx):
            with pytest.raises(HTTPException) as exc_info:
                _run(update_user_entitlements("nonexistent", body, admin))
        assert exc_info.value.status_code == 404

    def test_invalid_role_returns_400(self):
        from fastapi import HTTPException
        admin = _make_user(role="admin")
        target = _make_user(role="user")

        ctx, db = _mock_async_session()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = target
        db.execute = AsyncMock(return_value=user_result)

        body = UpdateEntitlementsRequest(role="superadmin")

        with patch("admin_settings.async_session", return_value=ctx):
            with pytest.raises(HTTPException) as exc_info:
                _run(update_user_entitlements(str(target.id), body, admin))
        assert exc_info.value.status_code == 400

    def test_invalid_plan_code_returns_400(self):
        from fastapi import HTTPException
        admin = _make_user(role="admin")
        target = _make_user(role="user")

        ctx, db = _mock_async_session()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = target
        db.execute = AsyncMock(return_value=user_result)

        body = UpdateEntitlementsRequest(plan_code="enterprise")

        with patch("admin_settings.async_session", return_value=ctx):
            with pytest.raises(HTTPException) as exc_info:
                _run(update_user_entitlements(str(target.id), body, admin))
        assert exc_info.value.status_code == 400

    def test_last_admin_cannot_be_demoted(self):
        """Demoting the only admin returns 409."""
        from fastapi import HTTPException
        admin = _make_user(role="admin", uid="sole-admin")
        # target is the same admin being demoted
        target = _make_user(role="admin", uid="sole-admin", email="sole@t.com")

        ctx, db = _mock_async_session()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = target
        # Admin count = 1 (sole admin)
        count_result = MagicMock()
        count_result.scalar.return_value = 1

        call_n = {"n": 0}
        async def smart_execute(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1: return user_result
            return count_result
        db.execute = smart_execute

        body = UpdateEntitlementsRequest(role="user")

        with patch("admin_settings.async_session", return_value=ctx):
            with pytest.raises(HTTPException) as exc_info:
                _run(update_user_entitlements("sole-admin", body, admin))
        assert exc_info.value.status_code == 409
        assert "至少需要保留一个管理员" in exc_info.value.detail

    def test_demoting_admin_when_multiple_admins_succeeds(self):
        admin = _make_user(role="admin", uid="admin-1")
        target = _make_user(role="admin", uid="admin-2", email="other@t.com")

        ctx, db = _mock_async_session()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = target
        count_result = MagicMock()
        count_result.scalar.return_value = 2  # Two admins, safe to demote one

        call_n = {"n": 0}
        async def smart_execute(*a, **kw):
            call_n["n"] += 1
            if call_n["n"] == 1: return user_result
            return count_result
        db.execute = smart_execute

        body = UpdateEntitlementsRequest(role="user")

        with patch("admin_settings.async_session", return_value=ctx):
            result = _run(update_user_entitlements("admin-2", body, admin))

        assert result["updated"] is True
        assert result["user"]["role"] == "user"

    def test_negative_quota_total_returns_400(self):
        from fastapi import HTTPException
        admin = _make_user(role="admin")
        target = _make_user(role="user", quota_total=5, quota_used=0)

        ctx, db = _mock_async_session()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = target
        db.execute = AsyncMock(return_value=user_result)

        body = UpdateEntitlementsRequest(free_jobs_quota_total=-1)

        with patch("admin_settings.async_session", return_value=ctx):
            with pytest.raises(HTTPException) as exc_info:
                _run(update_user_entitlements(str(target.id), body, admin))
        assert exc_info.value.status_code == 400
        assert "不能为负数" in exc_info.value.detail

    def test_negative_quota_used_returns_400(self):
        from fastapi import HTTPException
        admin = _make_user(role="admin")
        target = _make_user(role="user", quota_total=5, quota_used=0)

        ctx, db = _mock_async_session()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = target
        db.execute = AsyncMock(return_value=user_result)

        body = UpdateEntitlementsRequest(free_jobs_quota_used=-1)

        with patch("admin_settings.async_session", return_value=ctx):
            with pytest.raises(HTTPException) as exc_info:
                _run(update_user_entitlements(str(target.id), body, admin))
        assert exc_info.value.status_code == 400

    def test_used_exceeding_total_returns_400(self):
        from fastapi import HTTPException
        admin = _make_user(role="admin")
        target = _make_user(role="user", quota_total=5, quota_used=0)

        ctx, db = _mock_async_session()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = target
        db.execute = AsyncMock(return_value=user_result)

        body = UpdateEntitlementsRequest(free_jobs_quota_used=10)  # exceeds total=5

        with patch("admin_settings.async_session", return_value=ctx):
            with pytest.raises(HTTPException) as exc_info:
                _run(update_user_entitlements(str(target.id), body, admin))
        assert exc_info.value.status_code == 400
        assert "不能大于" in exc_info.value.detail

    def test_used_exceeding_new_total_returns_400(self):
        """Setting total=3 with existing used=4 → 400."""
        from fastapi import HTTPException
        admin = _make_user(role="admin")
        target = _make_user(role="user", quota_total=5, quota_used=4)

        ctx, db = _mock_async_session()
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = target
        db.execute = AsyncMock(return_value=user_result)

        body = UpdateEntitlementsRequest(free_jobs_quota_total=3)  # used=4 > new total=3

        with patch("admin_settings.async_session", return_value=ctx):
            with pytest.raises(HTTPException) as exc_info:
                _run(update_user_entitlements(str(target.id), body, admin))
        assert exc_info.value.status_code == 400


# ===================================================================
# GET /api/admin/users/{id}/audit-log — real function
# ===================================================================

class TestAuditLog:
    def test_returns_audit_entries(self):
        admin = _make_user(role="admin")

        entry = SimpleNamespace(
            id=str(uuid.uuid4()),
            admin_user_id="admin-1",
            target_user_id="target-1",
            action="update_role",
            field_name="role",
            old_value="user",
            new_value="admin",
            created_at=datetime(2026, 3, 29, tzinfo=timezone.utc),
        )

        ctx, db = _mock_async_session()
        result_mock = MagicMock()
        result_mock.all.return_value = [(entry, "admin@test.com")]
        db.execute = AsyncMock(return_value=result_mock)

        with patch("admin_settings.async_session", return_value=ctx):
            result = _run(get_user_audit_log("target-1", admin))

        assert len(result["entries"]) == 1
        e = result["entries"][0]
        assert e["action"] == "update_role"
        assert e["field_name"] == "role"
        assert e["old_value"] == "user"
        assert e["new_value"] == "admin"
        assert e["admin_email"] == "admin@test.com"

    def test_empty_audit_log(self):
        admin = _make_user(role="admin")

        ctx, db = _mock_async_session()
        result_mock = MagicMock()
        result_mock.all.return_value = []
        db.execute = AsyncMock(return_value=result_mock)

        with patch("admin_settings.async_session", return_value=ctx):
            result = _run(get_user_audit_log("target-1", admin))

        assert result["entries"] == []

    def test_rejects_non_admin(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _run(get_user_audit_log("target-1", _make_user(role="user")))


# ===================================================================
# Model checks
# ===================================================================

class TestAuditLogModel:
    def test_audit_log_fields_exist(self):
        columns = {c.name for c in AdminAuditLog.__table__.columns}
        assert "id" in columns
        assert "admin_user_id" in columns
        assert "target_user_id" in columns
        assert "action" in columns
        assert "field_name" in columns
        assert "old_value" in columns
        assert "new_value" in columns
        assert "created_at" in columns

    def test_audit_log_indexes(self):
        index_names = {idx.name for idx in AdminAuditLog.__table__.indexes}
        assert "idx_audit_target_user" in index_names
        assert "idx_audit_created_at" in index_names
