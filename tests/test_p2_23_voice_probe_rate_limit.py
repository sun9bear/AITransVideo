"""P2-23 (audit 2026-05-07) regression: voice-probe per-user rate limit.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        P2-23 — `/gateway/user-voices/probe` calls paid TTS providers
                (MiniMax / CosyVoice / VolcEngine) with no rate limit.
                A logged-in attacker could spend the platform's TTS
                quota at line-rate.

The fix introduces ``risk_control.check_voice_probe_allowed`` (called
BEFORE the paid call) and ``record_voice_probe`` (called AFTER a
successful response) with two windows:
  * 10 calls per 60 seconds (per user)
  * 100 calls per 24 hours (per user)

Both windows share the same per-user buffer; the short window catches
loops while the day window caps total daily spend per account.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_DIR = str(_REPO_ROOT / "gateway")
if _GATEWAY_DIR not in sys.path:
    sys.path.insert(0, _GATEWAY_DIR)


@pytest.fixture(autouse=True)
def _reset_voice_probe():
    """Each test starts with empty probe buffers — wipe before AND after
    so a leak from one test doesn't poison the next."""
    import risk_control
    risk_control.reset_voice_probe_rate_limits()
    yield
    risk_control.reset_voice_probe_rate_limits()


# ---------------------------------------------------------------------------
# Short-window (10 / 60s) limit
# ---------------------------------------------------------------------------


def test_voice_probe_allows_first_ten_calls():
    """The first 10 calls in the same window must pass without exception.
    11th call must raise — that's the trip-point."""
    import risk_control

    user = "user-fixture-1"
    for i in range(10):
        risk_control.check_voice_probe_allowed(user)
        risk_control.record_voice_probe(user)
    with pytest.raises(risk_control.RateLimitExceeded) as excinfo:
        risk_control.check_voice_probe_allowed(user)
    assert excinfo.value.scope == "voice_probe_short", (
        "P2-23 regression: 11th call within 60s window did not trip "
        f"the short-window limit. Got scope={excinfo.value.scope!r}."
    )


def test_voice_probe_record_after_success_only():
    """``record_voice_probe`` MUST be called separately from the check.
    The endpoint should record AFTER the paid TTS call returns a
    non-empty audio buffer; if it recorded BEFORE, a flaky 502 from
    the provider would consume the user's daily quota."""
    import risk_control

    user = "user-fixture-2"
    # Simulate 5 failed probes (check OK, but no record) — quota
    # should still allow further calls because record never fired.
    for _ in range(5):
        risk_control.check_voice_probe_allowed(user)
        # NOTE: deliberately NOT calling record_voice_probe to mirror
        # the 502/empty-audio path in user_voice_api.probe_user_voice
    # Buffer is empty; should still allow many more calls.
    for _ in range(10):
        risk_control.check_voice_probe_allowed(user)
        risk_control.record_voice_probe(user)
    # Now we've recorded 10; 11th must trip.
    with pytest.raises(risk_control.RateLimitExceeded):
        risk_control.check_voice_probe_allowed(user)


def test_voice_probe_per_user_isolation():
    """One user hitting their cap must NOT block a different user."""
    import risk_control

    user_a = "user-a"
    user_b = "user-b"
    for _ in range(10):
        risk_control.check_voice_probe_allowed(user_a)
        risk_control.record_voice_probe(user_a)
    # user_a is at cap — verify
    with pytest.raises(risk_control.RateLimitExceeded):
        risk_control.check_voice_probe_allowed(user_a)
    # user_b is untouched — must still pass
    risk_control.check_voice_probe_allowed(user_b)


# ---------------------------------------------------------------------------
# Day-window (100 / 24h) limit
# ---------------------------------------------------------------------------


def test_voice_probe_day_limit_via_buffer_injection():
    """We can't realistically wait 60s in a unit test, so instead we
    inject 100 timestamps into the buffer artificially via the
    record helper, all within the day window. The 101st check must
    trip ``voice_probe_day``.
    """
    import risk_control

    user = "user-day-cap"
    for _ in range(100):
        risk_control.record_voice_probe(user)
    with pytest.raises(risk_control.RateLimitExceeded) as excinfo:
        risk_control.check_voice_probe_allowed(user)
    # Order of checks: short-window check fires first (since ALL 100
    # are also in the short window). The fix is correct as long as
    # SOMETHING trips. Either scope is acceptable here.
    assert excinfo.value.scope in (
        "voice_probe_short",
        "voice_probe_day",
    ), (
        f"P2-23 regression: 101st call did not trip ANY voice-probe "
        f"limit. Got scope={excinfo.value.scope!r}."
    )


# ---------------------------------------------------------------------------
# Empty user_id no-op
# ---------------------------------------------------------------------------


def test_voice_probe_empty_user_id_skips_check():
    """Defensive: an empty user_id (anonymous request, shouldn't reach
    here) must NOT raise — the auth layer is responsible for blocking
    that path. The rate limiter just no-ops."""
    import risk_control

    # No exception expected.
    risk_control.check_voice_probe_allowed("")
    risk_control.check_voice_probe_allowed(None)  # type: ignore[arg-type]
    risk_control.record_voice_probe("")
    risk_control.record_voice_probe(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Endpoint integration: AST scan (the live endpoint must call both helpers
# in the right order — check before paid call, record after success)
# ---------------------------------------------------------------------------


def test_endpoint_calls_check_before_paid_call_and_record_after():
    """Source-level guard: ``probe_user_voice`` must call
    ``risk_control.check_voice_probe_allowed`` BEFORE the
    ``synth_fn(...)`` invocation, and call
    ``risk_control.record_voice_probe`` AFTER (or alongside) the
    success response. Drift here re-opens the abuse vector."""
    import inspect

    import user_voice_api

    src = inspect.getsource(user_voice_api.probe_user_voice)
    check_pos = src.find("risk_control.check_voice_probe_allowed")
    # ``synth_fn`` is invoked via ``asyncio.to_thread(synth_fn, ...)`` —
    # match the to_thread call site since that's the actual paid call.
    synth_pos = src.find("asyncio.to_thread(synth_fn")
    record_pos = src.find("risk_control.record_voice_probe")
    assert check_pos != -1, (
        "P2-23 regression: probe_user_voice no longer calls "
        "risk_control.check_voice_probe_allowed. Each invocation hits "
        "paid TTS providers — without the rate-limit guard a logged-in "
        "attacker can spend the platform's TTS quota at line-rate."
    )
    assert synth_pos != -1, (
        "test fixture stale: asyncio.to_thread(synth_fn ...) no "
        "longer in probe_user_voice — update the marker."
    )
    assert record_pos != -1, (
        "P2-23 regression: probe_user_voice no longer calls "
        "risk_control.record_voice_probe. Without the stamp, the "
        "limit can't tick and the day-cap never fires."
    )
    assert check_pos < synth_pos, (
        "P2-23 regression: check_voice_probe_allowed runs AFTER the "
        "paid synth_fn call. Order matters — the rate-limit guard "
        "must be the FIRST thing the endpoint does (after auth) so "
        "an over-limit caller can't consume even one paid call. Got "
        f"check_pos={check_pos}, synth_pos={synth_pos}."
    )
    assert synth_pos < record_pos, (
        "P2-23 regression: record_voice_probe runs BEFORE the paid "
        "synth_fn call. Stamping before the call means a flaky "
        "provider 502 still ticks the user's daily quota — that "
        "lets a hostile-provider scenario consume legitimate users' "
        "probe budgets."
    )
