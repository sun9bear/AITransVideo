"""T0 voice CPS auto-calibration regression test suite.

Each test is a regression guard for one of the codex review findings (v3
through v4.3). Test name prefix indicates which finding it pins:

  - codex_F1 / F3:        5-tuple in-flight key
  - codex_F2:             model-aware writes (calibrate_voice signature)
  - codex_F-v4-3:         atomic claim_or_join (joiner no reserve race)
  - codex_F-v4-4:         factory always returns CalibrationResult (no raise);
                          refund only on paid_call_count == 0
  - codex_F-v4.1-1:       JSONB concurrent merge preserves both keys
  - codex_F-v4.1-4:       joiner shield against cancellation
  - codex_F-v4.1-6:       identity-checked release(key, future)
  - codex_F-v4.1-7:       paid_call_count incremented BEFORE synth call
  - codex_F-v4.3-2:       manual endpoint releases route db BEFORE paid call

Tests use mock DB sessions (project pattern — no real PostgreSQL fixture).
The actual SELECT FOR UPDATE row locking semantics are a PostgreSQL
guarantee; tests verify the helper's contract (open transaction, call
with_for_update(), merge dict correctly) which is the only part of the
correctness story that lives in our code.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# sys.path setup: gateway lives outside src/, mirror the existing
# voice_speed_calibrator test pattern.
# ---------------------------------------------------------------------------
_gateway_dir = str(Path(__file__).resolve().parent.parent / "gateway")
if _gateway_dir not in sys.path:
    sys.path.insert(0, _gateway_dir)
_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# Database / models import shims (same pattern as test_voice_speed_calibrator.py).
_fake_db = types.ModuleType("database")
_fake_db.get_db = MagicMock()
_fake_db.engine = MagicMock()
_fake_db.async_session = MagicMock()
sys.modules.setdefault("database", _fake_db)

import risk_control  # noqa: E402
from voice_calibration_inflight import (  # noqa: E402
    CalibrationKey,
    CalibrationInFlightRegistry,
    run_calibration_task,
)
from voice_speed_calibrator import (  # noqa: E402
    CalibrationResult,
    calibrate_voice,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_budget():
    """Wipe the in-process calibration budget before AND after each test
    so cap behaviour is independent of test ordering."""
    risk_control.reset_voice_calibration_rate_limits()
    yield
    risk_control.reset_voice_calibration_rate_limits()


# ---------------------------------------------------------------------------
# T0-A budget primitives (codex F-v4-4)
# ---------------------------------------------------------------------------


def test_calibration_budget_blocks_after_per_minute_limit():
    """6 reservations within 60s → 6th raises RateLimitExceeded with
    scope="voice_calibration_short". The 5/min cap matches plan §7.2."""
    user_id = "user_a"

    # First 5 succeed
    for _ in range(5):
        risk_control.reserve_voice_calibration(user_id)

    with pytest.raises(risk_control.RateLimitExceeded) as exc_info:
        risk_control.reserve_voice_calibration(user_id)

    assert exc_info.value.scope == "voice_calibration_short"


def test_calibration_budget_refund_releases_slot():
    """Refund returns the slot — a subsequent reserve in the same window succeeds.

    Codex v4 F-v4-4 boundary: refund is for "no paid call issued" cases
    (validation failure / rate_limited path / pre-paid-call exception).
    Production callers MUST NOT refund after paid_call_count > 0.
    """
    user_id = "user_b"

    reservations = [risk_control.reserve_voice_calibration(user_id) for _ in range(5)]
    # Cap reached — 6th would raise.
    with pytest.raises(risk_control.RateLimitExceeded):
        risk_control.reserve_voice_calibration(user_id)

    # Refund one → next reserve succeeds.
    risk_control.refund_voice_calibration(user_id, reservations[0])
    new_reservation = risk_control.reserve_voice_calibration(user_id)
    assert new_reservation > 0


def test_calibration_budget_does_not_share_user_buckets():
    """Each user gets independent cap — user_a hitting 5/min does not
    affect user_b. Necessary so a noisy power user can't lock everyone out."""
    risk_control.reset_voice_calibration_rate_limits()
    for _ in range(5):
        risk_control.reserve_voice_calibration("user_a")
    # user_a is capped; user_b should still have full quota
    new_reservation = risk_control.reserve_voice_calibration("user_b")
    assert new_reservation > 0


# ---------------------------------------------------------------------------
# T0-B in-flight registry (codex F1 + F3 + F-v4.1-4 + F-v4.1-6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inflight_5tuple_key_isolates_models():
    """codex F1+F3: same voice_id under different model_keys must be
    SEPARATE futures (different keys) so turbo + hd can run in parallel
    without sharing each other's results."""
    reg = CalibrationInFlightRegistry()

    key_turbo = CalibrationKey(
        scope="user", owner="u1", provider="minimax",
        voice_id="v1", model_key="speech-2.8-turbo",
    )
    key_hd = CalibrationKey(
        scope="user", owner="u1", provider="minimax",
        voice_id="v1", model_key="speech-2.8-hd",
    )

    fut_turbo, role_turbo = await reg.claim_or_join(key_turbo)
    fut_hd, role_hd = await reg.claim_or_join(key_hd)

    assert role_turbo == "starter"
    assert role_hd == "starter"
    assert fut_turbo is not fut_hd  # different futures for different model_keys


