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


class TestAdminSettingsSmartVoicePolicyPhase3:
    """Phase 3 (plan 2026-05-17-user-voice-candidate-first §后台策略字段):
    three independent admin toggles for Smart's voice candidate / clone
    policy. Defaults preserve existing Smart behavior (reuse + clone
    both allowed, no pause on possible match)."""

    def test_defaults_for_smart_voice_policy(self):
        """Plan §后台策略字段 defaults:
          - smart_auto_clone_enabled = True
          - smart_reuse_user_voice_enabled = True
          - smart_pause_on_possible_user_voice_match = False

        Default values matter because Phase 3 is rolled out to existing
        Smart users. Drifting any default would silently change behavior
        on the first restart after a Phase 3 deploy."""
        from admin_settings import AdminSettings

        s = AdminSettings()
        assert hasattr(s, "smart_auto_clone_enabled"), (
            "AdminSettings missing smart_auto_clone_enabled — Phase 3 "
            "gate field. process.py reads this; absence falls back to "
            "True via getattr but masks deploy drift."
        )
        assert hasattr(s, "smart_reuse_user_voice_enabled"), (
            "AdminSettings missing smart_reuse_user_voice_enabled."
        )
        assert hasattr(s, "smart_pause_on_possible_user_voice_match"), (
            "AdminSettings missing smart_pause_on_possible_user_voice_match."
        )
        assert s.smart_auto_clone_enabled is True
        assert s.smart_reuse_user_voice_enabled is True
        assert s.smart_pause_on_possible_user_voice_match is False

    def test_fields_accept_admin_override(self):
        """All three toggles accept explicit overrides via constructor
        (mirrors the POST /api/admin/settings payload)."""
        from admin_settings import AdminSettings

        s = AdminSettings(
            smart_auto_clone_enabled=False,
            smart_reuse_user_voice_enabled=False,
            smart_pause_on_possible_user_voice_match=True,
        )
        assert s.smart_auto_clone_enabled is False
        assert s.smart_reuse_user_voice_enabled is False
        assert s.smart_pause_on_possible_user_voice_match is True

    def test_save_load_round_trip_preserves_smart_voice_policy(
        self, tmp_path, monkeypatch,
    ):
        """save_settings → load_settings round-trips all three fields
        verbatim. Catches schema drift between the save merge logic
        and the parse path."""
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        import importlib
        import admin_settings as gw_admin_settings
        importlib.reload(gw_admin_settings)
        AdminSettings = gw_admin_settings.AdminSettings
        save_settings = gw_admin_settings.save_settings
        load_settings = gw_admin_settings.load_settings

        s = AdminSettings(
            smart_auto_clone_enabled=False,
            smart_reuse_user_voice_enabled=False,
            smart_pause_on_possible_user_voice_match=True,
        )
        save_settings(s)

        loaded = load_settings()
        assert loaded.smart_auto_clone_enabled is False
        assert loaded.smart_reuse_user_voice_enabled is False
        assert loaded.smart_pause_on_possible_user_voice_match is True

    def test_save_smart_voice_policy_preserves_existing_smart_cap(
        self, tmp_path, monkeypatch,
    ):
        """Phase 3 save must NOT clobber the existing
        ``smart_user_voice_clone_cap`` field — the save merge logic
        already preserves unrelated keys, and this verifies the new
        fields don't accidentally overwrite siblings."""
        import json
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        import importlib
        import admin_settings as gw_admin_settings
        importlib.reload(gw_admin_settings)
        AdminSettings = gw_admin_settings.AdminSettings
        save_settings = gw_admin_settings.save_settings
        load_settings = gw_admin_settings.load_settings

        # Step 1: save with a custom cap.
        s = AdminSettings(smart_user_voice_clone_cap=50)
        save_settings(s)
        first_loaded = load_settings()
        assert first_loaded.smart_user_voice_clone_cap == 50

        # Step 2: save Phase 3 fields; the cap must persist.
        s2 = AdminSettings(
            smart_user_voice_clone_cap=50,
            smart_auto_clone_enabled=False,
            smart_reuse_user_voice_enabled=False,
        )
        save_settings(s2)
        loaded = load_settings()
        assert loaded.smart_user_voice_clone_cap == 50, (
            "Phase 3 save clobbered smart_user_voice_clone_cap."
        )
        assert loaded.smart_auto_clone_enabled is False
        assert loaded.smart_reuse_user_voice_enabled is False
        # Defaults still hold for the un-touched field.
        assert loaded.smart_pause_on_possible_user_voice_match is False

    def test_get_settings_response_includes_phase3_fields(self):
        """GET /api/admin/settings returns model_dump(), so any new
        AdminSettings field is auto-exposed. Verify the three Phase 3
        fields are in the dump (and have correct default values)."""
        from admin_settings import AdminSettings

        dump = AdminSettings().model_dump()
        assert "smart_auto_clone_enabled" in dump
        assert "smart_reuse_user_voice_enabled" in dump
        assert "smart_pause_on_possible_user_voice_match" in dump
        # Default shape matches plan §后台策略字段.
        assert dump["smart_auto_clone_enabled"] is True
        assert dump["smart_reuse_user_voice_enabled"] is True
        assert dump["smart_pause_on_possible_user_voice_match"] is False

    def test_post_settings_with_missing_phase3_fields_resets_them_to_defaults(
        self, tmp_path, monkeypatch,
    ):
        """CONTRACT-LOCK TEST — pins current full-body POST semantics.

        Phase 3 follow-up P2 (Codex review of commit 4073886): this test
        documents that the current POST /api/admin/settings endpoint
        treats the request body as a complete ``AdminSettings`` Pydantic
        model. Any field absent from the request body is populated with
        the Pydantic default, and ``save_settings`` then merges all
        fields (defaults included) into admin_settings.json. As a
        result, a stale admin form / API caller that doesn't know about
        Phase 3 fields will silently RESET those fields to defaults.

        This is NOT aspirational behavior — it's the current contract.
        The test exists so a future PR that adds PATCH semantics or an
        admin UI sync MUST consciously break this test (and choose to
        update the contract or migrate callers). Until then, admin
        operators must send the full AdminSettings shape including the
        Phase 3 fields.

        See plan ``docs/plans/2026-05-17-user-voice-candidate-first-plan.md``
        §Phase 3 finding P2 for the rationale.
        """
        monkeypatch.setenv("AIVIDEOTRANS_CONFIG_DIR", str(tmp_path))
        import importlib
        import admin_settings as gw_admin_settings
        importlib.reload(gw_admin_settings)
        AdminSettings = gw_admin_settings.AdminSettings
        save_settings = gw_admin_settings.save_settings
        load_settings = gw_admin_settings.load_settings

        # Step 1: seed the file with NON-default Phase 3 values so we
        # can observe whether they survive a partial save.
        s_initial = AdminSettings(
            smart_auto_clone_enabled=False,
            smart_reuse_user_voice_enabled=False,
            smart_pause_on_possible_user_voice_match=True,
        )
        save_settings(s_initial)
        seeded = load_settings()
        assert seeded.smart_auto_clone_enabled is False
        assert seeded.smart_reuse_user_voice_enabled is False
        assert seeded.smart_pause_on_possible_user_voice_match is True

        # Step 2: simulate a stale admin POST that only knows about
        # whisper_alignment_enabled — it omits the Phase 3 fields.
        # The endpoint constructs the full AdminSettings from the body
        # (Pydantic fills missing fields with their defaults), then
        # save_settings merges the dump into admin_settings.json.
        stale_body = {"whisper_alignment_enabled": True}
        body_model = AdminSettings(**stale_body)
        save_settings(body_model)

        # Step 3: reload and assert the Phase 3 fields were silently
        # reset to their Pydantic defaults. If this assertion ever
        # changes, the contract changed — confirm intentional.
        loaded = load_settings()
        assert loaded.smart_auto_clone_enabled is True, (
            "Contract change detected: smart_auto_clone_enabled "
            "survived a partial POST. If you intended PATCH semantics, "
            "update plan §Phase 3 finding P2 and rewrite this test."
        )
        assert loaded.smart_reuse_user_voice_enabled is True, (
            "Contract change detected: smart_reuse_user_voice_enabled "
            "survived a partial POST. Update plan + this test."
        )
        assert loaded.smart_pause_on_possible_user_voice_match is False, (
            "Contract change detected: "
            "smart_pause_on_possible_user_voice_match survived a "
            "partial POST. Update plan + this test."
        )
        # And the field that WAS in the partial body went through.
        assert loaded.whisper_alignment_enabled is True


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
    async def test_register_smart_endpoint_registered_on_internal_router(self):
        """PR#3C-b3e-fix (Codex 第二十九轮 P0): the register-smart endpoint
        must be wired on internal_router so the Caddyfile @internal_block
        properly shields it from public ingress."""
        import user_voice_api

        routes = [
            (r.path, r.methods)
            for r in user_voice_api.internal_router.routes
        ]
        register_routes = [
            (path, methods)
            for path, methods in routes
            if path.endswith("/user-voices/register-smart")
        ]
        assert register_routes, (
            "POST /api/internal/user-voices/register-smart not "
            "registered on internal_router. Codex 第二十九轮 P0 mirror "
            "channel — without it, Smart auto-clone quota signal "
            "goes stale across jobs.\n"
            f"Available internal routes: {routes}"
        )
        path, methods = register_routes[0]
        assert path == "/api/internal/user-voices/register-smart"
        assert "POST" in methods

    @pytest.mark.asyncio
    async def test_match_endpoint_registered_on_internal_router(self):
        import user_voice_api

        routes = [
            (r.path, r.methods)
            for r in user_voice_api.internal_router.routes
        ]
        match_routes = [
            (path, methods)
            for path, methods in routes
            if path.endswith("/user-voices/match")
        ]
        assert match_routes, (
            "POST /api/internal/user-voices/match not registered on "
            "internal_router. Phase 2 matching must stay internal-only."
        )
        path, methods = match_routes[0]
        assert path == "/api/internal/user-voices/match"
        assert "POST" in methods

    @pytest.mark.asyncio
    async def test_match_endpoint_returns_strong_candidate(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        import user_voice_api
        from user_voice_service import UserVoiceMatch

        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )
        monkeypatch.setattr(
            user_voice_api,
            "_voice_to_dict",
            lambda v: {"voice_id": v.voice_id, "label": v.label},
        )

        recorded = {}

        async def _fake_match(db, **kwargs):
            recorded.update(kwargs)
            return [
                UserVoiceMatch(
                    voice=SimpleNamespace(voice_id="vt_match", label="Speaker A · 2026-05-16 14:32"),
                    confidence="strong",
                    reason="same_source_content_hash_and_speaker_id",
                    score=100,
                )
            ]

        monkeypatch.setattr(user_voice_api, "match_user_voices", _fake_match)
        valid_uuid = "00000000-0000-0000-0000-000000000001"
        body = {
            "user_id": valid_uuid,
            "source_content_hash": "youtube:abc",
            "speaker_id": "speaker_a",
            "speaker_name": "Speaker A",
            "provider": "minimax_voice_clone",
            "tts_provider": "minimax_tts",
            "platform": "minimax_domestic",
        }
        fake_req = MagicMock()
        fake_req.body = AsyncMock(return_value=__import__("json").dumps(body).encode())

        resp = await user_voice_api.internal_match_user_voice(
            request=fake_req,
            db=MagicMock(),
        )

        import json
        body_out = json.loads(resp.body.decode("utf-8"))
        assert body_out["matched"] is True
        assert body_out["confidence"] == "strong"
        assert body_out["auto_reuse_allowed"] is True
        assert body_out["voice"]["voice_id"] == "vt_match"
        assert body_out["candidates"][0]["voice"]["voice_id"] == "vt_match"
        assert recorded["user_id"].hex == "00000000000000000000000000000001"
        assert recorded["source_content_hash"] == "youtube:abc"
        assert recorded["source_speaker_id"] == "speaker_a"
        assert recorded["source_speaker_name"] == "Speaker A"
        assert recorded["provider"] == "minimax_voice_clone"
        assert recorded["tts_provider"] == "minimax_tts"
        assert recorded["platform"] == "minimax_domestic"

    @pytest.mark.asyncio
    async def test_match_endpoint_missing_hash_returns_no_candidate(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        import user_voice_api

        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )
        matcher = AsyncMock(return_value=[])
        monkeypatch.setattr(user_voice_api, "match_user_voices", matcher)
        fake_req = MagicMock()
        fake_req.body = AsyncMock(return_value=b'{"user_id": "00000000-0000-0000-0000-000000000001"}')

        resp = await user_voice_api.internal_match_user_voice(
            request=fake_req,
            db=MagicMock(),
        )

        import json
        body_out = json.loads(resp.body.decode("utf-8"))
        assert body_out["matched"] is False
        assert body_out["reason"] == "missing_source_content_hash"
        matcher.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_register_smart_endpoint_writes_to_user_voices(
        self, monkeypatch,
    ):
        """End-to-end mirror: register-smart endpoint should call
        ``add_user_voice`` with the same field shape as the Studio
        manual-clone path (voice_selection_api.py:503)."""
        from unittest.mock import AsyncMock, MagicMock

        import user_voice_api

        # Mock auth
        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )

        # Track calls to add_user_voice
        recorded_calls = []
        fake_voice = MagicMock()
        fake_voice.voice_id = "vt_xxx"
        fake_voice.user_id = "00000000-0000-0000-0000-000000000001"

        async def _fake_add(db, **kwargs):
            recorded_calls.append(kwargs)
            return fake_voice

        monkeypatch.setattr(user_voice_api, "add_user_voice", _fake_add)

        # Build request with JSON body
        valid_uuid = "00000000-0000-0000-0000-000000000001"
        body = {
            "user_id": valid_uuid,
            "voice_id": "vt_xxx",
            "label": "Speaker A · 2026-05-16 14:32",
            "source_speaker_id": "speaker_a",
            "source_job_id": "job-1",
            "source_type": "youtube_url",
            "source_ref": "https://youtu.be/abc",
            "source_content_hash": "youtube:abc",
            "source_video_title": "Source Title",
            "source_published_at": "2024-05-01T00:00:00+00:00",
            "source_content_summary": "频道：Test Channel",
            "source_content_era": "2024",
            "source_content_tags": {"channel": "Test Channel", "tags": ["AI"]},
            "source_speaker_name": "Speaker A",
            "clone_sample_seconds": 12.5,
            "clone_sample_segment_ids": [1, 2],
            "created_from": "smart_auto",
            "notes": "Smart auto-clone from job j-1",
        }

        fake_req = MagicMock()
        fake_req.body = AsyncMock(return_value=__import__("json").dumps(body).encode())

        resp = await user_voice_api.internal_register_smart_clone(
            request=fake_req,
            db=MagicMock(),
        )

        import json
        body_out = json.loads(resp.body.decode("utf-8"))
        assert body_out["ok"] is True
        assert body_out["voice_id"] == "vt_xxx"

        # add_user_voice was called with the right field shape.
        assert len(recorded_calls) == 1
        call = recorded_calls[0]
        # Field defaults must match Studio manual-clone path:
        assert call["provider"] == "minimax_voice_clone"
        assert call["tts_provider"] == "minimax_tts"
        assert call["platform"] == "minimax_domestic"
        assert call["label"] == "Speaker A · 2026-05-16 14:32"
        assert call["source_speaker_id"] == "speaker_a"
        assert call["source_job_id"] == "job-1"
        assert call["source_type"] == "youtube_url"
        assert call["source_ref"] == "https://youtu.be/abc"
        assert call["source_content_hash"] == "youtube:abc"
        assert call["source_video_title"] == "Source Title"
        assert call["source_published_at"].isoformat() == "2024-05-01T00:00:00+00:00"
        assert call["source_content_summary"] == "频道：Test Channel"
        assert call["source_content_era"] == "2024"
        assert call["source_content_tags"] == {"channel": "Test Channel", "tags": ["AI"]}
        assert call["source_speaker_name"] == "Speaker A"
        assert call["clone_sample_seconds"] == 12.5
        assert call["clone_sample_segment_ids"] == [1, 2]
        assert call["created_from"] == "smart_auto"
        assert call["notes"] == "Smart auto-clone from job j-1"
        assert call["voice_id"] == "vt_xxx"

    @pytest.mark.asyncio
    async def test_register_smart_endpoint_rejects_missing_fields(
        self, monkeypatch,
    ):
        """Empty user_id or voice_id → 400 BEFORE touching add_user_voice."""
        from unittest.mock import AsyncMock, MagicMock

        import user_voice_api

        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )

        # Missing voice_id
        fake_req = MagicMock()
        fake_req.body = AsyncMock(return_value=b'{"user_id": "00000000-0000-0000-0000-000000000001"}')
        resp = await user_voice_api.internal_register_smart_clone(
            request=fake_req,
            db=MagicMock(),
        )
        assert resp.status_code == 400

        # Missing user_id
        fake_req.body = AsyncMock(return_value=b'{"voice_id": "vt_x"}')
        resp = await user_voice_api.internal_register_smart_clone(
            request=fake_req,
            db=MagicMock(),
        )
        assert resp.status_code == 400

        # Invalid UUID
        fake_req.body = AsyncMock(return_value=b'{"user_id": "not-a-uuid", "voice_id": "vt_x"}')
        resp = await user_voice_api.internal_register_smart_clone(
            request=fake_req,
            db=MagicMock(),
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_register_smart_endpoint_returns_500_on_db_failure(
        self, monkeypatch,
    ):
        """add_user_voice exception → 500 so the app helper returns
        False → process.py escalates to handoff."""
        from unittest.mock import AsyncMock, MagicMock

        import user_voice_api

        monkeypatch.setattr(
            user_voice_api, "_internal_access_error",
            lambda req: None,
        )

        async def _failing_add(*args, **kwargs):
            raise RuntimeError("DB connection lost")

        monkeypatch.setattr(user_voice_api, "add_user_voice", _failing_add)

        fake_req = MagicMock()
        fake_req.body = AsyncMock(return_value=__import__("json").dumps({
            "user_id": "00000000-0000-0000-0000-000000000001",
            "voice_id": "vt_xxx",
            "label": "test",
        }).encode())

        resp = await user_voice_api.internal_register_smart_clone(
            request=fake_req,
            db=MagicMock(),
        )
        assert resp.status_code == 500
        import json
        body = json.loads(resp.body.decode("utf-8"))
        assert body["error"] == "register_failed"

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
