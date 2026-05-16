"""Tests for Phase 2: Gateway job creation validation, structured errors, policy computation.

Imports real gateway modules (with stubbed database layer).
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Stub database before importing gateway modules
_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)

_fake_database = types.ModuleType("database")
_fake_database.get_db = MagicMock()
_fake_database.engine = MagicMock()
_fake_database.async_session = MagicMock()
sys.modules.setdefault("database", _fake_database)

from job_intercept import PLAN_CATALOG, compute_job_policy, _error_response  # noqa: E402


def _make_user(*, role="user", plan_code="free", email="u@test.com"):
    return SimpleNamespace(
        id="uid-1", email=email, display_name="Test",
        role=role, plan_code=plan_code,
        free_jobs_quota_total=5, free_jobs_quota_used=0,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ===================================================================
# compute_job_policy
# ===================================================================

class TestComputeJobPolicy:
    def test_express_default_cosyvoice(self):
        """Default express provider from admin settings is cosyvoice."""
        p = compute_job_policy(_make_user(plan_code="free"), "express")
        assert p["service_mode"] == "express"
        assert p["tts_provider"] == "cosyvoice"
        assert p["tts_model"] == "cosyvoice-v3-flash"
        assert p["requires_review"] is False
        assert p["voice_clone_enabled"] is False
        assert p["voice_strategy"] == "preset_mapping"
        assert p["plan_code_snapshot"] == "free"
        assert p["role_snapshot"] == "user"

    def test_studio_default_minimax(self):
        p = compute_job_policy(_make_user(plan_code="plus"), "studio")
        assert p["service_mode"] == "studio"
        assert p["tts_provider"] == "minimax"
        assert p["tts_model"] == "speech-2.8-turbo"
        assert p["requires_review"] is True
        assert p["voice_clone_enabled"] is True

    def test_studio_pro_user(self):
        p = compute_job_policy(_make_user(plan_code="pro"), "studio")
        assert p["tts_model"] == "speech-2.8-hd"

    def test_studio_admin(self):
        p = compute_job_policy(_make_user(role="admin", plan_code="free"), "studio")
        assert p["tts_model"] == "speech-2.8-hd"
        assert p["role_snapshot"] == "admin"

    def test_quality_tier_is_standard_for_all_modes(self):
        """V3-6: quality_tier is always 'standard' in current policy (single truth source)."""
        for mode in ("express", "studio"):
            p = compute_job_policy(_make_user(plan_code="plus"), mode)
            assert p["quality_tier"] == "standard", f"quality_tier for {mode} should be 'standard'"

    def test_quality_tier_present_in_policy(self):
        """V3-6: quality_tier must be present in policy output."""
        p = compute_job_policy(_make_user(plan_code="free"), "express")
        assert "quality_tier" in p

    # --- Admin settings driven provider selection ---

    def test_express_volcengine_from_settings(self, monkeypatch):
        """When admin sets express_tts_provider=volcengine, policy uses it."""
        import admin_settings as admin_mod
        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.express_tts_provider = "volcengine"
            return s

        monkeypatch.setattr(admin_mod, "load_settings", mock_load)
        p = compute_job_policy(_make_user(plan_code="free"), "express")
        assert p["tts_provider"] == "volcengine"

    def test_studio_volcengine_from_settings(self, monkeypatch):
        """When admin sets studio_tts_provider=volcengine, policy uses it."""
        import admin_settings as admin_mod
        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.studio_tts_provider = "volcengine"
            return s

        monkeypatch.setattr(admin_mod, "load_settings", mock_load)
        p = compute_job_policy(_make_user(plan_code="plus"), "studio")
        assert p["tts_provider"] == "volcengine"

    def test_express_invalid_provider_falls_back_to_cosyvoice(self, monkeypatch):
        """Invalid express provider value falls back to cosyvoice."""
        import admin_settings as admin_mod
        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.express_tts_provider = "nonexistent_provider"
            return s

        monkeypatch.setattr(admin_mod, "load_settings", mock_load)
        p = compute_job_policy(_make_user(plan_code="free"), "express")
        assert p["tts_provider"] == "cosyvoice"

    def test_studio_invalid_provider_falls_back_to_minimax(self, monkeypatch):
        """Invalid studio provider value falls back to minimax."""
        import admin_settings as admin_mod
        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.studio_tts_provider = "nonexistent_provider"
            return s

        monkeypatch.setattr(admin_mod, "load_settings", mock_load)
        p = compute_job_policy(_make_user(plan_code="plus"), "studio")
        assert p["tts_provider"] == "minimax"

    # --- B2: volcengine dual-mode tts_model / voice_clone_enabled ---

    def test_express_volcengine_model_seed_tts_1_1(self, monkeypatch):
        """express + volcengine → tts_model = 'seed-tts-1.1' (req_params.model for 1.0)."""
        import admin_settings as admin_mod
        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.express_tts_provider = "volcengine"
            return s

        monkeypatch.setattr(admin_mod, "load_settings", mock_load)
        p = compute_job_policy(_make_user(plan_code="free"), "express")
        assert p["tts_provider"] == "volcengine"
        assert p["tts_model"] == "seed-tts-1.1"
        assert p["voice_clone_enabled"] is False

    def test_studio_volcengine_model_none(self, monkeypatch):
        """studio + volcengine → tts_model is None (2.0 public voices don't need model)."""
        import admin_settings as admin_mod
        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.studio_tts_provider = "volcengine"
            return s

        monkeypatch.setattr(admin_mod, "load_settings", mock_load)
        p = compute_job_policy(_make_user(plan_code="plus"), "studio")
        assert p["tts_provider"] == "volcengine"
        assert p["tts_model"] is None

    def test_studio_volcengine_clone_disabled(self, monkeypatch):
        """studio + volcengine → voice_clone_enabled is False."""
        import admin_settings as admin_mod
        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.studio_tts_provider = "volcengine"
            return s

        monkeypatch.setattr(admin_mod, "load_settings", mock_load)
        p = compute_job_policy(_make_user(plan_code="plus"), "studio")
        assert p["voice_clone_enabled"] is False

    def test_non_volcengine_studio_unchanged(self, monkeypatch):
        """studio + minimax → tts_model and voice_clone_enabled unchanged from before."""
        import admin_settings as admin_mod
        original_load = admin_mod.load_settings

        def mock_load():
            s = original_load()
            s.studio_tts_provider = "minimax"
            return s

        monkeypatch.setattr(admin_mod, "load_settings", mock_load)
        p = compute_job_policy(_make_user(plan_code="plus"), "studio")
        assert p["tts_model"] == "speech-2.8-turbo"
        assert p["voice_clone_enabled"] is True

    def test_non_volcengine_express_unchanged(self):
        """express + cosyvoice (default) → tts_model unchanged."""
        p = compute_job_policy(_make_user(plan_code="free"), "express")
        assert p["tts_model"] == "cosyvoice-v3-flash"
        assert p["voice_clone_enabled"] is False


# ===================================================================
# Structured error responses
# ===================================================================

class TestStructuredErrors:
    def test_error_response_format(self):
        resp = _error_response(403, "service_mode_not_allowed", "msg", {"key": "val"})
        body = json.loads(resp.body)
        assert resp.status_code == 403
        assert body["error"] == "service_mode_not_allowed"
        assert body["message"] == "msg"
        assert body["detail"]["key"] == "val"

    def test_error_response_without_detail(self):
        resp = _error_response(409, "concurrent_limit", "too many")
        body = json.loads(resp.body)
        assert "detail" not in body
        assert body["error"] == "concurrent_limit"


# ===================================================================
# PLAN_CATALOG consistency
# ===================================================================

class TestPlanCatalog:
    def test_free_plan_only_allows_express(self):
        assert PLAN_CATALOG["free"]["allowed_service_modes"] == ["express"]
        assert PLAN_CATALOG["free"]["max_duration_minutes"] == 10
        assert PLAN_CATALOG["free"]["max_concurrent_jobs"] == 1

    def test_plus_plan_allows_studio(self):
        assert "studio" in PLAN_CATALOG["plus"]["allowed_service_modes"]
        assert PLAN_CATALOG["plus"]["max_duration_minutes"] == 45

    def test_pro_plan_limits(self):
        assert PLAN_CATALOG["pro"]["max_duration_minutes"] == 180
        assert PLAN_CATALOG["pro"]["max_concurrent_jobs"] == 5


# ===================================================================
# Smart MVP P2 launch — compute_job_policy("smart")
# ===================================================================
# Discovery (2026-05-16): smart submissions through real UI silently
# fell into the ``else`` (express) branch — got tts_provider=cosyvoice
# (admin default), tts_model=cosyvoice-v3-flash, voice_clone_enabled=False,
# requires_review=False. User paid 100 credits/min (smart price) but
# received the express experience. Master plan §5.0 explicitly locks
# smart to MiniMax + auto-clone. These tests pin the new smart branch.


class TestComputeJobPolicySmart:

    def test_smart_locks_minimax_provider_regardless_of_admin_settings(
        self, monkeypatch,
    ):
        """Smart's TTS provider MUST be MiniMax. admin can't override
        — master plan §5.0 says smart auto-decision relies on MiniMax's
        clone API + quota model. Any other provider breaks that
        contract."""
        import admin_settings as admin_mod
        original = admin_mod.load_settings

        def fake_load():
            s = SimpleNamespace()
            s.studio_tts_provider = "minimax"
            # Admin set express to cosyvoice; smart MUST NOT inherit
            s.express_tts_provider = "cosyvoice"
            return s

        monkeypatch.setattr(admin_mod, "load_settings", fake_load)
        try:
            p = compute_job_policy(_make_user(plan_code="plus"), "smart")
            assert p["service_mode"] == "smart"
            assert p["tts_provider"] == "minimax", (
                "smart MUST lock to MiniMax — admin's express setting "
                "(cosyvoice) MUST NOT bleed through"
            )
        finally:
            monkeypatch.setattr(admin_mod, "load_settings", original)

    def test_smart_uses_speech_2_8_hd_model(self):
        """Master plan §15 P2: smart selects MiniMax 高质量 TTS
        (speech-2.8-hd), not the cheaper turbo model used by Plus
        Studio."""
        p = compute_job_policy(_make_user(plan_code="plus"), "smart")
        assert p["tts_model"] == "speech-2.8-hd"

    def test_smart_enables_voice_clone(self):
        """``voice_clone_enabled`` MUST be True for smart — smart's
        whole value proposition is auto-clone of main speakers
        (gated by smart_consent.auto_voice_clone at runtime)."""
        p = compute_job_policy(_make_user(plan_code="plus"), "smart")
        assert p["voice_clone_enabled"] is True

    def test_smart_requires_review_flag_true(self):
        """``requires_review=True`` so the review_state_manager and
        gate-based code paths see smart as a "review" job; smart
        inline branch in process.py auto-approves the review payloads
        without user interaction."""
        p = compute_job_policy(_make_user(plan_code="plus"), "smart")
        assert p["requires_review"] is True

    def test_smart_voice_strategy_distinct_from_express_studio(self):
        """Distinct strategy string for audit clarity. Studio uses
        ``user_selected``, express uses ``preset_mapping``. Smart
        gets its own value so downstream code / audit can branch."""
        p = compute_job_policy(_make_user(plan_code="plus"), "smart")
        assert p["voice_strategy"] not in ("user_selected", "preset_mapping")
        # Document expected literal — adjust if implementation picks
        # a different name (test serves as the source of truth).
        assert "smart" in p["voice_strategy"].lower()

    def test_smart_quality_tier_standard(self):
        """Master plan §2.1: smart has a single product tier; internal
        ``quality_tier`` stays "standard" for compat with the 2D
        pricing table. NOT exposed to user."""
        p = compute_job_policy(_make_user(plan_code="plus"), "smart")
        assert p["quality_tier"] == "standard"

    def test_smart_admin_user_also_gets_minimax_2_8_hd(self):
        """Admin users get the same smart policy — no per-role
        downgrade. (Studio's per-plan tts_model picks turbo for non-pro;
        smart uses HD for all to match the smart product spec.)"""
        p = compute_job_policy(_make_user(role="admin", plan_code="free"), "smart")
        assert p["tts_provider"] == "minimax"
        assert p["tts_model"] == "speech-2.8-hd"

    def test_smart_pro_user_also_gets_speech_2_8_hd(self):
        """Pro users see the same smart policy as Plus — fixed
        product spec, not plan-tiered."""
        p = compute_job_policy(_make_user(plan_code="pro"), "smart")
        assert p["tts_model"] == "speech-2.8-hd"

    def test_smart_snapshot_fields_present(self):
        p = compute_job_policy(_make_user(plan_code="plus"), "smart")
        assert p["plan_code_snapshot"] == "plus"
        assert p["role_snapshot"] == "user"
