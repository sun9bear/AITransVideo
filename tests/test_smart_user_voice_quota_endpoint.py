"""Gateway-side guards for PR#3C-b3e: smart user-voice quota endpoint.

These tests focus on the contract surface (AdminSettings field, router
registration, response shape) without spinning up a full FastAPI
TestClient — the SQLAlchemy AsyncSession mocking burden is high and
the actual SQL/HTTP plumbing mirrors the battle-tested
``internal_lookup_user_voices_by_ids`` endpoint right above it.

The app-side helper ``_fetch_smart_user_voice_quota_remaining`` has its
own dedicated tests in test_smart_business_logic.py — together they
pin the Codex 第二十七轮 P0 atomic invariant from both ends.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
_GATEWAY = _REPO / "gateway"
if str(_GATEWAY) not in sys.path:
    sys.path.insert(0, str(_GATEWAY))


class TestAdminSettingsSmartVoiceCloneCap:
    """PR#3C-b3e: ``smart_user_voice_clone_cap`` is the admin-tunable
    per-user soft cap that drives the Gateway quota endpoint."""

    def test_field_exists_with_default_30(self):
        """Default cap = 30 mirrors MiniMax's commonly-cited per-account
        voice quota. Admin can tune via admin_settings.json."""
        from admin_settings import AdminSettings

        s = AdminSettings()
        assert hasattr(s, "smart_user_voice_clone_cap"), (
            "AdminSettings missing smart_user_voice_clone_cap field — "
            "PR#3C-b3e quota endpoint reads this; falling back to "
            "hardcoded 30 would mask admin config drift."
        )
        assert s.smart_user_voice_clone_cap == 30, (
            f"Default cap drifted to {s.smart_user_voice_clone_cap}; "
            f"PR#3C-b3e committed to 30 (MiniMax per-account limit). "
            f"If you intentionally moved it, update both this test and "
            f"the docstring in admin_settings.py."
        )

    def test_field_accepts_admin_override(self):
        """Admin can override via JSON config — must accept int."""
        from admin_settings import AdminSettings

        s = AdminSettings(smart_user_voice_clone_cap=50)
        assert s.smart_user_voice_clone_cap == 50

        s = AdminSettings(smart_user_voice_clone_cap=10)
        assert s.smart_user_voice_clone_cap == 10


class TestQuotaEndpointRegistration:
    """PR#3C-b3e: ``GET /api/internal/user-voices/quota`` must be wired
    into the internal_router so the Caddyfile @internal_block can
    properly shield it (P0-2b audit 2026-05-07 pattern)."""

    def test_endpoint_registered_on_internal_router(self):
        """Pin the route path + method + internal-router membership."""
        import user_voice_api

        routes = [
            (r.path, r.methods)
            for r in user_voice_api.internal_router.routes
        ]
        quota_routes = [
            (path, methods)
            for path, methods in routes
            if path.endswith("/user-voices/quota")
        ]
        assert quota_routes, (
            "GET /api/internal/user-voices/quota not registered on "
            "internal_router. PR#3C-b3e wires it for smart's quota "
            "snapshot. Without it the app helper's HTTP call always "
            "404s → None → smart always fail-closed handoffs.\n"
            f"Available internal routes: {routes}"
        )
        # The route path includes the router's prefix
        # (/api/internal/user-voices/quota).
        path, methods = quota_routes[0]
        assert path == "/api/internal/user-voices/quota", path
        assert "GET" in methods, f"Expected GET method, got {methods}"

    def test_endpoint_not_on_public_router(self):
        """The quota endpoint MUST live on internal_router (with
        ``/api/internal`` prefix), NOT on the public ``router`` (which
        carries ``/gateway`` prefix). Public exposure would let any
        authenticated user read another user's library count."""
        import user_voice_api

        public_routes = [
            r.path for r in user_voice_api.router.routes
        ]
        quota_public = [p for p in public_routes if "quota" in p]
        assert not quota_public, (
            f"Quota endpoint accidentally registered on public router: "
            f"{quota_public}. PR#3C-b3e must keep it internal-only."
        )


