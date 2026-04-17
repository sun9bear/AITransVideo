"""Regression guards for T2.2: admin routes must send X-Internal-Key when
calling the Job API.

Before 2026-04-17 T2.2, ``admin_job_monitor_api.py``, ``admin_settings.py``
and ``s2_monitor_api.py`` all talked to the upstream Job API **without**
sending ``X-Internal-Key`` — they relied only on loopback + ``_require_admin``.
T2.2 added the header via a shared ``gateway/internal_auth.internal_headers()``
helper. These tests lock that down: if anyone later refactors out the header
(e.g. drops the ``headers=internal_headers()`` kwarg from
``httpx.AsyncClient(...)``), the admin → Job API call loses its auth header
and the Job API's internal-path check will 403 in production.

Scope is intentionally narrow: one guard per admin module, covering the
hottest call site per file. Covering every admin endpoint is not useful —
if the helper is invoked correctly anywhere in a module, it's correct
everywhere in that module (the same ``internal_headers()`` function is used).
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

# Stub database before importing gateway modules (matches other gateway tests)
_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
_fake_database.init_db = MagicMock()
sys.modules.setdefault("database", _fake_database)
if not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = MagicMock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OriginalAsyncClient = httpx.AsyncClient


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _admin_user():
    """Minimal admin user stand-in for ``_require_admin`` checks."""
    return SimpleNamespace(
        id="admin-uuid",
        role="admin",
        email="admin@test.local",
    )


def _install_capture_transport(monkeypatch, target_module_path: str, mock_response_json: dict):
    """Patch httpx.AsyncClient in target module so every outbound request is
    captured in the returned dict, and all responses return ``mock_response_json``.

    Returns a list that will accumulate captured request dicts with keys:
        url, method, headers
    """
    captured: list[dict] = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        captured.append({
            "url": str(request.url),
            "method": request.method,
            "headers": dict(request.headers),
        })
        return httpx.Response(200, json=mock_response_json)

    transport = httpx.MockTransport(mock_handler)

    def _async_client_factory(**kwargs):
        # Drop any 'transport' kwarg the production code might pass (none do
        # today) and replace with our capture transport.
        kwargs.pop("transport", None)
        return _OriginalAsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(f"{target_module_path}.httpx.AsyncClient", _async_client_factory)
    return captured


# ---------------------------------------------------------------------------
# admin_job_monitor_api.admin_get_job_logs
# ---------------------------------------------------------------------------

class TestAdminJobMonitorInternalKey:
    def test_admin_get_job_logs_sends_x_internal_key(self, monkeypatch):
        """T2.2 guard: admin log fetch must send X-Internal-Key header.

        If someone removes ``headers=internal_headers()`` from the
        ``httpx.AsyncClient(...)`` call in admin_job_monitor_api.py, this
        test fails because the captured request no longer carries the header.
        """
        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-admin-key-abc123")

        import admin_job_monitor_api as mod

        captured = _install_capture_transport(
            monkeypatch,
            "admin_job_monitor_api",
            {"events": []},  # shape the endpoint would normally forward
        )

        _run(mod.admin_get_job_logs("job-xyz", _admin_user()))

        assert len(captured) == 1, f"expected 1 request, got {len(captured)}"
        req = captured[0]
        # URL came from settings.job_api_upstream + /jobs/<id>/logs
        assert req["url"].endswith("/jobs/job-xyz/logs"), req["url"]
        # The critical guard — header must be present with exact value
        assert req["headers"].get("x-internal-key") == "test-admin-key-abc123", (
            f"admin_get_job_logs lost its X-Internal-Key header. "
            f"Got headers: {sorted(req['headers'].keys())}"
        )


# ---------------------------------------------------------------------------
# admin_settings.list_all_jobs
# ---------------------------------------------------------------------------

class TestAdminSettingsInternalKey:
    def test_list_all_jobs_sends_x_internal_key(self, monkeypatch):
        """T2.2 guard: admin list-all-jobs must send X-Internal-Key header."""
        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-admin-settings-key")

        import admin_settings as mod

        captured = _install_capture_transport(
            monkeypatch,
            "admin_settings",
            {"jobs": []},
        )

        # list_all_jobs only calls Job API once (Job API fetch); the rest of
        # the function hits the local DB which we don't need to test here.
        # If the implementation later adds more httpx calls / DB queries, we
        # stub out the DB sessions with AsyncMock to isolate.
        _fake_async_session = MagicMock()
        _fake_async_session.return_value.__aenter__ = AsyncMock(
            return_value=MagicMock(execute=AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )
        _fake_async_session.return_value.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(mod, "async_session", _fake_async_session)

        _run(mod.list_all_jobs(_admin_user()))

        # list_all_jobs fires exactly one external httpx call (Job API). It
        # may also do DB reads, which we stubbed above; those don't go via
        # httpx so they don't show up in `captured`.
        assert captured, "expected at least 1 httpx request"
        req = captured[0]
        assert req["url"].endswith("/jobs"), req["url"]
        assert req["headers"].get("x-internal-key") == "test-admin-settings-key", (
            f"list_all_jobs lost its X-Internal-Key header. "
            f"Got headers: {sorted(req['headers'].keys())}"
        )


# ---------------------------------------------------------------------------
# s2_monitor_api — bonus, because it also added X-Internal-Key in T2.2
# ---------------------------------------------------------------------------

class TestS2MonitorInternalKey:
    def test_s2_monitor_dashboard_sends_x_internal_key(self, monkeypatch):
        """T2.2 guard: S2 monitor dashboard must send X-Internal-Key when
        fetching the job list from Job API."""
        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "test-s2-key-xyz")

        import s2_monitor_api as mod

        captured = _install_capture_transport(
            monkeypatch,
            "s2_monitor_api",
            {"jobs": []},
        )

        # The dashboard hits Job API once for the jobs list then does a DB
        # enrichment pass. Stub the session so we don't need a real DB.
        _fake_async_session = MagicMock()
        _fake_async_session.return_value.__aenter__ = AsyncMock(
            return_value=MagicMock(execute=AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )
        _fake_async_session.return_value.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(mod, "async_session", _fake_async_session)

        # The handler is `get_s2_stats` (name reflects "S2 stats" endpoint
        # semantics; full signature has many Query params with defaults).
        handler = getattr(mod, "get_s2_stats", None)
        assert handler is not None, "Could not locate get_s2_stats handler"
        _run(handler(
            _admin_user(),
            days=7,
            limit=50,
            offset=0,
            service_mode="all",
            review_model="",
        ))

        assert captured, "expected at least 1 httpx request"
        req = captured[0]
        assert req["url"].endswith("/jobs"), req["url"]
        assert req["headers"].get("x-internal-key") == "test-s2-key-xyz", (
            f"s2_monitor_api lost its X-Internal-Key header. "
            f"Got headers: {sorted(req['headers'].keys())}"
        )


# ---------------------------------------------------------------------------
# Shared helper: confirm internal_headers() behavior is correct
# ---------------------------------------------------------------------------

class TestInternalHeadersHelper:
    def test_includes_key_when_env_set(self, monkeypatch):
        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "hello-world-32-chars-xxxxxxxxxxxxx")
        from internal_auth import internal_headers
        h = internal_headers()
        assert h["Content-Type"] == "application/json"
        assert h["X-Internal-Key"] == "hello-world-32-chars-xxxxxxxxxxxxx"

    def test_omits_key_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("AVT_INTERNAL_API_KEY", raising=False)
        from internal_auth import internal_headers
        h = internal_headers()
        assert h["Content-Type"] == "application/json"
        assert "X-Internal-Key" not in h

    def test_reads_env_at_call_time_not_import_time(self, monkeypatch):
        """Regression: internal_headers() must read env at call time so
        test fixtures / config reload can change the value without a
        module reload."""
        from internal_auth import internal_headers
        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "initial-key")
        h1 = internal_headers()
        monkeypatch.setenv("AVT_INTERNAL_API_KEY", "updated-key")
        h2 = internal_headers()
        assert h1["X-Internal-Key"] == "initial-key"
        assert h2["X-Internal-Key"] == "updated-key"
