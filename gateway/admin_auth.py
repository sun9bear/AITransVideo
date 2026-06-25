"""Shared admin authentication helpers for all gateway routers.

BACKGROUND
----------
Before this module, every admin router file contained its own copy of
``_require_admin`` / ``_is_admin``.  The copies had drifted in subtle ways:
- Return type: some returned ``User``, two returned ``None`` (monitor APIs)
- Role check: some used ``getattr(user, "role", None) != "admin"`` (no
  sentinel for missing-field), others used ``(getattr(...) or "user") != "admin"``

This single source of truth normalises behaviour: missing or falsy role is
treated as ``"user"`` (the ``or "user"`` sentinel), and the function always
returns the authenticated ``User`` object so callers that need it can use
the return value.

NOTE: gateway/pan/auth.py has its own _require_admin / _is_admin because
pan/ has an independent authentication context.  Its semantic equivalence
with this module has not been fully verified.  Migrate pan/ in a separate
PR once equivalence is confirmed (TU-05 explicit exception).

USAGE
-----
In any gateway router that needs an admin gate::

    from admin_auth import require_admin

    @router.get("/api/admin/something")
    async def my_handler(
        user: User | None = Depends(get_current_user),
    ) -> ...:
        require_admin(user)  # raises 401 / 403 for non-admin
        ...

The function is intentionally a **plain call** (not ``Depends``) so that
the existing ``test_admin_gate_coverage.py`` AST scan continues to work
unchanged (it keys on the string ``"_require_admin("`` in the route body).

WHAT IS CHECKED
---------------
``user.role == "admin"`` via the ``role`` column added in Alembic migration
002.  To bootstrap the first admin:
    UPDATE users SET role='admin' WHERE email='your-admin@example.com';
"""
from __future__ import annotations

from fastapi import HTTPException
from models import User


def is_admin(user: User) -> bool:
    """Return True iff the user has the admin role.

    Uses ``(getattr(user, "role", None) or "user")`` so that a missing or
    falsy role field is treated as the default ``"user"`` role — consistent
    with Alembic 002 which gives all pre-existing users ``role='user'``.
    """
    return (getattr(user, "role", None) or "user") == "admin"


def require_admin(user: User | None) -> User:
    """Assert that *user* is an authenticated admin.

    Raises:
        HTTPException 401: if ``user`` is None (not logged in).
        HTTPException 403: if ``user`` is logged in but not admin.

    Returns:
        The same ``user`` object (so callers that need it can chain:
        ``admin = require_admin(user)``).
    """
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ---------------------------------------------------------------------------
# Backward-compat aliases used by the gate-coverage AST scanner
# (tests/test_admin_gate_coverage.py keys on "_require_admin(")
# ---------------------------------------------------------------------------
_is_admin = is_admin
_require_admin = require_admin
