"""Tests for gateway/pan/_feature_gate.py.

Plan 2026-05-26 postmortem P0a (Codex feedback). When
``AVT_ENABLE_PAN_BACKUP=false``, every endpoint under ``/api/admin/pan/*``
must return 503 — both the admin_api routes (status / backups / restores
/ credentials) and the auth routes (connect / callback).

Prior to this gate, the env flag only short-circuited scheduler ticks
in gateway/pan/scheduler.py while the HTTP routers remained registered
unconditionally in gateway/main.py. An admin could still hit "Create
backup" while the flag was off → buggy concurrent executor path fired
anyway, exactly the regression that caused the 2026-05-26 disk-full
incident.

Tests run with FastAPI TestClient so the dependency chain is actually
exercised. Direct handler-call tests would NOT catch this — the gate
runs before the handler.
"""
from __future__ import annotations

import pytest


# =========================================================================
# Unit: pure function behaviour
# =========================================================================


def test_require_pan_enabled_raises_503_when_flag_off(monkeypatch):
    """The dependency function itself raises a 503 HTTPException with a
    Chinese-language detail when settings.enable_pan_backup is False."""
    from fastapi import HTTPException, status
    from config import settings
    from pan._feature_gate import require_pan_enabled

    monkeypatch.setattr(settings, "enable_pan_backup", False)
    with pytest.raises(HTTPException) as excinfo:
        require_pan_enabled()
    assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    # Must mention the env-var name so ops can find the toggle
    assert "AVT_ENABLE_PAN_BACKUP" in excinfo.value.detail


def test_require_pan_enabled_returns_none_when_flag_on(monkeypatch):
    """When flag is True, the dependency is a pure no-op (returns None)."""
    from config import settings
    from pan._feature_gate import require_pan_enabled

    monkeypatch.setattr(settings, "enable_pan_backup", True)
    # Returns None, doesn't raise
    assert require_pan_enabled() is None


# =========================================================================
# Integration: TestClient exercises the dependency chain
# =========================================================================


def _make_app_with_pan_routers():
    """Spin up a bare FastAPI app with both pan routers attached.

    Uses real router definitions from pan.admin_api / pan.auth so the
    dependency wiring is exactly what production will see — no manual
    re-wiring that could drift from prod.
    """
    from fastapi import FastAPI

    # Import lazily to honor any monkeypatched settings on the per-test
    # boundary (otherwise module-import-time settings would lock in
    # the global default before the test patches).
    from pan.admin_api import router as admin_router
    from pan.auth import router as auth_router

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(auth_router)
    return app


def test_flag_off_rejects_admin_api_endpoints(monkeypatch):
    """When flag is off, every admin_api endpoint returns 503 — does not
    matter whether the caller has admin credentials or not. The gate is
    the FIRST dependency, so it precedes auth checks."""
    from fastapi.testclient import TestClient
    from config import settings

    monkeypatch.setattr(settings, "enable_pan_backup", False)
    app = _make_app_with_pan_routers()
    client = TestClient(app, headers={"origin": "http://testserver"})

    # A representative sample of admin_api endpoints. We don't enumerate
    # all of them — the gate is a router-level dependency that applies
    # uniformly. One GET + one POST + one DELETE covers method coverage.
    probes = [
        ("GET", "/api/admin/pan/status"),
        ("POST", "/api/admin/pan/backups"),
        ("POST", "/api/admin/pan/restores"),
        ("DELETE", "/api/admin/pan/credentials"),
        ("GET", "/api/admin/pan/backups"),
    ]
    for method, path in probes:
        resp = client.request(method, path, json={})
        assert resp.status_code == 503, (
            f"{method} {path} returned {resp.status_code}, "
            f"expected 503 because flag is off. body={resp.text}"
        )
        detail = resp.json().get("detail", "")
        assert "AVT_ENABLE_PAN_BACKUP" in detail, (
            f"{method} {path} 503 detail should mention env var, got: {detail}"
        )


def test_flag_off_rejects_auth_endpoints(monkeypatch):
    """OAuth connect / callback must also be gated. Otherwise an admin
    could re-authorize Baidu Pan with the feature 'off', leaving the
    system holding fresh tokens while the rest of the API rejects."""
    from fastapi.testclient import TestClient
    from config import settings

    monkeypatch.setattr(settings, "enable_pan_backup", False)
    app = _make_app_with_pan_routers()
    client = TestClient(app, headers={"origin": "http://testserver"})

    # connect is POST, callback is GET (Baidu redirect target).
    probes = [
        ("POST", "/api/admin/pan/connect"),
        ("GET", "/api/admin/pan/callback?code=foo&state=bar"),
    ]
    for method, path in probes:
        resp = client.request(method, path)
        assert resp.status_code == 503, (
            f"{method} {path} returned {resp.status_code}, "
            f"expected 503. body={resp.text}"
        )


def test_flag_on_does_not_short_circuit_other_dependencies(monkeypatch):
    """When flag is ON, the feature gate must be a pure no-op — control
    flows on to the rest of the dependency chain (auth / csrf / DB).

    We don't assert on the eventual response code because that depends
    on DB / auth fixtures the test doesn't set up. We assert only the
    NEGATIVE: the feature-gate 503 message must NOT be in the response.
    Anything else (200, 401, 422, 500) is fine — those are downstream
    layers, not our concern.

    Uses dependency_overrides to short-circuit get_db so the test can
    reach the actual handler/auth check without a real DB."""
    from fastapi.testclient import TestClient
    from config import settings
    from database import get_db

    monkeypatch.setattr(settings, "enable_pan_backup", True)
    app = _make_app_with_pan_routers()

    async def fake_db():
        # Yield None — handlers will fail later (None.execute()), but
        # that failure is downstream of the feature gate which is all
        # we're testing.
        yield None
    app.dependency_overrides[get_db] = fake_db
    client = TestClient(
        app,
        headers={"origin": "http://testserver"},
        raise_server_exceptions=False,
    )

    resp = client.get("/api/admin/pan/status")
    # Decisive check: 503 with our specific message must NOT appear.
    if resp.status_code == 503:
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            detail = resp.text
        assert "AVT_ENABLE_PAN_BACKUP" not in detail, (
            "flag-on request hit feature gate 503 — gate is firing "
            "incorrectly"
        )


def test_feature_gate_is_first_dependency(monkeypatch):
    """Contract: ``require_pan_enabled`` must be the FIRST dependency on
    each router so the 503 message is unambiguous (feature off, not
    permission denied). If someone reorders the deps list in admin_api
    or auth, this test catches it.

    Without this ordering, a non-admin user hitting a flag-off endpoint
    would get 401 ("not logged in") which is misleading."""
    from pan.admin_api import router as admin_router
    from pan.auth import router as auth_router
    from pan._feature_gate import require_pan_enabled

    for router_name, router in (("admin_api", admin_router), ("auth", auth_router)):
        deps = router.dependencies
        assert len(deps) >= 1, f"{router_name} router has no dependencies"
        # FastAPI's Depends object stores the callable as `.dependency`
        first_callable = deps[0].dependency
        assert first_callable is require_pan_enabled, (
            f"{router_name} router's FIRST dependency should be "
            f"require_pan_enabled, got {first_callable!r}"
        )
