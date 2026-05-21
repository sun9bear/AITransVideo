from __future__ import annotations

import sys
from pathlib import Path

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

import admin_disk_api  # noqa: E402
import admin_settings  # noqa: E402
from csrf import require_same_origin_state_change  # noqa: E402


def _route(router, method: str, path: str):
    for item in router.routes:
        if getattr(item, "path", None) == path and method in getattr(item, "methods", set()):
            return item
    raise AssertionError(f"route not found: {method} {path}")


def _has_csrf_dependency(router, method: str, path: str) -> bool:
    route = _route(router, method, path)
    return any(
        dependency.call is require_same_origin_state_change
        for dependency in route.dependant.dependencies
    )


def test_admin_settings_write_routes_have_same_origin_guard():
    guarded_routes = [
        ("POST", "/api/admin/settings"),
        ("POST", "/api/admin/review-prompts"),
        ("POST", "/api/admin/model-toggle"),
        ("POST", "/api/admin/review-prompts/restore"),
        ("DELETE", "/api/admin/review-prompts/history/{index}"),
        ("POST", "/api/admin/jobs/{job_id}/cancel"),
        ("POST", "/api/admin/jobs/{job_id}/delete"),
        ("PATCH", "/api/admin/users/{user_id}/entitlements"),
    ]

    assert all(
        _has_csrf_dependency(admin_settings.router, method, path)
        for method, path in guarded_routes
    )


def test_admin_disk_write_routes_have_same_origin_guard():
    guarded_routes = [
        ("POST", "/api/admin/disk/cleanup-orphans"),
        ("POST", "/api/admin/disk/cleanup-expired"),
        ("POST", "/api/admin/disk/resize-filesystem"),
    ]

    assert all(
        _has_csrf_dependency(admin_disk_api.router, method, path)
        for method, path in guarded_routes
    )


def test_admin_read_routes_are_not_guarded_by_csrf_dependency():
    assert not _has_csrf_dependency(admin_settings.router, "GET", "/api/admin/settings")
    assert not _has_csrf_dependency(admin_disk_api.router, "GET", "/api/admin/disk/overview")
