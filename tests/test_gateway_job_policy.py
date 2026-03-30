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
    def test_express_free_user(self):
        p = compute_job_policy(_make_user(plan_code="free"), "express")
        assert p["service_mode"] == "express"
        assert p["tts_provider"] == "cosyvoice"
        assert p["tts_model"] == "cosyvoice-v3-flash"
        assert p["requires_review"] is False
        assert p["voice_clone_enabled"] is False
        assert p["voice_strategy"] == "preset_mapping"
        assert p["plan_code_snapshot"] == "free"
        assert p["role_snapshot"] == "user"

    def test_studio_plus_user(self):
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
        assert PLAN_CATALOG["plus"]["max_duration_minutes"] == 60

    def test_pro_plan_limits(self):
        assert PLAN_CATALOG["pro"]["max_duration_minutes"] == 180
        assert PLAN_CATALOG["pro"]["max_concurrent_jobs"] == 10