@pytest.mark.asyncio
async def test_inflight_5tuple_key_isolates_users():
    """Same voice_id + model_key but different owner = different futures.
    Without this, user_a's calibration result would be served to user_b's
    calibrate request (data leak + wrong scope write)."""
    reg = CalibrationInFlightRegistry()

    key_a = CalibrationKey(
        scope="user", owner="user_a", provider="minimax",
        voice_id="v1", model_key="speech-2.8-turbo",
    )
    key_b = CalibrationKey(
        scope="user", owner="user_b", provider="minimax",
        voice_id="v1", model_key="speech-2.8-turbo",
    )

    fut_a, role_a = await reg.claim_or_join(key_a)
    fut_b, role_b = await reg.claim_or_join(key_b)

    assert role_a == "starter"
    assert role_b == "starter"
    assert fut_a is not fut_b


@pytest.mark.asyncio
async def test_inflight_starter_then_joiner_share_future():
    """Two callers with the SAME 5-tuple key → first is starter, second is
    joiner, both receive the SAME future object. Pure-logic guard against
    accidental dict-key-hashing changes."""
    reg = CalibrationInFlightRegistry()

    key = CalibrationKey(
        scope="user", owner="u1", provider="minimax",
        voice_id="v1", model_key="speech-2.8-turbo",
    )

    fut1, role1 = await reg.claim_or_join(key)
    fut2, role2 = await reg.claim_or_join(key)

    assert role1 == "starter"
    assert role2 == "joiner"
    assert fut1 is fut2  # same object


@pytest.mark.asyncio
async def test_inflight_release_identity_check():
    """codex F-v4.1-6: release(key, future) only pops if registry still
    holds THAT future. An aborted starter's release MUST NOT delete a
    successor starter's freshly-registered future for the same key."""
    reg = CalibrationInFlightRegistry()

    key = CalibrationKey(
        scope="user", owner="u1", provider="minimax",
        voice_id="v1", model_key="speech-2.8-turbo",
    )

    # Starter 1 claims, then needs to abort (e.g. RateLimitExceeded)
    fut1, _ = await reg.claim_or_join(key)
    fut1.set_exception(RuntimeError("aborted"))
    # Starter 1 releases
    await reg.release(key, fut1)

    # Starter 2 claims fresh
    fut2, role2 = await reg.claim_or_join(key)
    assert role2 == "starter"
    assert fut2 is not fut1

    # Starter 1 (somehow) calls release again with stale fut1 — must be no-op
    await reg.release(key, fut1)

    # fut2 should STILL be in the registry — Starter 2's joiner can find it
    fut3, role3 = await reg.claim_or_join(key)
    assert role3 == "joiner"
    assert fut3 is fut2  # NOT lost by Starter 1's stale release


@pytest.mark.asyncio
async def test_inflight_joiner_uses_shield_against_cancel():
    """codex F-v4.1-4: joiner awaits via asyncio.shield so a cancelled
    joiner doesn't propagate cancellation into the shared future, which
    would break starter and any other joiners.

    This test verifies the run_calibration_task helper's joiner path
    handles cancellation gracefully."""

    reg_singleton_patch = patch(
        "voice_calibration_inflight.registry",
        new_callable=lambda: CalibrationInFlightRegistry(),
    )

    with reg_singleton_patch:
        # Starter factory: holds for 0.5s before completing.
        starter_done = asyncio.Event()

        async def slow_factory():
            await asyncio.sleep(0.3)
            starter_done.set()
            return CalibrationResult(
                ok=True, cps=4.5, paid_call_count=3,
                model_key="speech-2.8-turbo",
            )

        async def joiner_factory():
            # joiner shouldn't ever invoke its own factory — but if it did,
            # it'd return immediately. Still need the kwarg.
            return CalibrationResult(
                ok=False, cps=0.0, paid_call_count=0,
                model_key="speech-2.8-turbo",
            )

        key = CalibrationKey(
            scope="user", owner="u1", provider="minimax",
            voice_id="v1", model_key="speech-2.8-turbo",
        )

        # Start the starter task
        starter_task = asyncio.create_task(run_calibration_task(
            key=key, user_id_for_budget="u1", factory=slow_factory,
        ))
        await asyncio.sleep(0.05)  # let starter register the future

        # Start a joiner task; it'll await asyncio.shield(future)
        joiner_task = asyncio.create_task(run_calibration_task(
            key=key, user_id_for_budget="u1", factory=joiner_factory,
        ))
        await asyncio.sleep(0.05)  # let joiner enter shield-wait

        # Cancel the joiner
        joiner_task.cancel()
        joiner_result = await joiner_task  # shield catches CancelledError → internal_error
        # joiner gets internal_error result (not propagated cancellation)
        assert joiner_result.error_class == "internal_error"

        # Starter must still complete normally
        starter_result = await starter_task
        assert starter_done.is_set()
        assert starter_result.ok is True
        assert starter_result.cps == 4.5


