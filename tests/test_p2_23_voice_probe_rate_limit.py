"""P2-23 (audit 2026-05-07) regression: voice-probe per-user rate limit.

Audit reference:
    docs/audits/2026-05-07-comprehensive-codebase-audit.md
        P2-23 — `/gateway/user-voices/probe` calls paid TTS providers
                (MiniMax / CosyVoice / VolcEngine) with no rate limit.
                A logged-in attacker could spend the platform's TTS
                quota at line-rate.

The fix introduces ``risk_control.reserve_voice_probe`` (atomic check
+ append BEFORE the paid call) and ``refund_voice_probe`` (rollback on
provider failure / bad input) with two windows:
  * 10 calls per 60 seconds (per user)
  * 100 calls per 24 hours (per user)

P2-23 follow-up (Codex review of 2a9c529): the v0 split between
``check_voice_probe_allowed`` (BEFORE the await) and
``record_voice_probe`` (AFTER the await) was vulnerable to concurrent
burst — N coroutines all passed the check before anyone stamped, so
all N reached the paid provider in parallel. The v1 reserve/refund
pattern closes the gap by making check + append atomic under one
lock. The concurrency test below proves exactly that.
"""
from __future__ import annotations

import sys
import threading
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
# Reserve / refund happy path
# ---------------------------------------------------------------------------


def test_reserve_returns_timestamp_and_records_slot():
    """``reserve_voice_probe`` returns a non-zero timestamp on success
    and the slot counts toward the limit."""
    import risk_control

    user = "user-fixture-1"
    ts = risk_control.reserve_voice_probe(user)
    assert ts > 0, (
        "P2-23 regression: reserve_voice_probe returned 0/None for a "
        "valid user. The reservation timestamp is needed for refund."
    )
    # The slot is now counted — buffer length should be 1.
    import risk_control as _rc
    assert len(_rc._voice_probe_buf[user]) == 1


def test_eleventh_concurrent_reservation_raises():
    """The 11th reservation in a short window must raise. Sequential
    test (no threading) — proves the basic check works."""
    import risk_control

    user = "user-fixture-2"
    for _ in range(10):
        risk_control.reserve_voice_probe(user)
    with pytest.raises(risk_control.RateLimitExceeded) as excinfo:
        risk_control.reserve_voice_probe(user)
    assert excinfo.value.scope == "voice_probe_short"


def test_refund_returns_slot_to_pool():
    """``refund_voice_probe`` removes the matching reservation so a
    later call can succeed. Critical for the failure-on-paid-call
    refund path."""
    import risk_control

    user = "user-fixture-3"
    # Fill to 10.
    reservations = [risk_control.reserve_voice_probe(user) for _ in range(10)]
    # 11th raises.
    with pytest.raises(risk_control.RateLimitExceeded):
        risk_control.reserve_voice_probe(user)
    # Refund one — quota now has room for one more.
    risk_control.refund_voice_probe(user, reservations[5])
    # Now we can reserve again.
    new_ts = risk_control.reserve_voice_probe(user)
    assert new_ts > 0


def test_refund_only_pops_matching_reservation():
    """Two concurrent failures must NOT refund each other's slots —
    refund pops the specific timestamp passed in, not "the most recent".

    Inject distinct timestamps directly to bypass clock-resolution
    issues on Windows runners (``time.monotonic()`` can have 15ms
    resolution, so two back-to-back reservations may share a value).
    The behavior we're testing is the deque's ``remove(value)``
    semantics, which is independent of whether the values came from
    a real clock or a synthetic injection.
    """
    import risk_control

    user = "user-fixture-4"
    a_fake = 1000.123
    b_fake = 2000.456
    buf = risk_control._voice_probe_buf[user]
    buf.append(a_fake)
    buf.append(b_fake)
    risk_control.refund_voice_probe(user, a_fake)
    # b is still reserved; a is gone.
    assert b_fake in buf
    assert a_fake not in buf


def test_refund_zero_or_empty_user_id_noop():
    """Defensive: refund with bad inputs must not crash."""
    import risk_control

    risk_control.refund_voice_probe("user", 0.0)  # zero ts
    risk_control.refund_voice_probe("", 12345.0)  # empty user
    risk_control.refund_voice_probe(None, 12345.0)  # type: ignore[arg-type]


