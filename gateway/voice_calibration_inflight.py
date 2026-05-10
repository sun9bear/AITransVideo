"""In-flight dedup for voice CPS calibration.

Atomic claim_or_join so concurrent callers (T1 clone-after, T2 review
preflight, manual /calibrate-speed endpoint, T3 admin batch) never
issue duplicate paid TTS calls for the same calibration target.

Key design (P2 voice CPS auto-calibration plan v4.3 T0-B, codex v3 F1+F3
+ v4 F-v4-3 + v4.1 F-v4.1-4 + v4.1 F-v4.1-6):

The identity is a 5-tuple ``(scope, owner, provider, voice_id, model_key)``
because narrower keys cross-pollinate results:

  - ``voice_id`` alone:  same provider voice id can belong to different
    users in ``user_voices`` (no DB unique constraint on voice_id alone),
    and the same voice_id between user_voices and voice_catalog has
    different storage owners.
  - ``(provider, voice_id)``: same voice id under MiniMax
    ``speech-2.8-turbo`` vs ``speech-2.8-hd`` has materially different
    CPS — sharing a future would poison one model with the other's data.
  - ``scope``: keeps user_voices writes vs voice_catalog writes from
    racing each other.

Atomic claim semantics (codex v4 F-v4-3): the v3 "peek → reserve →
get_or_start" 3-step flow had a race where two concurrent callers could
both peek-miss, both reserve, then only the second find an existing
future — the joiner had already wasted a budget reservation. v4 fixes
this by deciding starter/joiner inside ONE lock acquisition.

Joiner shield (codex v4.1 F-v4.1-4): the v4 design called for joiners
to ``await future``, but a bare await propagates joiner cancellation
into the shared future, breaking starter and any other joiners. v4.1
requires joiners to ``await asyncio.shield(future)`` so a cancelled
joiner doesn't take down the whole calibration.

Identity-checked release (codex v4.1 F-v4.1-6): release(key) without
a future identity could erroneously delete a successor starter's
freshly-registered future for the same key, breaking dedup for new
joiners. v4.1 takes ``release(key, future)`` and only pops if the
registry still holds THIS future for THIS key.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class CalibrationKey:
    """Full identity of a calibration request. All five fields required.

    See module docstring for why narrower keys cross-pollinate. Frozen +
    slots so instances are hashable and cheap (used as dict key in the
    registry).
    """

    scope: Literal["user", "catalog"]
    """Which storage table we'll write to."""

    owner: str
    """user_id (uuid string) when scope='user'; literal 'catalog' when scope='catalog'."""

    provider: str
    """Canonical lowercase: 'minimax' | 'cosyvoice' | 'volcengine'."""

    voice_id: str
    """Provider-side voice id (UserVoice.voice_id, NOT UserVoice.id which is UUID PK)."""

    model_key: str
    """Canonical model id, e.g. 'speech-2.8-turbo', 'speech-2.8-hd'."""