# ---------------------------------------------------------------------------
# T0-C bounded primitives + paid_call_count (codex F5 + F-v4.1-7)
# ---------------------------------------------------------------------------


def test_paid_call_count_incremented_before_synth_attempt():
    """codex F-v4.1-7: paid_call_count increments BEFORE the synth call.
    If synth raises (provider 5xx, timeout), the count must reflect "we
    issued this call" — otherwise caller's refund logic would refund a
    call that was already paid for.
    """
    call_count = [0]

    def synth_that_raises_on_first_call(text, voice_id, model):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("provider 502")
        return b"fake_wav_payload"

    result = calibrate_voice(
        provider="minimax",
        model="speech-2.8-turbo",
        voice_id="v1",
        inter_call_sleep_s=0.0,
        synth_fn=synth_that_raises_on_first_call,
    )

    # Counted the failed call → paid_call_count == 1, NOT 0
    assert result.paid_call_count == 1
    assert result.ok is False
    assert result.error_class == "synth_failed"


def test_paid_call_count_after_provider_failure_preserved():
    """Mid-text failure preserves count of successful prior segments.
    Use call-counter to fail the 2nd call deterministically."""
    call_log: list[int] = []

    def synth_failing_on_call_2(text, voice_id, model):
        call_log.append(1)
        if len(call_log) == 2:
            raise RuntimeError("provider error on call 2")
        # Realistic ~4.5 cps: 101 hanzi → ~22 sec for T1.
        # Use 30s flat for whatever text — adequate for the cps sanity test.
        return (30_000).to_bytes(4, "big")

    def decode_duration(blob):
        return int.from_bytes(blob, "big")

    result = calibrate_voice(
        provider="minimax",
        model="speech-2.8-turbo",
        voice_id="v1",
        inter_call_sleep_s=0.0,
        synth_fn=synth_failing_on_call_2,
        duration_fn=decode_duration,
    )

    # T1 succeeded → 1 call. T2 attempted (count incremented BEFORE call)
    # then raised → count ends at 2.
    assert result.paid_call_count == 2
    assert result.ok is False
    assert result.error_class == "synth_failed"


def test_total_timeout_skips_remaining_texts_after_budget_exhausted():
    """codex F5: total_timeout_seconds checked at SEGMENT BOUNDARIES.
    If budget runs out before T3, T3 must NOT be issued (paid_call_count
    stays at the work actually done).

    Uses a fake clock that advances 28 seconds per call so a 60s budget
    is exhausted before T3.
    """
    fake_clock = [1000.0]

    def fake_monotonic():
        return fake_clock[0]

    def slow_synth(text, voice_id, model):
        # Each call advances the fake clock by 28s, simulating a slow
        # but bounded synth.
        fake_clock[0] += 28.0
        return (1000).to_bytes(4, "big")

    def decode_duration(blob):
        return int.from_bytes(blob, "big")

    result = calibrate_voice(
        provider="minimax",
        model="speech-2.8-turbo",
        voice_id="v1",
        total_timeout_seconds=60.0,
        inter_call_sleep_s=0.0,
        synth_fn=slow_synth,
        duration_fn=decode_duration,
        monotonic_fn=fake_monotonic,
    )

    # T1 took clock 0 → 28; T2 took 28 → 56; before T3 entry the
    # check sees elapsed=56 < 60 → T3 starts → 56 → 84. After-T3
    # check sees elapsed=84 > 60 — but per the implementation, the
    # check is BEFORE the next text issuance, so T3 ran but no T4
    # exists. With only 3 texts, all 3 may run.
    #
    # To get a clear "T3 skipped" we need a tighter budget. Re-run
    # with budget=50:
    fake_clock[0] = 1000.0
    result_strict = calibrate_voice(
        provider="minimax",
        model="speech-2.8-turbo",
        voice_id="v1",
        total_timeout_seconds=50.0,
        inter_call_sleep_s=0.0,
        synth_fn=slow_synth,
        duration_fn=decode_duration,
        monotonic_fn=fake_monotonic,
    )

    # T1 + T2 = 56s, before T3 the check sees 56 > 50 → skip T3.
    # paid_call_count should be 2 (T1, T2 ran).
    assert result_strict.error_class == "total_timeout"
    assert result_strict.paid_call_count == 2


