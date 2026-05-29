"""Phase 2a free-tier guards (plan 2026-05-29 Task 0 + Task 1).

Task 0 (committed 536da7f): flag plumbing — enable_free_tier Settings field,
default False, AVT_-prefixed env binding.

Task 1 (committed df04b4a): the service_mode="free" fail-closed gate.
  - Pure _gate_service_mode helper: free recognized + rejected (403
    free_disabled) when AVT_ENABLE_FREE_TIER off — never a silent express
    downgrade. Unknown (non-free) modes keep the legacy express fallback
    (intentional, matches the smart-whitelist precedent).
  - Handler-level test (CodeX P3): the real boundary intercept_create_job
    rejects free+flag-off with 403 free_disabled and never reaches the
    upstream proxy_request.
  - compute_job_policy free branch (MiMo voiceclone; no voice_id clone).
    credits=0 is NOT in the policy dict — debit truth is DEBIT_RATES (Task 3).
"""
import asyncio
import json
import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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


# ── Task 1: import harness ───────────────────────────────────────────────

def _stub_database_module():
    """Stub `database` before importing job_intercept (mirrors
    test_smart_kill_switch.py); the gate fires before any real DB use."""
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


def _compute_policy():
    _stub_database_module()
    from job_intercept import compute_job_policy
    return compute_job_policy


# ── Task 1 Step 1(a): pure _gate_service_mode helper ─────────────────────

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
    """Non-free unknown modes keep the legacy express coercion (intentional —
    matches the smart-whitelist precedent; only free is fail-closed)."""
    mode, err = _gate()("bogus", free_enabled=False)
    assert mode == "express"
    assert err is None


def test_known_modes_pass_through():
    g = _gate()
    for m in ("express", "studio", "smart"):
        mode, err = g(m, free_enabled=False)
        assert mode == m
        assert err is None


# ── Task 1 Step 1(b): handler-level rejection (real security boundary) ────
# Mirrors tests/test_smart_kill_switch.py's intercept_create_job harness.

def _free_job_request(service_mode="free"):
    req = MagicMock()
    body = {
        "service_mode": service_mode,
        "source": {"type": "youtube_url", "value": "https://youtube.com/watch?v=x"},
    }
    req.body = AsyncMock(return_value=json.dumps(body, ensure_ascii=False).encode("utf-8"))
    req.headers = {"content-type": "application/json"}
    req.method = "POST"
    req.url = MagicMock()
    req.url.path = "/job-api/jobs"
    req.query_params = {}
    return req


def _min_db():
    db = MagicMock()
    result = MagicMock()
    result.scalar = MagicMock(return_value=0)
    result.all = MagicMock(return_value=[])
    db.execute = AsyncMock(return_value=result)
    return db


def _run_create(req, db, user):
    from job_intercept import intercept_create_job
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(intercept_create_job(req, db, user))
    finally:
        loop.close()


def test_handler_rejects_free_when_flag_off():
    """Real boundary: intercept_create_job with free + flag off → 403
    free_disabled, and the upstream proxy_request is never reached."""
    _stub_database_module()
    import job_intercept as ji
    user = types.SimpleNamespace(
        id=uuid.uuid4(), email="u@test.com", display_name="T",
        role="user", plan_code="free",
        free_jobs_quota_total=5, free_jobs_quota_used=0,
    )
    with patch.object(ji.settings, "enable_free_tier", False), \
         patch.object(ji, "proxy_request", AsyncMock()) as spy_proxy:
        resp = _run_create(_free_job_request("free"), _min_db(), user)
    body = json.loads(resp.body)
    assert resp.status_code == 403
    assert body["error"] == "free_disabled"
    spy_proxy.assert_not_called()


# ── Task 1 Step 2: compute_job_policy free branch ─────────────────────────

def test_compute_job_policy_free_branch():
    """flag-on free policy: MiMo voiceclone, non-interactive, no voice_id clone.

    credits=0 is intentionally NOT asserted: the policy dict carries no
    credits key for any mode — debit is DEBIT_RATES (free, standard)=0 (Task 3).
    """
    user = types.SimpleNamespace(role="user", plan_code="free")
    p = _compute_policy()(user, "free")
    assert p["service_mode"] == "free"
    assert p["tts_provider"] == "mimo"
    assert p["tts_model"] == "mimo-v2.5-tts-voiceclone"
    assert p["voice_strategy"] == "free_voiceclone"
    assert p["voice_clone_enabled"] is False
    assert p["requires_review"] is False
    assert p["quality_tier"] == "standard"
    assert "credits" not in p  # debit lives in DEBIT_RATES, not the policy dict
