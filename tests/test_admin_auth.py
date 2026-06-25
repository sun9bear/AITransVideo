"""Unit tests for gateway/admin_auth.py — the shared admin gate helper."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "gateway"))

import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException

from admin_auth import is_admin, require_admin, _require_admin, _is_admin


def _make_user(role):
    u = MagicMock()
    u.role = role
    return u


class TestIsAdmin:
    def test_admin_role_returns_true(self):
        assert is_admin(_make_user("admin")) is True

    def test_user_role_returns_false(self):
        assert is_admin(_make_user("user")) is False

    def test_none_role_returns_false(self):
        assert is_admin(_make_user(None)) is False

    def test_empty_string_role_returns_false(self):
        assert is_admin(_make_user("")) is False


class TestRequireAdmin:
    def test_none_user_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            require_admin(None)
        assert exc.value.status_code == 401

    def test_non_admin_raises_403(self):
        with pytest.raises(HTTPException) as exc:
            require_admin(_make_user("user"))
        assert exc.value.status_code == 403

    def test_admin_returns_user(self):
        u = _make_user("admin")
        assert require_admin(u) is u

    def test_alias_require_admin_same_as_require_admin(self):
        """_require_admin alias must behave identically."""
        u = _make_user("admin")
        assert _require_admin(u) is u

    def test_alias_is_admin_same_as_is_admin(self):
        assert _is_admin(_make_user("admin")) is True
        assert _is_admin(_make_user("user")) is False