def test_refund_already_pruned_reservation_noop():
    """If the reservation timestamp was pruned out of the buffer
    (window passed) before refund could fire, refund must silently
    no-op rather than raise."""
    import risk_control

    user = "user-fixture-5"
    # Reserve, then manually clear (simulates pruning).
    ts = risk_control.reserve_voice_probe(user)
    risk_control.reset_voice_probe_rate_limits()
    # Refund should not raise.
    risk_control.refund_voice_probe(user, ts)


# ---------------------------------------------------------------------------
# Per-user isolation
# ---------------------------------------------------------------------------


def test_per_user_isolation():
    """One user hitting their cap must NOT block a different user."""
    import risk_control

    user_a = "user-a"
    user_b = "user-b"
    for _ in range(10):
        risk_control.reserve_voice_probe(user_a)
    with pytest.raises(risk_control.RateLimitExceeded):
        risk_control.reserve_voice_probe(user_a)
    # user_b is untouched.
    risk_control.reserve_voice_probe(user_b)


# ---------------------------------------------------------------------------
# Day-window cap
# ---------------------------------------------------------------------------


def test_day_window_cap_via_buffer_injection():
    """Inject 100 timestamps directly into the buffer (bypassing
    ``reserve_voice_probe`` so the short-window check doesn't fire on
    call #11). Then call ``reserve_voice_probe`` once: the day-window
    cap (100) trips and we get a ``voice_probe_day`` error.

    The injected timestamps are old enough to be outside the short
    window but fresh enough to be inside the day window — concretely,
    we set them to ``now - short_window - 1`` so the short check
    counts zero but the day check counts 100.
    """
    import time
    import risk_control

    user = "user-day-cap"
    short_window = risk_control._VOICE_PROBE_SHORT_WINDOW
    now = time.monotonic()
    # Inject 100 timestamps just outside the short window. They count
    # toward the day cap but not the minute cap.
    fake_ts = now - short_window - 1.0
    buf = risk_control._voice_probe_buf[user]
    for _ in range(100):
        buf.append(fake_ts)
    # Sanity: short-window count is 0 (timestamps are too old), day
    # count is 100 (limit). Next reservation hits day cap.
    with pytest.raises(risk_control.RateLimitExceeded) as excinfo:
        risk_control.reserve_voice_probe(user)
    assert excinfo.value.scope == "voice_probe_day", (
        f"P2-23 regression: expected voice_probe_day, got "
        f"{excinfo.value.scope!r}. Day-window cap (100) is the daily "
        "spend ceiling — without it a user could spread 100+ probes "
        "over the day and bypass the short-window limit."
    )


# ---------------------------------------------------------------------------
# Concurrency: the bug Codex flagged
# ---------------------------------------------------------------------------


def test_concurrent_reservations_admit_exactly_limit():
    """**Codex review of 2a9c529 — the regression test.** 20 threads
    race on ``reserve_voice_probe`` with the same user_id. Under the
    v0 check/record split, all 20 would pass before any recorded
    (since record happened AFTER the paid call, which all 20 invoke
    in parallel). Under the v1 reserve/refund pattern, exactly 10
    must succeed and exactly 10 must raise RateLimitExceeded.
    """
    import risk_control

    user = "user-burst"
    n_threads = 20
    barrier = threading.Barrier(n_threads)
    successes: list[float] = []
    failures: list[str] = []
    result_lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            ts = risk_control.reserve_voice_probe(user)
            with result_lock:
                successes.append(ts)
        except risk_control.RateLimitExceeded as exc:
            with result_lock:
                failures.append(exc.scope)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    for t in threads:
        assert not t.is_alive(), (
            "P2-23 follow-up: reservation thread deadlocked under "
            "concurrent load. _voice_probe_lock should be a fast "
            "non-blocking critical section."
        )

    assert len(successes) == 10, (
        f"P2-23 follow-up regression: concurrent burst admitted "
        f"{len(successes)} reservations (expected exactly 10 = the "
        f"short-window limit). Codex review of 2a9c529 caught this "
        f"exact bypass — the check + append must be atomic under one "
        f"lock so a second concurrent reserver observes the first "
        f"one's slot."
    )
    assert len(failures) == n_threads - 10, (
        f"P2-23 follow-up regression: expected {n_threads - 10} "
        f"failures, got {len(failures)}."
    )
    assert all(scope == "voice_probe_short" for scope in failures), (
        f"P2-23 follow-up regression: failure scopes = {set(failures)}; "
        f"expected all 'voice_probe_short' for sub-day burst."
    )


