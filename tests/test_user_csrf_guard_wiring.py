from __future__ import annotations

import sys
import importlib.util
from pathlib import Path

_repo_dir = Path(__file__).resolve().parent.parent
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

import background_task_api  # noqa: E402
import auth_email  # noqa: E402
import auth_phone  # noqa: E402
import billing  # noqa: E402
import materials_api  # noqa: E402
import notifications_api  # noqa: E402
import user_voice_api  # noqa: E402
from csrf import require_same_origin_state_change  # noqa: E402


_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_SESSION_USER_ROUTERS = [
    ("background_task_api", background_task_api.router),
    ("materials_api", materials_api.router),
    ("notifications_api", notifications_api.router),
    ("user_voice_api", user_voice_api.router),
]

_AUTH_FLOW_ROUTERS = [
    ("auth_email", auth_email.router),
    ("auth_phone", auth_phone.router),
]


def _route_has_csrf_dependency(route) -> bool:
    return any(
        dependency.call is require_same_origin_state_change
        for dependency in route.dependant.dependencies
    )


def _find_route(router, method: str, path: str):
    for route in router.routes:
        if path == getattr(route, "path", "") and method in getattr(route, "methods", set()):
            return route
    raise AssertionError(f"route not found: {method} {path}")


def _load_gateway_main():
    spec = importlib.util.spec_from_file_location(
        "gateway_main_for_csrf_wiring",
        _repo_dir / "gateway" / "main.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_priority_session_user_write_routers_have_same_origin_guard():
    gaps: list[str] = []
    for label, router in _SESSION_USER_ROUTERS:
        for route in router.routes:
            write_methods = sorted(
                set(getattr(route, "methods", set())) & _STATE_CHANGING_METHODS
            )
            if not write_methods:
                continue
            if _route_has_csrf_dependency(route):
                continue
            gaps.append(
                f"{label}: {','.join(write_methods)} "
                f"{getattr(route, 'path', '?')}"
            )

    assert gaps == []


def test_internal_user_write_routers_are_not_given_session_csrf_guard():
    for router in [
        notifications_api.internal_router,
        user_voice_api.internal_router,
    ]:
        for route in router.routes:
            assert not _route_has_csrf_dependency(route)


def test_auth_flow_write_routers_have_same_origin_guard():
    gaps: list[str] = []
    for label, router in _AUTH_FLOW_ROUTERS:
        for route in router.routes:
            write_methods = sorted(
                set(getattr(route, "methods", set())) & _STATE_CHANGING_METHODS
            )
            if not write_methods:
                continue
            if _route_has_csrf_dependency(route):
                continue
            gaps.append(
                f"{label}: {','.join(write_methods)} "
                f"{getattr(route, 'path', '?')}"
            )

    assert gaps == []


def test_billing_order_has_guard_but_callbacks_stay_exempt():
    assert _route_has_csrf_dependency(
        _find_route(billing.router, "POST", "/api/billing/orders")
    )

    exempt_routes = [
        ("POST", "/api/billing/fake-pay/{order_id}"),
        ("POST", "/api/billing/webhooks/{provider_name}"),
    ]
    for method, path in exempt_routes:
        assert not _route_has_csrf_dependency(_find_route(billing.router, method, path))


def test_direct_main_write_routes_have_same_origin_guard():
    gateway_main = _load_gateway_main()
    expected_routes = {
        ("POST", "/auth/register"),
        ("POST", "/auth/login"),
        ("POST", "/auth/logout"),
        ("POST", "/api/account/change-password"),
        ("POST", "/api/account/bind-email"),
        ("POST", "/gateway/upload-video"),
        ("PATCH", "/gateway/jobs/{job_id}"),
        ("POST", "/job-api/jobs"),
        ("DELETE", "/job-api/jobs/{job_id}"),
        ("POST", "/job-api/jobs/{job_id}/voice-clone"),
        ("POST", "/job-api/jobs/{job_id}/voice-match"),
        ("POST", "/job-api/jobs/{job_id}/voice-candidates"),
        ("POST", "/job-api/jobs/{job_id}/{subpath:path}"),
    }
    found = set()
    missing_guard = []

    for route in gateway_main.app.routes:
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", set()))
        for item in expected_routes:
            method, expected_path = item
            if path != expected_path or method not in methods:
                continue
            found.add(item)
            if not _route_has_csrf_dependency(route):
                missing_guard.append(f"{method} {path}")

    assert found == expected_routes
    assert missing_guard == []
