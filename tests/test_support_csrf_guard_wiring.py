from __future__ import annotations

import sys
from pathlib import Path

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

import support_api  # noqa: E402
from csrf import require_same_origin_state_change  # noqa: E402


def _route(method: str, path: str):
    for item in support_api.router.routes:
        if getattr(item, "path", None) == path and method in getattr(item, "methods", set()):
            return item
    raise AssertionError(f"route not found: {method} {path}")


def _has_csrf_dependency(route) -> bool:
    return any(
        dependency.call is require_same_origin_state_change
        for dependency in route.dependant.dependencies
    )


def test_support_visitor_cookie_writes_have_same_origin_guard():
    guarded_routes = [
        ("POST", "/api/support/conversations"),
        ("POST", "/api/support/conversations/{conversation_id}/messages"),
        ("POST", "/api/support/conversations/{conversation_id}/handoff"),
    ]

    assert all(_has_csrf_dependency(_route(method, path)) for method, path in guarded_routes)


def test_support_read_routes_keep_noop_same_origin_dependency():
    """Router-level dependency is acceptable for GET because the helper no-ops."""
    read_routes = [
        ("GET", "/api/support/config"),
        ("GET", "/api/support/online-status"),
        ("GET", "/api/support/wechat-qr"),
        ("GET", "/api/support/conversations/my/open"),
        ("GET", "/api/support/conversations/{conversation_id}"),
    ]

    assert all(_has_csrf_dependency(_route(method, path)) for method, path in read_routes)