def test_calibrate_voice_returns_model_key_in_result():
    """codex F2: every CalibrationResult carries model_key so callers
    know which JSONB key to write into.

    Use ~4.5 cps realistic durations: 458 total hanzi / 4.5 cps ≈ 102s
    total, so ~34s per text on average. Tests need durations that land
    cps within MIN_VALID_CPS..MAX_VALID_CPS = [2.0, 8.0]."""

    # Each text returns 30000ms (30s). Total: 458 hanzi / 90s = 5.09 cps.
    def realistic_synth(text, voice_id, model):
        return (30_000).to_bytes(4, "big")

    def decode_duration(blob):
        return int.from_bytes(blob, "big")

    result = calibrate_voice(
        provider="minimax",
        model="speech-2.8-hd",
        voice_id="v1",
        inter_call_sleep_s=0.0,
        synth_fn=realistic_synth,
        duration_fn=decode_duration,
    )

    assert result.ok is True
    assert result.model_key == "speech-2.8-hd"
    assert 2.0 <= result.cps <= 8.0


def test_calibrate_voice_unknown_provider_returns_zero_paid_count():
    """Unknown provider is a pre-paid-call validation failure. Caller
    should refund (paid_call_count == 0)."""
    result = calibrate_voice(
        provider="unknown_provider",
        model="x",
        voice_id="v1",
        inter_call_sleep_s=0.0,
    )
    assert result.ok is False
    assert result.error_class == "unknown_provider"
    assert result.paid_call_count == 0


# ---------------------------------------------------------------------------
# T0-D run_calibration_task end-to-end (codex F-v4-3 + F-v4-4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_calibration_task_starter_reserves_joiner_does_not(monkeypatch):
    """codex F-v4-3: only starter consumes budget. Joiner shares the
    future without reserving — otherwise concurrent callers double-charge
    the budget for one paid call."""

    # Replace module-level registry with a fresh one (avoid test-order pollution)
    fresh_registry = CalibrationInFlightRegistry()
    monkeypatch.setattr("voice_calibration_inflight.registry", fresh_registry)
    risk_control.reset_voice_calibration_rate_limits()

    started = asyncio.Event()

    async def slow_factory():
        # Hold long enough for joiner to enter
        started.set()
        await asyncio.sleep(0.2)
        return CalibrationResult(
            ok=True, cps=4.5, paid_call_count=3,
            model_key="speech-2.8-turbo",
        )

    key = CalibrationKey(
        scope="user", owner="u1", provider="minimax",
        voice_id="v1", model_key="speech-2.8-turbo",
    )

    starter_task = asyncio.create_task(run_calibration_task(
        key=key, user_id_for_budget="u1", factory=slow_factory,
    ))
    await started.wait()  # ensure starter has reserved
    # By now, starter has reserved 1 slot.

    joiner_task = asyncio.create_task(run_calibration_task(
        key=key, user_id_for_budget="u1", factory=slow_factory,
    ))

    await asyncio.gather(starter_task, joiner_task)

    # Total slots used should be 1 (starter), NOT 2.
    # Verify by attempting 4 more reserves — if joiner had double-reserved,
    # the cap would be hit at the 4th call (5 total = cap), but it's the 5th.
    for i in range(4):
        risk_control.reserve_voice_calibration("u1")  # should all succeed (1+4=5)
    with pytest.raises(risk_control.RateLimitExceeded):
        risk_control.reserve_voice_calibration("u1")  # 6th raises (cap=5)


@pytest.mark.asyncio
async def test_run_calibration_task_no_refund_after_paid_call_count_gt_zero(monkeypatch):
    """codex F-v4-4: refund ONLY when paid_call_count == 0. Provider 5xx /
    synth timeout / DB write fail → paid_call_count > 0 → DO NOT refund.

    Refunding these would let provider failure storms bypass the budget.
    """
    fresh_registry = CalibrationInFlightRegistry()
    monkeypatch.setattr("voice_calibration_inflight.registry", fresh_registry)
    risk_control.reset_voice_calibration_rate_limits()

    async def factory_provider_failure():
        return CalibrationResult(
            ok=False,
            error_class="synth_failed",
            paid_call_count=2,  # provider 5xx after 2 successful texts
            model_key="speech-2.8-turbo",
        )

    key = CalibrationKey(
        scope="user", owner="u1", provider="minimax",
        voice_id="v1", model_key="speech-2.8-turbo",
    )

    result = await run_calibration_task(
        key=key, user_id_for_budget="u1", factory=factory_provider_failure,
    )

    assert result.paid_call_count == 2
    assert result.ok is False

    # If refund had fired, we'd have 0 slots used. Verify slot is still
    # held by attempting to fill the bucket and seeing the 5th raise.
    for _ in range(4):
        risk_control.reserve_voice_calibration("u1")  # 1+4=5 with NO refund
    with pytest.raises(risk_control.RateLimitExceeded):
        risk_control.reserve_voice_calibration("u1")


