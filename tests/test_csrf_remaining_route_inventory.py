from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_repo_dir = Path(__file__).resolve().parent.parent
_gateway_dir = _repo_dir / "gateway"
if str(_gateway_dir) not in sys.path:
    sys.path.insert(0, str(_gateway_dir))

from csrf import require_same_origin_state_change  # noqa: E402


_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _load_gateway_main():
    spec = importlib.util.spec_from_file_location(
        "gateway_main_for_remaining_csrf_inventory",
        _gateway_dir / "main.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _route_has_csrf_dependency(route) -> bool:
    return any(
        dependency.call is require_same_origin_state_change
        for dependency in route.dependant.dependencies
    )


def test_remaining_unguarded_state_changing_routes_are_explicitly_classified():
    gateway_main = _load_gateway_main()

    remaining: set[tuple[tuple[str, ...], str]] = set()
    for route in gateway_main.app.routes:
        methods = tuple(sorted(set(getattr(route, "methods", set())) & _STATE_CHANGING_METHODS))
        if not methods:
            continue
        if _route_has_csrf_dependency(route):
            continue
        remaining.add((methods, getattr(route, "path", "?")))

    assert remaining == {
        # Billing/payment callbacks are not session-CSRF surfaces.
        (("POST",), "/api/billing/fake-pay/{order_id}"),
        (("POST",), "/api/billing/webhooks/{provider_name}"),
        # Internal routes use internal key / loopback / service auth.
        (("POST",), "/api/internal/user-voices/match"),
        (("POST",), "/api/internal/user-voices/candidates"),
        (("POST",), "/api/internal/user-voices/register-smart"),
        (("POST",), "/api/internal/user-voices/speed-profiles"),
        (("POST",), "/api/internal/user-voices/expire"),
        (("POST",), "/internal/notifications/dispatch"),
        (("POST",), "/job-api/jobs/{job_id}/source-metadata"),
        (("POST",), "/job-api/jobs/{job_id}/metering"),
        # Transparent non-jobs Job API proxy needs subpath-level audit before
        # applying session CSRF broadly.
        (("DELETE", "PATCH", "PUT"), "/job-api/{path:path}"),
    }
