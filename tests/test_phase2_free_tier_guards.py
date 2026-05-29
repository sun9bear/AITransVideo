"""Phase 2a free-tier guards (plan 2026-05-29 Task 0 + Task 1).

Task 0 (committed 536da7f): flag plumbing — enable_free_tier Settings field,
default False, AVT_-prefixed env binding.

Task 1 Step 1 (failing-test-first, per CodeX review): the service_mode="free"
fail-closed gate. With AVT_ENABLE_FREE_TIER off, a "free" submission must be
*rejected*, NOT silently coerced to express (the legacy job_intercept.py:1042
behavior). Tested via the pure _gate_service_mode helper so the gate is not
buried in the HTTP handler / frontend changes.
"""
import json
import sys
import types
from unittest.mock import MagicMock

from config import GatewaySettings


# ── Task 0: flag plumbing ────────────────────────────────────────────────

def test_enable_free_tier_defaults_false(monkeypatch):
    monkeypatch.delenv("AVT_ENABLE_FREE_TIER", raising=False)
    s = GatewaySettings(database_url="", pg_password="")
    assert s.enable_free_tier is False


def test_enable_free_tier_reads_avt_env(monkeypatch):
    monkeypatch.setenv("AVT_ENABLE_FREE_TIER", "true")
    s = GatewaySettings(database_url="", pg_password="")
    assert s.enable_free_tier is True


# ── Task 1 Step 1: service_mode="free" fail-closed gate ───────────────────

def _stub_database_module():
    """Stub `database` before importing job_intercept (mirrors
    test_smart_kill_switch.py); the pure gate helper needs no real DB."""
    if "database" not in sys.modules or not hasattr(
        sys.modules["database"], "_free_tier_stub"
    ):
        fake = types.ModuleType("database")
        fake.get_db = MagicMock()
        fake.engine = MagicMock()
        fake.async_session = MagicMock()
        fake._free_tier_stub = True
        sys.modules["database"] = fake


def _gate():
    _stub_database_module()
    from job_intercept import _gate_service_mode
    return _gate_service_mode


def test_free_rejected_when_flag_off():
    """flag off + free -> reject (403 free_disabled), NOT express downgrade."""
    mode, err = _gate()("free", free_enabled=False)
    assert mode == "free"  # recognized as free, NOT silently coerced to express
    assert err is not None
    assert err.status_code == 403
    assert json.loads(err.body)["error"] == "free_disabled"


def test_free_allowed_when_flag_on():
    mode, err = _gate()("free", free_enabled=True)
    assert mode == "free"
    assert err is None


def test_unknown_mode_still_coerces_to_express():
    """Non-free unknown modes keep the legacy express coercion."""
    mode, err = _gate()("bogus", free_enabled=False)
    assert mode == "express"
    assert err is None


def test_known_modes_pass_through():
    g = _gate()
    for m in ("express", "studio", "smart"):
        mode, err = g(m, free_enabled=False)
        assert mode == m
        assert err is None


# ── Task 1 Step 2: compute_job_policy free branch ─────────────────────────

def _compute_policy():
    _stub_database_module()
    from job_intercept import compute_job_policy
    return compute_job_policy


def test_compute_job_policy_free_branch():
    """flag-on free policy: MiMo voiceclone, non-interactive, no voice_id clone."""
    user = types.SimpleNamespace(role="user", plan_code="free")
    p = _compute_policy()(user, "free")
    assert p["service_mode"] == "free"
    assert p["tts_provider"] == "mimo"
    assert p["tts_model"] == "mimo-v2.5-tts-voiceclone"
    assert p["voice_strategy"] == "free_voiceclone"
    assert p["voice_clone_enabled"] is False
    assert p["requires_review"] is False
    assert p["quality_tier"] == "standard"