class TestQuotaEndpointBusinessLogic:
    """Validate the endpoint's computation logic (call the function
    directly with mocked dependencies). This is lighter than a full
    TestClient + DB harness."""

    @pytest.mark.asyncio
    async def test_quota_returns_remaining_with_default_cap(
        self, monkeypatch, tmp_path,
    ):
        """End-to-end internal call: used=5, cap=30 (admin default) →
        remaining=25. Verifies COUNT query + admin cap read + arithmetic.
        """
        from unittest.mock import AsyncMock, MagicMock

        import user_voice_api

        # Mock the auth gate to pass.
        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )

        # Mock load_settings → default cap=30
        import admin_settings as _admin_settings_mod
        monkeypatch.setattr(
            _admin_settings_mod, "load_settings",
            lambda: _admin_settings_mod.AdminSettings(),  # default cap=30
        )

        # Mock the DB session: db.execute returns a result whose
        # .scalar() yields the used count.
        fake_result = MagicMock()
        fake_result.scalar.return_value = 5  # 5 voices used
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        # Build a fake request (no body needed).
        fake_req = MagicMock()
        valid_uuid = "00000000-0000-0000-0000-000000000001"

        resp = await user_voice_api.internal_user_voice_quota(
            request=fake_req,
            user_id=valid_uuid,
            db=fake_db,
        )

        import json
        body = json.loads(resp.body.decode("utf-8"))
        assert body == {
            "user_id": valid_uuid,
            "used": 5,
            "limit": 30,
            "remaining": 25,
        }

    @pytest.mark.asyncio
    async def test_quota_clamps_remaining_to_zero(self, monkeypatch):
        """When used > cap (e.g. admin lowered cap after voices already
        existed), remaining must clamp to 0 — never negative."""
        from unittest.mock import AsyncMock, MagicMock

        import user_voice_api
        import admin_settings as _admin_settings_mod

        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )
        monkeypatch.setattr(
            _admin_settings_mod, "load_settings",
            lambda: _admin_settings_mod.AdminSettings(
                smart_user_voice_clone_cap=10
            ),
        )

        fake_result = MagicMock()
        fake_result.scalar.return_value = 15  # exceeds cap
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        resp = await user_voice_api.internal_user_voice_quota(
            request=MagicMock(),
            user_id="00000000-0000-0000-0000-000000000002",
            db=fake_db,
        )

        import json
        body = json.loads(resp.body.decode("utf-8"))
        assert body["used"] == 15
        assert body["limit"] == 10
        assert body["remaining"] == 0, (
            f"remaining must clamp to 0 when used > limit; got "
            f"{body['remaining']!r}"
        )

    @pytest.mark.asyncio
    async def test_quota_rejects_invalid_user_id(self, monkeypatch):
        """Malformed UUID → 400. Prevents bad input from reaching the
        SQL query."""
        from unittest.mock import MagicMock

        import user_voice_api

        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )

        resp = await user_voice_api.internal_user_voice_quota(
            request=MagicMock(),
            user_id="not-a-uuid",
            db=MagicMock(),
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_quota_falls_back_to_default_on_admin_settings_load_error(
        self, monkeypatch,
    ):
        """Defensive: if load_settings raises (corrupt JSON / missing
        file), fall back to the AdminSettings default (30) instead of
        bubbling a 500. Smart UX should degrade gracefully — admin
        misconfiguration shouldn't block the whole pipeline."""
        from unittest.mock import AsyncMock, MagicMock

        import user_voice_api
        import admin_settings as _admin_settings_mod

        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )

        def _load_raises():
            raise RuntimeError("admin_settings.json corrupt")

        monkeypatch.setattr(
            _admin_settings_mod, "load_settings", _load_raises,
        )

        fake_result = MagicMock()
        fake_result.scalar.return_value = 3
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        resp = await user_voice_api.internal_user_voice_quota(
            request=MagicMock(),
            user_id="00000000-0000-0000-0000-000000000003",
            db=fake_db,
        )

        import json
        body = json.loads(resp.body.decode("utf-8"))
        assert resp.status_code == 200, (
            "load_settings exception should fall back to default, not "
            f"return 500. Got status {resp.status_code}, body={body!r}"
        )
        assert body["limit"] == 30, (
            f"Fallback default should be 30 (AdminSettings default); "
            f"got {body['limit']!r}"
        )