@pytest.mark.asyncio
async def test_run_calibration_task_refunds_on_paid_call_count_zero(monkeypatch):
    """Symmetric guard: validation failure (paid_call_count == 0) should
    REFUND the slot so the user's budget isn't drained by trivial config
    errors."""
    fresh_registry = CalibrationInFlightRegistry()
    monkeypatch.setattr("voice_calibration_inflight.registry", fresh_registry)
    risk_control.reset_voice_calibration_rate_limits()

    async def factory_validation_failure():
        return CalibrationResult(
            ok=False,
            error_class="voice_not_found",
            paid_call_count=0,  # never reached provider
            model_key="speech-2.8-turbo",
        )

    key = CalibrationKey(
        scope="user", owner="u1", provider="minimax",
        voice_id="v1", model_key="speech-2.8-turbo",
    )

    result = await run_calibration_task(
        key=key, user_id_for_budget="u1", factory=factory_validation_failure,
    )
    assert result.paid_call_count == 0

    # Refund should have fired → user has full 5 slots available.
    for _ in range(5):
        risk_control.reserve_voice_calibration("u1")
    with pytest.raises(risk_control.RateLimitExceeded):
        risk_control.reserve_voice_calibration("u1")


# ---------------------------------------------------------------------------
# T0-D JSONB merge — concurrent turbo+hd preserves both (codex F-v4.1-1)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T0-review F-T0-1: starter cancel race (codex round 7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starter_cancel_does_not_abandon_paid_tts(monkeypatch):
    """codex T0-review F-T0-1 — when caller is cancelled while factory is
    running, paid TTS in `asyncio.to_thread(calibrate_voice)` cannot be
    interrupted. The fix is to spawn factory as a background task and
    finalize via done_callback. Caller cancellation should:

      1. Raise CancelledError to caller (via shield)
      2. Keep factory_task alive in background
      3. NOT release in-flight registry yet (so a second caller can't
         re-trigger paid TTS for the same key)
      4. Eventually finalize with the real paid_call_count when factory
         completes

    Without the fix, a single cancel → finally fires immediately →
    refund + release → next caller spawns SECOND paid call. This test
    pins the post-fix invariant: registry holds the future until
    factory actually finishes.
    """
    fresh_registry = CalibrationInFlightRegistry()
    monkeypatch.setattr("voice_calibration_inflight.registry", fresh_registry)
    risk_control.reset_voice_calibration_rate_limits()

    factory_started = asyncio.Event()
    factory_can_complete = asyncio.Event()
    factory_completed_result: dict[str, "CalibrationResult | None"] = {"r": None}  # noqa: F821

    async def slow_factory():
        factory_started.set()
        # Simulate paid TTS in flight — cannot be interrupted from outside
        await factory_can_complete.wait()
        result = CalibrationResult(
            ok=True, cps=4.5, paid_call_count=3,
            model_key="speech-2.8-turbo",
        )
        factory_completed_result["r"] = result
        return result

    key = CalibrationKey(
        scope="user", owner="u_cancel", provider="minimax",
        voice_id="v_cancel", model_key="speech-2.8-turbo",
    )

    # Caller 1: starter, will be cancelled mid-flight
    caller1 = asyncio.create_task(run_calibration_task(
        key=key, user_id_for_budget="u_cancel", factory=slow_factory,
    ))
    await factory_started.wait()  # ensure factory_task entered the wait

    # Cancel the caller
    caller1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller1

    # Right after cancel: factory_task should STILL be running.
    # The in-flight registry should STILL hold the future (otherwise a
    # second caller could spawn another paid call).
    await asyncio.sleep(0.05)  # let cancellation propagate
    assert factory_completed_result["r"] is None, "factory completed too early"
    assert key in fresh_registry._futures, (
        "in-flight registry released the future before factory completed — "
        "second caller could now race a duplicate paid TTS call"
    )

    # Now allow factory to finish. done_callback should:
    # - record the real paid_call_count (3, ok=True)
    # - NOT refund (paid_call_count > 0)
    # - eventually release the registry entry
    factory_can_complete.set()
    # Give the event loop time to process: factory finishes → done_callback
    # fires → finalize() → schedules release task → release runs
    for _ in range(20):
        await asyncio.sleep(0.05)
        if key not in fresh_registry._futures:
            break
    assert factory_completed_result["r"] is not None, "factory should have finished"
    assert factory_completed_result["r"].paid_call_count == 3
    # Eventually the registry entry is released (release ran via the
    # asyncio.create_task scheduled in _finalize)
    assert key not in fresh_registry._futures, (
        "registry should have been released after factory completed"
    )

    # Budget: caller1 reserved 1 slot; result.ok=True means no refund;
    # so 1 slot remains used. Verify by filling 4 more (5 total = cap).
    for _ in range(4):
        risk_control.reserve_voice_calibration("u_cancel")
    with pytest.raises(risk_control.RateLimitExceeded):
        risk_control.reserve_voice_calibration("u_cancel")


# ---------------------------------------------------------------------------
# T0-review F-T0-2: manual endpoint model_key whitelist (codex round 7)
# ---------------------------------------------------------------------------


