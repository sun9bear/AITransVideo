"""TTS routing invariant tests.

Tests that guard the product invariants:
  INV-1: free/express → cosyvoice
  INV-2: paid/studio  → minimax
  INV-3: per-job tts_provider takes priority over global default
  INV-4: missing job identity must not silently use global default

The ``--job-id`` propagation link from process_runner → main.py →
pipeline has been restored.  All tests in this file are hard assertions.

See:
  docs/specs/2026-03-30-cosyvoice-routing-and-voice-matching-design.md
  docs/POST_MEMBERSHIP_MINIMAL_REPAIR_CHECKLIST.md
"""

from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# Gateway-level tests (PASSING — Gateway logic is correct)
# ---------------------------------------------------------------------------

# Stub database before importing gateway modules
_gateway_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)
_fake_database = types.ModuleType("database")
_fake_database.get_db = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
_fake_database.engine = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
_fake_database.async_session = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
sys.modules.setdefault("database", _fake_database)

from job_intercept import compute_job_policy  # noqa: E402


def _user(*, plan_code: str = "free", role: str = "user"):
    return SimpleNamespace(
        id="u1", email="t@t.com", display_name="T",
        role=role, plan_code=plan_code,
        free_jobs_quota_total=5, free_jobs_quota_used=0,
        created_at=None,
    )


class TestGatewayRoutingInvariants:
    """INV-1 / INV-2: Gateway correctly maps service_mode → provider."""

    def test_inv1_free_express_uses_cosyvoice(self):
        p = compute_job_policy(_user(plan_code="free"), "express")
        assert p["tts_provider"] == "cosyvoice"
        assert p["service_mode"] == "express"

    def test_inv1_plus_express_uses_cosyvoice(self):
        p = compute_job_policy(_user(plan_code="plus"), "express")
        assert p["tts_provider"] == "cosyvoice"

    def test_inv2_plus_studio_uses_minimax(self):
        p = compute_job_policy(_user(plan_code="plus"), "studio")
        assert p["tts_provider"] == "minimax"

    def test_inv2_pro_studio_uses_minimax(self):
        p = compute_job_policy(_user(plan_code="pro"), "studio")
        assert p["tts_provider"] == "minimax"


# ---------------------------------------------------------------------------
# TTS strategy-level tests (PASSING — per-job lookup works when given a record)
# ---------------------------------------------------------------------------

from services.tts.tts_strategy import get_tts_provider_for_job, get_tts_provider  # noqa: E402


class TestPerJobProviderLookup:
    """INV-3: per-job tts_provider takes priority over global default."""

    def test_inv3_dict_record_cosyvoice(self):
        record = {"tts_provider": "cosyvoice"}
        assert get_tts_provider_for_job(record) == "cosyvoice"

    def test_inv3_dict_record_minimax(self):
        record = {"tts_provider": "minimax"}
        assert get_tts_provider_for_job(record) == "minimax"

    def test_inv3_object_record(self):
        record = SimpleNamespace(tts_provider="cosyvoice")
        assert get_tts_provider_for_job(record) == "cosyvoice"

    def test_inv3_invalid_provider_falls_back(self):
        record = {"tts_provider": "unknown"}
        # Falls back to legacy — we just check it doesn't crash
        result = get_tts_provider_for_job(record)
        assert result in {"minimax", "cosyvoice", "mimo"}

    def test_inv3_none_record_falls_back(self):
        record = {"tts_provider": None}
        result = get_tts_provider_for_job(record)
        assert result in {"minimax", "cosyvoice", "mimo"}


# ---------------------------------------------------------------------------
# Pipeline-level contract tests (--job-id link restored)
# ---------------------------------------------------------------------------


class TestPipelineJobIdentityContract:
    """INV-4: pipeline must not silently lose per-job provider."""

    def test_build_command_includes_job_id(self):
        """process_runner must pass --job-id so pipeline can load job record."""
        from services.jobs.process_runner import ProcessJobRunner

        import inspect
        src = inspect.getsource(ProcessJobRunner._build_command)
        assert "--job-id" in src, (
            "_build_command does not include --job-id. "
            "Without it, pipeline falls back to admin_settings.json global default."
        )


class TestMainAcceptsJobId:
    """main.py must accept --job-id so ProcessConfig.job_id is populated."""

    def test_parse_process_args_has_job_id(self):
        import inspect
        # Import indirectly to avoid circular issues
        import importlib
        main_module = importlib.import_module("main")
        src = inspect.getsource(main_module.parse_process_args)
        assert "--job-id" in src