class CalibrationInFlightRegistry:
    """Atomic claim_or_join for concurrent calibration requests.

    Usage (canonical caller pattern, see plan §3.0 T0-B):

        future, role = await registry.claim_or_join(key)

        if role == "joiner":
            # Shield: a cancelled joiner must not propagate cancellation
            # into the shared future (codex v4.1 F-v4.1-4).
            return await asyncio.shield(future)

        # starter: reserve budget AFTER claim succeeds (codex v4 F-v4-3).
        try:
            reservation = risk_control.reserve_voice_calibration(user_id)
        except RateLimitExceeded as exc:
            if not future.done():
                future.set_exception(exc)
            await registry.release(key, future)
            raise

        result: CalibrationResult | None = None
        try:
            result = await factory()  # ALWAYS returns CalibrationResult, never raises
        finally:
            if result is None:
                # Defensive: factory contract violated. Treat as no-paid-call.
                risk_control.refund_voice_calibration(user_id, reservation)
            elif not result.ok and result.paid_call_count == 0:
                risk_control.refund_voice_calibration(user_id, reservation)
            # else: paid_call_count > 0 means budget correctly spent — DO NOT refund.

            # Defensive set_result: future may have been cancelled out from
            # under us (e.g. process shutdown). set_result on a cancelled
            # future raises InvalidStateError; check first.
            if not future.done():
                future.set_result(result if result is not None else _internal_error_result(key))
            await registry.release(key, future)

        return result
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._futures: dict[CalibrationKey, asyncio.Future] = {}

    async def claim_or_join(
        self, key: CalibrationKey
    ) -> tuple[asyncio.Future, Literal["starter", "joiner"]]:
        """Atomically check + claim. Returns the SAME future object for
        the same key while it's in-flight, distinguishing starter vs joiner
        in a single lock acquisition.

        starter contract:
          - MUST eventually call ``set_result`` or ``set_exception`` on the
            returned future, AND call ``release(key, future)``, OR the
            registry leaks.
          - MUST be the only path that reserves budget; joiners get the
            in-flight result without consuming budget.

        joiner contract:
          - Just await the returned future (preferably wrapped in
            ``asyncio.shield`` so this caller's cancellation doesn't kill
            the shared work).
          - MUST NOT reserve budget; MUST NOT call release.
        """
        async with self._lock:
            existing = self._futures.get(key)
            if existing is not None and not existing.done():
                return existing, "joiner"
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._futures[key] = future
            return future, "starter"

    async def release(
        self, key: CalibrationKey, future: asyncio.Future
    ) -> None:
        """Identity-checked release.

        Only pops the registry entry if it still holds THIS future for
        THIS key. Without identity check, an aborted starter's release
        could delete a successor starter's freshly-registered future for
        the same key, breaking dedup for new joiners.
        """
        async with self._lock:
            existing = self._futures.get(key)
            if existing is future:
                self._futures.pop(key, None)

    def reset_for_tests(self) -> None:
        """Test-support: drop all in-flight futures.

        DO NOT call from production code — leaks any in-flight tasks.
        Use only between test cases to avoid carryover state.
        """
        # No async lock here because tests run between event loops; if a
        # test leaves an active future, that's the test's bug to surface.
        self._futures.clear()


# Module-level singleton — all callers (T1 clone-after, T2 review preflight,
# manual endpoint, T3 admin batch) share this. Tests should call
# ``registry.reset_for_tests()`` between cases.
registry = CalibrationInFlightRegistry()


def _internal_error_result(key: CalibrationKey):
    """Build a defensive CalibrationResult for the contract-violation case
    where factory returned None instead of a real result.

    Imported lazily to avoid circular imports between voice_calibration_inflight
    and voice_speed_calibrator.
    """
    from voice_speed_calibrator import CalibrationResult

    return CalibrationResult(
        ok=False,
        cps=0.0,
        per_text=[],
        error="factory returned None (contract violation)",
        error_class="internal_error",
        paid_call_count=0,
        model_key=key.model_key,
    )


# ---------------------------------------------------------------------------
# Reusable caller helper
# ---------------------------------------------------------------------------
#
# All four entry points (manual /calibrate-speed endpoint, T1 clone-after hook,
# T2 review preflight, T3 admin batch) follow the same dance:
#   1. claim_or_join the registry with the 5-tuple key
#   2. starter only: reserve budget; joiner shielded-await the future
#   3. starter only: run the factory (returns CalibrationResult, never raises)
#   4. starter only: refund budget IFF paid_call_count == 0
#   5. starter only: set future result + identity-checked release
#
# Centralizing this avoids 4 copies that drift apart over time. Plan v4.3
# T0 contract.

import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


async def run_calibration_task(
    *,
    key: CalibrationKey,
    user_id_for_budget: str,
    factory: Callable[[], Awaitable["CalibrationResult"]],  # noqa: F821 — forward ref
) -> "CalibrationResult":  # noqa: F821
    """Execute a calibration task with full v4.1 caller-pattern semantics.

    Wraps:
    - claim_or_join (atomic starter/joiner decision)
    - joiner shielded await
    - starter budget reserve (only after claim succeeds)
    - factory invocation (must always return CalibrationResult, never raise)
    - paid_call_count-based refund (only when count == 0)
    - identity-checked release(key, future)
    - defensive future.done() guards on cancellation paths

    Parameters
    ----------
    key:
        CalibrationKey 5-tuple identifying the calibration target.
    user_id_for_budget:
        The user account whose calibration budget should be charged.
        For scope='user', this is normally key.owner. For scope='catalog'
        admin batch, this is the admin's user_id (or the system sentinel
        if running via internal API key — caller decides). Empty string
        is a defensive no-op (no budget reservation).
    factory:
        Async callable that performs the actual calibration work and
        returns a CalibrationResult. MUST NOT raise — wrap exceptions
        into CalibrationResult(ok=False, error_class=..., paid_call_count=N).
        See voice_speed_calibrator.calibrate_voice for the contract.

    Returns
    -------
    CalibrationResult
        Either the starter's fresh result OR (for joiner role) the same
        result that the in-flight starter eventually produced.

    Raises
    ------
    risk_control.RateLimitExceeded
        Only on starter path when budget is exhausted. Joiners always
        return a CalibrationResult (success or failure) — they don't
        observe rate-limit because they didn't reserve.
    """
    import risk_control  # local import to avoid circular reference at module load

    future, role = await registry.claim_or_join(key)

    if role == "joiner":
        # v4.1 codex F-v4.1-4: shield prevents cancellation from
        # propagating into the shared future.
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Joiner was cancelled. The starter (and any other joiners)
            # keep going. Surface this as an internal_error to the caller
            # so they don't double-count budget.
            logger.info("[calibration-inflight] joiner cancelled key=%s", key)
            return _internal_error_result(key)

    # Starter path: reserve budget AFTER claim succeeded.
    reservation: float = 0.0
    if user_id_for_budget:
        try:
            reservation = risk_control.reserve_voice_calibration(user_id_for_budget)
        except risk_control.RateLimitExceeded as exc:
            # Notify joiners (if any racing) that this attempt cannot proceed.
            # Then release so a future caller can re-claim and try again.
            if not future.done():
                future.set_exception(exc)
            await registry.release(key, future)
            raise

    # codex T0-review F-T0-1 (cancel race fix): spawn factory as a
    # background task and finalize via done_callback. If the caller is
    # cancelled (HTTP client disconnect, asyncio.gather sibling failure,
    # asyncio.wait_for timeout), the shielded await raises CancelledError
    # to the caller — but factory_task keeps running in the background,
    # the paid TTS thread inside `asyncio.to_thread(calibrate_voice)`
    # finishes its work, the done_callback fires with the real
    # paid_call_count, and refund/release decisions reflect what actually
    # happened.
    #
    # Without this wrapping, the v4.1 design path was: caller cancelled
    # → finally fires immediately with result=None → refund + release
    # → in-flight registry empty → next request claims as fresh starter
    # → second paid TTS call spawned for the same key. Single cancel
    # could trigger N paid calls.
    factory_task: asyncio.Task = asyncio.create_task(factory())

    def _finalize(task: asyncio.Task) -> None:
        """Runs synchronously on the event loop when factory_task
        finishes (success / failure / cancellation we did NOT request).

        We intentionally DO NOT cancel factory_task from the caller's
        cancel path — once the paid TTS has been issued (or is about to
        be), we want to know the true paid_call_count before refunding.
        """
        result: "CalibrationResult | None" = None  # noqa: F821
        try:
            if task.cancelled():
                # We never cancel factory_task ourselves. Treat any
                # observed cancellation as a contract violation.
                logger.error(
                    "[calibration-inflight] factory_task unexpectedly cancelled key=%s", key,
                )
                result = _internal_error_result(key)
            else:
                exc = task.exception()
                if exc is not None:
                    # Factory contract: never raises. If it does, treat
                    # as internal_error / paid_call_count==0 so refund
                    # fires (the safer default — provider-side state is
                    # unknown).
                    logger.exception(
                        "[calibration-inflight] factory raised key=%s",
                        key, exc_info=exc,
                    )
                    result = _internal_error_result(key)
                else:
                    result = task.result()
                    if result is None:
                        logger.error(
                            "[calibration-inflight] factory returned None for key=%s (contract violation)",
                            key,
                        )
                        result = _internal_error_result(key)

            # v4 codex F-v4-4: refund only when paid_call_count == 0.
            should_refund = (
                user_id_for_budget
                and reservation > 0
                and (not result.ok and result.paid_call_count == 0)
            )
            if should_refund:
                risk_control.refund_voice_calibration(user_id_for_budget, reservation)

            # v4.1 codex F-v4.1-4: defensive — future may have been
            # cancelled by some other path (test cleanup, etc.).
            if not future.done():
                future.set_result(result)
        except Exception:
            # done_callback must not raise — Python would log + drop it.
            # Log loudly and move on; release still attempted below.
            logger.exception("[calibration-inflight] _finalize raised key=%s", key)

        # v4.1 codex F-v4.1-6: identity-checked release. release() is
        # async; schedule it as a task since done_callback is sync.
        # The task is fire-and-forget — if event loop is shutting down
        # the registry leaks one entry, which is acceptable (no live
        # callers reference it anymore).
        try:
            asyncio.create_task(registry.release(key, future))
        except RuntimeError:
            # No running event loop (test teardown / shutdown); the
            # registry will be GC'd anyway.
            pass

    factory_task.add_done_callback(_finalize)

    # codex T0-review F-T0-4 (round 8): starter awaits the SHARED future,
    # NOT factory_task directly.
    #
    # Pre-fix: `await asyncio.shield(factory_task)` returns whatever the
    # task returned/raised. If the factory broke its "always returns
    # CalibrationResult" contract and raised, the starter would receive
    # the raw exception while joiners (awaiting `future`) would see the
    # _finalize-normalized `CalibrationResult(internal_error)`. This
    # broke the run_calibration_task helper contract and would crash
    # the manual endpoint's asyncio.gather with a 500.
    #
    # Post-fix: starter and joiner both `await asyncio.shield(future)`.
    # The factory task is purely a background work unit; _finalize is
    # the single source of truth that writes the normalized result into
    # the future. Caller cancellation still propagates outward via shield
    # (factory_task keeps running in background; finalize still fires
    # when it completes).
    return await asyncio.shield(future)
