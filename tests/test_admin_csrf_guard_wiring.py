from __future__ import annotations

import sys
from pathlib import Path

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

import admin_cost_api  # noqa: E402
import admin_cosyvoice_control_api  # noqa: E402
import admin_disk_api  # noqa: E402
import admin_job_monitor_api  # noqa: E402
import admin_settings  # noqa: E402
import admin_smart_analytics_api  # noqa: E402
import admin_support_api  # noqa: E402
import cost_management  # noqa: E402
import credits_observability  # noqa: E402
import pricing_admin  # noqa: E402
import s2_monitor_api  # noqa: E402
import traffic_analytics  # noqa: E402
import voice_catalog_api  # noqa: E402
from csrf import require_same_origin_state_change  # noqa: E402
from pan import admin_api as pan_admin_api  # noqa: E402
from pan import auth as pan_auth  # noqa: E402


_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_SESSION_ADMIN_ROUTERS = [
    ("admin_cost_api", admin_cost_api.router),
    ("admin_cosyvoice_control_api", admin_cosyvoice_control_api.router),
    ("admin_disk_api", admin_disk_api.router),
    ("admin_job_monitor_api", admin_job_monitor_api.router),
    ("admin_settings", admin_settings.router),
    ("admin_smart_analytics_api", admin_smart_analytics_api.router),
    ("admin_support_api", admin_support_api.router),
    ("cost_management", cost_management.router),
    ("credits_observability", credits_observability.router),
    ("pan.admin_api", pan_admin_api.router),
    ("pan.auth", pan_auth.router),
    ("pricing_admin", pricing_admin.router),
    ("s2_monitor_api", s2_monitor_api.router),
    ("traffic_analytics", traffic_analytics.router),
    ("voice_catalog_api", voice_catalog_api.router),
]


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


def _route_has_csrf_dependency(route) -> bool:
    return any(
        dependency.call is require_same_origin_state_change
        for dependency in route.dependant.dependencies
    )


def test_all_session_admin_write_routes_have_same_origin_guard():
    gaps: list[str] = []
    for label, router in _SESSION_ADMIN_ROUTERS:
        for route in router.routes:
            route_methods = set(getattr(route, "methods", set()))
            write_methods = sorted(route_methods & _STATE_CHANGING_METHODS)
            if not write_methods:
                continue
            if _route_has_csrf_dependency(route):
                continue
            gaps.append(
                f"{label}: {','.join(write_methods)} "
                f"{getattr(route, 'path', '?')}"
            )

    assert gaps == []


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