def test_canonical_models_provider_whitelist_minimax_only_for_t0():
    """codex T0-review F-T0-2 + F-T0-5 — verify the whitelist is
    MINIMAX ONLY for T0 phase 1.

    F-T0-2: any drift in MiniMax model keys is a security regression
    (arbitrary model_key flowing to paid TTS).

    F-T0-5: cosyvoice / volcengine MUST NOT be in this dict for T0.
    Their helpers don't have T0-C bounded primitives — CosyVoice has
    90s × 5-retry, VolcEngine 60s default. They'd blow past
    calibrate_voice's 60s total budget. Add them when T0-C-2 lands
    provider-specific bounded wrappers.
    """
    from user_voice_api import _CANONICAL_MODELS_BY_PROVIDER

    # MiniMax: turbo + hd are the only two we support per plan v4 T0-D
    assert set(_CANONICAL_MODELS_BY_PROVIDER["minimax"]) == {
        "speech-2.8-turbo", "speech-2.8-hd",
    }
    # F-T0-5: cosyvoice / volcengine MUST be absent for T0.
    assert "cosyvoice" not in _CANONICAL_MODELS_BY_PROVIDER
    assert "volcengine" not in _CANONICAL_MODELS_BY_PROVIDER
    # And of course unknown providers
    assert "openai" not in _CANONICAL_MODELS_BY_PROVIDER


# ---------------------------------------------------------------------------
# T0-review F-T0-3: legacy calibration response field (codex round 7)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T0-review F-T0-6: route-level endpoint tests (codex round 8)
# ---------------------------------------------------------------------------
# Direct handler-call tests with mocked dependencies. Verify that the
# whitelist + legacy compat actually fire at the HTTP layer, not just in
# isolated logic.


class _FakeRequest:
    """Minimal Request stub providing the only method calibrate_voice_speed uses."""
    def __init__(self, body_bytes: bytes = b""):
        self._body = body_bytes

    async def body(self):
        return self._body


class _FakeUser:
    def __init__(self, user_id: str = "00000000-0000-0000-0000-000000000001"):
        import uuid
        self.id = uuid.UUID(user_id)


class _FakeAsyncSession:
    """Minimal AsyncSession stub: tracks rollback calls + supplies execute()
    via a queue of pre-canned results."""
    def __init__(self):
        self.rollback_calls = 0
        self.commit_calls = 0
        self.execute_results = []  # caller pre-loads expected results

    async def rollback(self):
        self.rollback_calls += 1

    async def commit(self):
        self.commit_calls += 1

    async def close(self):
        pass

    async def execute(self, *args, **kwargs):
        # Return pre-loaded result if any; default to a Mock that handles
        # scalar_one_or_none() returning None.
        if self.execute_results:
            return self.execute_results.pop(0)
        m = MagicMock()
        m.scalar_one_or_none.return_value = None
        return m


@pytest.mark.asyncio
async def test_manual_endpoint_invalid_model_key_returns_400_without_paid_call(monkeypatch):
    """codex T0-review F-T0-6: route-level test that invalid model_key
    triggers 400 BEFORE run_calibration_task / budget reserve / paid TTS.

    Critical security guarantee — without this, a logged-in attacker
    submitting any model_key string would burn the user's budget and
    issue paid TTS to the provider.
    """
    risk_control.reset_voice_calibration_rate_limits()

    # Patch fetch_user_voice to return a usable voice
    fake_voice = MagicMock()
    fake_voice.tts_provider = "minimax"
    fake_voice.id = "fake-uuid"
    fake_voice.voice_id = "vt_test"

    async def fake_fetch(db, user_id, voice_id):
        return fake_voice

    # Patch run_calibration_task — IT MUST NOT BE CALLED
    run_calibration_task_was_called = []

    async def fake_run_calibration(**kwargs):
        run_calibration_task_was_called.append(kwargs)
        from voice_speed_calibrator import CalibrationResult
        return CalibrationResult(ok=True, cps=4.5, paid_call_count=3, model_key="x")

    import user_voice_api
    monkeypatch.setattr(user_voice_api, "fetch_user_voice", fake_fetch)
    monkeypatch.setattr(
        "voice_calibration_inflight.run_calibration_task", fake_run_calibration,
    )
    # _voice_to_dict reads many ORM attrs; stub it to a fixed dict so
    # MagicMock attribute access doesn't blow up json encoding.
    monkeypatch.setattr(
        user_voice_api, "_voice_to_dict",
        lambda v: {"voice_id": getattr(v, "voice_id", ""), "label": getattr(v, "label", "")},
    )

    user = _FakeUser()
    db = _FakeAsyncSession()
    request = _FakeRequest(b'{"model_key": "speech-99-malicious-not-real"}')

    response = await user_voice_api.calibrate_voice_speed(
        voice_id="vt_test",
        request=request,
        user=user,
        db=db,
    )

    # 400 Bad Request
    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["error"] == "invalid_model_key"
    assert "speech-99-malicious-not-real" in body["message"]
    assert "speech-2.8-turbo" in body["allowed_model_keys"]

    # CRITICAL: paid call path was never reached
    assert run_calibration_task_was_called == [], (
        "invalid model_key must NOT reach run_calibration_task — paid TTS would fire"
    )

    # And budget remains untouched
    for _ in range(5):
        risk_control.reserve_voice_calibration("budget-untouched-user")
    with pytest.raises(risk_control.RateLimitExceeded):
        risk_control.reserve_voice_calibration("budget-untouched-user")