def test_concurrent_reserve_with_refund_eventually_admits_more():
    """Concurrent reservers race; some succeed, some fail. The
    successful ones then refund. After all settle, fresh reservers
    should see capacity again. Proves refund participates in the
    same lock order.
    """
    import risk_control

    user = "user-burst-refund"
    barrier = threading.Barrier(15)
    timestamps: list[float] = []
    result_lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            ts = risk_control.reserve_voice_probe(user)
            with result_lock:
                timestamps.append(ts)
        except risk_control.RateLimitExceeded:
            pass

    threads = [threading.Thread(target=worker) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(timestamps) == 10  # Burst admitted exactly the limit.
    # All 10 winners refund their slots.
    for ts in timestamps:
        risk_control.refund_voice_probe(user, ts)
    # Buffer should now be empty — fresh reservation succeeds.
    new_ts = risk_control.reserve_voice_probe(user)
    assert new_ts > 0


# ---------------------------------------------------------------------------
# Endpoint AST guard: reserve BEFORE paid call, refund on every failure path
# ---------------------------------------------------------------------------


def test_endpoint_reserves_before_paid_call_and_refunds_on_failure():
    """Source-level guard: probe_user_voice must:
      1. Call ``risk_control.reserve_voice_probe`` BEFORE the
         ``asyncio.to_thread(synth_fn, ...)`` invocation.
      2. Call ``risk_control.refund_voice_probe`` on every error path
         that exits without consuming the paid provider call (bad
         input, unsupported provider, missing synth_fn) AND on the
         provider-failure path (synth raises / empty audio).

    Drift here re-opens either the line-rate spend vector (if reserve
    moves after the paid call) or the daily-quota-burns-on-flake
    vector (if refund is missing on failure paths).
    """
    import inspect

    import user_voice_api

    src = inspect.getsource(user_voice_api.probe_user_voice)
    reserve_pos = src.find("risk_control.reserve_voice_probe")
    # Match a less brittle marker — the to_thread call may be split
    # across lines, so we just look for the ``asyncio.to_thread(``
    # substring.
    synth_pos = src.find("asyncio.to_thread(")
    refund_count = src.count("risk_control.refund_voice_probe")

    assert reserve_pos != -1, (
        "P2-23 regression: probe_user_voice no longer calls "
        "risk_control.reserve_voice_probe. Without the atomic reserve, "
        "a concurrent burst bypasses the rate limit (Codex review of "
        "2a9c529 flagged exactly this)."
    )
    assert synth_pos != -1, (
        "test fixture stale: asyncio.to_thread(...) no longer in "
        "probe_user_voice — update the marker."
    )
    assert reserve_pos < synth_pos, (
        f"P2-23 follow-up regression: reserve_voice_probe runs AFTER "
        f"asyncio.to_thread(synth_fn ...). The reservation MUST land "
        f"before the paid call so concurrent reservers observe each "
        f"other's slot under the lock. Got reserve_pos={reserve_pos}, "
        f"synth_pos={synth_pos}."
    )
    # Refund must appear on multiple paths: bad input, unsupported
    # provider, missing synth_fn, synth exception, empty audio + a
    # defensive fallback in `except`. We expect at least 5 distinct
    # call sites — exact count may drift but should never drop below
    # the number of error returns.
    assert refund_count >= 5, (
        f"P2-23 follow-up regression: probe_user_voice has only "
        f"{refund_count} refund_voice_probe call sites. Each error "
        f"path that exits BEFORE consuming the paid call should "
        f"refund the reservation; otherwise bad-input rejections + "
        f"provider-flake failures consume the user's daily quota."
    )
