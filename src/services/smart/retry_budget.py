"""Smart MVP §6.3 — TTS time-fix retry budget tracker (PR#3A, business-logic only).

Pure-deterministic budget calculator. Given source_minutes + the current
retry consumption + the request being made, decide whether the retry is
allowed under the smart-only budget cap.

Plan §6.3 caps:
  - Per segment: max 2 re-TTS, max 2 in-segment rewrite
  - Per task total re-TTS audio duration: ``min(1.5 * source_minutes,
    source_minutes + 30 minutes)`` (long-video tightening per §9 +
    主方案 §9 plan)
  - Whole-task budget priority > per-segment quota — when whole-task
    remaining < average per-segment cost, refuse new requests; let
    in-flight retries finish but don't issue new ones

Budget exhaustion behaviour is NOT this module's concern — that's the
``smart_consent.on_budget_exhausted`` branch (degraded delivery vs
fail_and_refund), handled by the smart_state writer + settle dispatcher
(plan §6.3 末段 + §5.2). This module only says "this specific retry
request: yes/no, with reason and remaining budget".

Module is pure: no provider call, no I/O, no side effect. Caller (the
process.py integration in PR#3C) feeds it the current state from
UsageMeter + per-segment bookkeeping; this module returns a verdict.
Caller is responsible for actually executing or rejecting the retry.

Acceptance tests in tests/test_smart_business_logic.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# Per-segment caps (plan §6.3 table). Hard-coded — these are not
# admin knobs. Changing them is a Smart MVP behaviour change requiring
# plan update + owner sign-off.
PER_SEGMENT_RETTS_CAP = 2
PER_SEGMENT_REWRITE_CAP = 2

# Whole-task formula multipliers. plan §6.3 末段:
#   total_budget_minutes = min(1.5 * source_minutes, source_minutes + 30)
# Same shape as 主方案 §9 long-video tightening. The min(...) keeps the
# linear-1.5x policy for short videos and the +30 cap for long ones.
_BUDGET_MULTIPLIER = 1.5
_BUDGET_LONG_VIDEO_OFFSET_MINUTES = 30.0


class RetryKind(Enum):
    """The two retry actions Smart's retry loop can request."""

    RETTS = "retts"  # Re-TTS the same segment text on the same voice
    REWRITE = "rewrite"  # In-segment rewrite (shorter text), then re-TTS


@dataclass(frozen=True)
class BudgetSnapshot:
    """Caller-supplied current state.

    The caller (process.py integration) maintains a per-task tally of
    retry actions taken so far + the average per-retry audio duration
    observed; this module reads them as input and returns a verdict.
    """

    source_minutes: float
    consumed_retts_audio_seconds: float
    per_segment_retts_taken: int  # for the segment in this request
    per_segment_rewrite_taken: int  # for the segment in this request
    avg_per_retts_audio_seconds: float  # rolling avg, for "remaining < avg cost" check


@dataclass(frozen=True)
class BudgetDecision:
    """Allow / refuse + audit-grade rationale."""

    allowed: bool
    kind: RetryKind
    reason: str  # always populated — "approved" / specific refusal reason
    total_budget_seconds: float
    remaining_seconds: float
    per_segment_taken: int
    per_segment_cap: int


def compute_total_budget_minutes(source_minutes: float) -> float:
    """Plan §6.3 末段 + 主方案 §9 long-video tightening.

    Examples:
      source=10  → min(15.0, 40.0) = 15.0
      source=30  → min(45.0, 60.0) = 45.0
      source=60  → min(90.0, 90.0) = 90.0
      source=120 → min(180.0, 150.0) = 150.0  ← long-video cap kicks in
    """
    if source_minutes <= 0:
        return 0.0
    return min(
        _BUDGET_MULTIPLIER * source_minutes,
        source_minutes + _BUDGET_LONG_VIDEO_OFFSET_MINUTES,
    )


def evaluate_retry_request(
    snapshot: BudgetSnapshot,
    *,
    kind: RetryKind,
) -> BudgetDecision:
    """Verdict on a single retry request.

    Decision order (plan §6.3 末段 — whole-task budget priority):
      1. Per-segment cap exceeded for this kind → refuse
      2. Whole-task budget already exhausted → refuse
      3. Whole-task remaining < avg per-retry cost → refuse
         ("in-flight retries can finish, but no new applications")
      4. Otherwise → approve

    The avg-per-retry check at step 3 is the conservative gate: prevents
    an early-noisy segment from eating budget that later segments need.

    Note: per-segment caps are checked AGAINST the current taken count
    (not "after this request would push it over") because the snapshot
    represents state BEFORE this request; we return whether the request
    can be admitted, not whether the result is still under cap. So
    snapshot.per_segment_retts_taken==2 with cap==2 → refuse (this would
    be the 3rd, over).
    """
    total_budget_seconds = compute_total_budget_minutes(snapshot.source_minutes) * 60.0
    remaining = max(0.0, total_budget_seconds - snapshot.consumed_retts_audio_seconds)

    if kind is RetryKind.RETTS:
        cap = PER_SEGMENT_RETTS_CAP
        taken = snapshot.per_segment_retts_taken
    elif kind is RetryKind.REWRITE:
        cap = PER_SEGMENT_REWRITE_CAP
        taken = snapshot.per_segment_rewrite_taken
    else:  # pragma: no cover — Enum exhaustive
        raise ValueError(f"unknown retry kind: {kind!r}")

    # 1. Per-segment cap.
    if taken >= cap:
        return BudgetDecision(
            allowed=False,
            kind=kind,
            reason=f"per_segment_{kind.value}_cap_exhausted_{taken}_of_{cap}",
            total_budget_seconds=total_budget_seconds,
            remaining_seconds=remaining,
            per_segment_taken=taken,
            per_segment_cap=cap,
        )

    # 2. Whole-task budget exhausted entirely.
    if remaining <= 0:
        return BudgetDecision(
            allowed=False,
            kind=kind,
            reason="whole_task_budget_exhausted",
            total_budget_seconds=total_budget_seconds,
            remaining_seconds=0.0,
            per_segment_taken=taken,
            per_segment_cap=cap,
        )

    # 3. Whole-task remaining < avg per-retry cost — conservative refuse
    # so a runaway early segment doesn't starve later ones. Skipped when
    # avg cost not yet known (first request of the task).
    if (
        snapshot.avg_per_retts_audio_seconds > 0
        and remaining < snapshot.avg_per_retts_audio_seconds
    ):
        return BudgetDecision(
            allowed=False,
            kind=kind,
            reason=(
                f"whole_task_remaining_below_avg_cost_"
                f"{remaining:.1f}s_vs_{snapshot.avg_per_retts_audio_seconds:.1f}s"
            ),
            total_budget_seconds=total_budget_seconds,
            remaining_seconds=remaining,
            per_segment_taken=taken,
            per_segment_cap=cap,
        )

    return BudgetDecision(
        allowed=True,
        kind=kind,
        reason="approved",
        total_budget_seconds=total_budget_seconds,
        remaining_seconds=remaining,
        per_segment_taken=taken,
        per_segment_cap=cap,
    )


__all__ = [
    "BudgetDecision",
    "BudgetSnapshot",
    "PER_SEGMENT_RETTS_CAP",
    "PER_SEGMENT_REWRITE_CAP",
    "RetryKind",
    "compute_total_budget_minutes",
    "evaluate_retry_request",
]
