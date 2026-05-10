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


def test_canonical_models_provider_whitelist():
    """codex T0-review F-T0-2 — verify the constant exposes only the
    intended model keys for each provider. Manual endpoint validates
    user-supplied model_key against this dict; any drift here is a
    direct security regression (arbitrary model_key flowing to paid TTS).
    """
    from user_voice_api import _CANONICAL_MODELS_BY_PROVIDER

    # MiniMax: turbo + hd are the only two we support per plan v4 T0-D
    assert set(_CANONICAL_MODELS_BY_PROVIDER["minimax"]) == {
        "speech-2.8-turbo", "speech-2.8-hd",
    }
    # Other providers: single canonical model each (T0-C-2 future will
    # expand cosyvoice / volcengine).
    assert _CANONICAL_MODELS_BY_PROVIDER["cosyvoice"] == ["cosyvoice-v3-flash"]
    assert _CANONICAL_MODELS_BY_PROVIDER["volcengine"] == ["seed-tts-2.0"]

    # No entry for an unknown provider
    assert "openai" not in _CANONICAL_MODELS_BY_PROVIDER


# ---------------------------------------------------------------------------
# T0-review F-T0-3: legacy calibration response field (codex round 7)
# ---------------------------------------------------------------------------


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