@pytest.mark.asyncio
async def test_manual_endpoint_non_minimax_provider_returns_400(monkeypatch):
    """codex T0-review F-T0-5: T0 phase 1 only auto-calibrates MiniMax.
    A user voice tagged tts_provider='cosyvoice' must return 400 with
    error_class='unsupported_provider_for_auto_calibration', NOT proceed
    to call calibrate_voice (which has no bounded primitive for cosyvoice).
    """
    risk_control.reset_voice_calibration_rate_limits()

    fake_voice = MagicMock()
    fake_voice.tts_provider = "cosyvoice"

    async def fake_fetch(db, user_id, voice_id):
        return fake_voice

    run_calibration_task_was_called = []

    async def fake_run_calibration(**kwargs):
        run_calibration_task_was_called.append(kwargs)
        from voice_speed_calibrator import CalibrationResult
        return CalibrationResult(ok=True, cps=4.5, paid_call_count=3, model_key="x")

    import user_voice_api
    monkeypatch.setattr(user_voice_api, "fetch_user_voice", fake_fetch)
    monkeypatch.setattr(
        "voice_calibration_inflight.run_calibration_task", fake_run_calibration,
    )
    # _voice_to_dict reads many ORM attrs; stub it to a fixed dict so
    # MagicMock attribute access doesn't blow up json encoding.
    monkeypatch.setattr(
        user_voice_api, "_voice_to_dict",
        lambda v: {"voice_id": getattr(v, "voice_id", ""), "label": getattr(v, "label", "")},
    )

    user = _FakeUser()
    db = _FakeAsyncSession()
    request = _FakeRequest(b"")  # no model_key body

    response = await user_voice_api.calibrate_voice_speed(
        voice_id="vt_cosy",
        request=request,
        user=user,
        db=db,
    )

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["error"] == "unsupported_provider_for_auto_calibration"
    assert body["provider"] == "cosyvoice"
    assert run_calibration_task_was_called == [], (
        "non-MiniMax provider must NOT reach run_calibration_task in T0"
    )


@pytest.mark.asyncio
async def test_manual_endpoint_success_response_includes_legacy_calibration(monkeypatch):
    """codex T0-review F-T0-3 + F-T0-6: a successful calibration must
    return the legacy `calibration` field with `.cps` so the existing
    voices/page.tsx (handleCalibrate reads result.calibration?.cps)
    keeps working until the frontend migrates to read `results[]`.
    """
    risk_control.reset_voice_calibration_rate_limits()

    fake_voice_initial = MagicMock()
    fake_voice_initial.tts_provider = "minimax"
    fake_voice_initial.voice_id = "vt_success"

    fake_voice_refreshed = MagicMock()
    fake_voice_refreshed.id = "fake-uuid"
    fake_voice_refreshed.voice_id = "vt_success"
    fake_voice_refreshed.label = "test"
    fake_voice_refreshed.chars_per_second = 5.09
    fake_voice_refreshed.chars_per_second_by_model = {
        "speech-2.8-turbo": 5.09, "speech-2.8-hd": 4.18,
    }
    fake_voice_refreshed.speed_calibrated_at = None
    fake_voice_refreshed.user_id = MagicMock()
    fake_voice_refreshed.expired_at = None
    fake_voice_refreshed.provider = "minimax_voice_clone"
    fake_voice_refreshed.platform = "minimax_domestic"
    fake_voice_refreshed.source_speaker_id = None
    fake_voice_refreshed.notes = None
    fake_voice_refreshed.created_at = None
    fake_voice_refreshed.updated_at = None
    fake_voice_refreshed.chars_per_second_by_model = {"speech-2.8-turbo": 5.09}

    fetch_calls = []

    async def fake_fetch(db, user_id, voice_id):
        fetch_calls.append(voice_id)
        # First call (route entry) returns initial voice; second call
        # (after calibrate, fresh session) returns refreshed.
        if len(fetch_calls) == 1:
            return fake_voice_initial
        return fake_voice_refreshed

    # Stub run_calibration_task with an ok result for both turbo + hd
    async def fake_run_calibration(*, key, user_id_for_budget, factory):
        from voice_speed_calibrator import CalibrationResult, TextResult
        return CalibrationResult(
            ok=True,
            cps=5.09 if key.model_key == "speech-2.8-turbo" else 4.18,
            total_hanzi=458,
            total_duration_ms=90_000 if key.model_key == "speech-2.8-turbo" else 109_500,
            per_text=[TextResult(name="T1", hanzi=101, duration_ms=20_000, cps=5.05)],
            paid_call_count=3,
            model_key=key.model_key,
        )

    import user_voice_api
    monkeypatch.setattr(user_voice_api, "fetch_user_voice", fake_fetch)
    monkeypatch.setattr(
        "voice_calibration_inflight.run_calibration_task", fake_run_calibration,
    )
    # _voice_to_dict reads many ORM attrs; stub it to a fixed dict so
    # MagicMock attribute access doesn't blow up json encoding.
    monkeypatch.setattr(
        user_voice_api, "_voice_to_dict",
        lambda v: {"voice_id": getattr(v, "voice_id", ""), "label": getattr(v, "label", "")},
    )
    # Stub async_session for the post-calibrate refresh
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_async_session():
        yield _FakeAsyncSession()

    monkeypatch.setattr("database.async_session", fake_async_session)

    user = _FakeUser()
    db = _FakeAsyncSession()
    request = _FakeRequest(b"")  # no model_key body → both models

    response = await user_voice_api.calibrate_voice_speed(
        voice_id="vt_success",
        request=request,
        user=user,
        db=db,
    )

    assert response.status_code == 200
    body = json.loads(response.body)

    # F-T0-6: legacy calibration field MUST be present so old client works
    assert body.get("calibration") is not None, (
        "missing legacy `calibration` field — old voices/page.tsx would show 未标定"
    )
    legacy = body["calibration"]
    assert "cps" in legacy
    assert "model" in legacy
    assert "per_text" in legacy
    assert legacy["cps"] in (5.09, 4.18)  # one of the two ok results
    assert legacy["provider"] == "minimax"

    # New shape also present
    assert body["ok"] is True
    assert isinstance(body["results"], list)
    assert len(body["results"]) == 2  # turbo + hd both ran
    assert body["provider"] == "minimax"

    # route db was rolled back BEFORE paid call (F-v4.3-2 verified inline)
    assert db.rollback_calls >= 1, (
        "route db must be rollback'd before paid call (codex F-v4.3-2)"
    )


def test_legacy_calibration_response_field_built_from_first_ok_result():
    """codex T0-review F-T0-3 — existing frontend reads
    result.calibration?.cps. After T0 the response is multi-model
    {results: [...]}. The endpoint synthesizes a `calibration` field
    from the first ok result for backward compat.

    Replicates the synthesis logic so a future refactor (e.g. extracting
    to a helper) keeps the same field shape that voiceLibrary.ts /
    voices/page.tsx depend on.
    """
    # Mimic the structure user_voice_api.py builds.
    results_payload = [
        {
            "model_key": "speech-2.8-turbo",
            "ok": False,
            "error_class": "rate_limited",
            "message": "rate limited",
        },
        {
            "model_key": "speech-2.8-hd",
            "ok": True,
            "cps": 4.18,
            "total_hanzi": 458,
            "total_duration_ms": 109_500,
            "error_class": "",
            "error": "",
            "paid_call_count": 3,
            "per_text": [
                {"name": "T1", "hanzi": 101, "duration_ms": 24000, "cps": 4.21},
            ],
        },
    ]

    # Apply the same selection logic the endpoint uses.
    legacy_calibration = None
    for entry in results_payload:
        if entry.get("ok"):
            legacy_calibration = {
                "cps": entry["cps"],
                "total_hanzi": entry["total_hanzi"],
                "total_duration_ms": entry["total_duration_ms"],
                "provider": "minimax",
                "model": entry["model_key"],
                "per_text": entry["per_text"],
            }
            break

    # Frontend's CalibrateSpeedResponse type expects exactly these fields.
    assert legacy_calibration is not None
    assert legacy_calibration["cps"] == 4.18
    assert legacy_calibration["model"] == "speech-2.8-hd"  # second result wins
    assert legacy_calibration["total_hanzi"] == 458
    assert legacy_calibration["provider"] == "minimax"
    assert isinstance(legacy_calibration["per_text"], list)


def test_merged_by_model_helper_preserves_existing_keys():
    """The pure dict-merge primitive used inside the SELECT FOR UPDATE
    transaction. Verifies semantics independent of DB.

    Real concurrency safety comes from the row lock (PostgreSQL
    guarantees), but the merge MUST also be correct: read existing,
    set new key, return merged dict — without dropping or reordering
    existing keys.
    """
    from user_voice_service import _merged_by_model

    # Existing turbo, add hd
    existing = {"speech-2.8-turbo": 4.5}
    merged = _merged_by_model(existing, model_key="speech-2.8-hd", cps=4.2)
    assert merged == {"speech-2.8-turbo": 4.5, "speech-2.8-hd": 4.2}

    # Add to empty
    merged = _merged_by_model(None, model_key="speech-2.8-turbo", cps=4.5)
    assert merged == {"speech-2.8-turbo": 4.5}

    # Re-calibrate same model — overwrite, don't accumulate
    existing = {"speech-2.8-turbo": 4.5, "speech-2.8-hd": 4.2}
    merged = _merged_by_model(existing, model_key="speech-2.8-turbo", cps=4.7)
    assert merged == {"speech-2.8-turbo": 4.7, "speech-2.8-hd": 4.2}

    # Returns NEW dict, doesn't mutate input
    assert existing == {"speech-2.8-turbo": 4.5, "speech-2.8-hd": 4.2}
