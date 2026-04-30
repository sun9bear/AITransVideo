"""Shadow credits service — V3-1 parallel ledger.

This module manages CreditsBucket and CreditsLedger in **shadow mode**:
all writes are best-effort and failures are logged but never block the V2
production path (quota, billing, entitlements).

Operations:
- grant():    create a bucket and initial ledger entry
- reserve():  pre-deduct estimated credits from the highest-priority bucket
- capture():  finalize actual credits (adjust delta vs. estimate)
- release():  refund reserved credits (job failed / cancelled)
- rollback(): reverse a grant (e.g. subscription refund)

Consumption priority is mode-dependent:
  Express:  free → subscription → topup → trial
  Studio:   trial → subscription → topup → free

Design principles:
- Gateway is the truth source; frontend only reads.
- Every credit movement produces an immutable ledger entry.
- Bucket selection logic is centralized here, not scattered.
- Shadow failures are caught and logged, never re-raised.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import CreditsBucket, CreditsLedger

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost model constants (frozen V3 parameters — backward compat snapshots)
# ---------------------------------------------------------------------------

# Points per source minute by (service_mode, quality_tier)
DEBIT_RATES: dict[tuple[str, str], int] = {
    ("express", "standard"): 10,
    ("studio", "standard"): 15,
    ("studio", "high"): 30,
    ("studio", "flagship"): 50,
}

DEFAULT_DEBIT_RATE = 10  # fallback

# Bucket consumption priority per service mode
BUCKET_PRIORITY: dict[str, list[str]] = {
    "express": ["free", "subscription", "topup", "trial"],
    "studio": ["trial", "subscription", "topup", "free"],
}

# Bucket types that are valid
VALID_BUCKET_TYPES = {"free", "trial", "subscription", "topup", "manual_adjustment"}

# Frozen V3 credit grant amounts (from V3 plan doc)
GRANT_AMOUNTS: dict[str, int] = {
    "free": 500,
    "trial": 300,
    "plus": 3500,
    "pro": 12000,
}


# ---------------------------------------------------------------------------
# Runtime pricing accessors — derive from pricing_runtime, fallback to frozen
# ---------------------------------------------------------------------------


def _get_runtime_debit_rates() -> dict[tuple[str, str], int]:
    """Derive debit rates from runtime pricing, fallback to frozen constants."""
    try:
        from pricing_runtime import get_runtime_pricing
        credits = get_runtime_pricing().credits
        result: dict[tuple[str, str], int] = {}
        for key, value in credits.debit_rates.items():
            parts = key.split(".", 1)
            if len(parts) == 2:
                result[(parts[0], parts[1])] = value
        return result if result else DEBIT_RATES
    except Exception:
        return DEBIT_RATES


def _get_runtime_grant_amounts() -> dict[str, int]:
    """Derive grant amounts from runtime pricing, fallback to frozen constants."""
    try:
        from pricing_runtime import get_runtime_pricing
        payload = get_runtime_pricing()
        result: dict[str, int] = {"free": payload.credits.free_grant_credits}
        result["trial"] = payload.trial.grant_credits
        for code, plan in payload.plans.items():
            if plan.monthly_grant_credits is not None:
                result[code] = plan.monthly_grant_credits
        return result if result else GRANT_AMOUNTS
    except Exception:
        return GRANT_AMOUNTS


def _get_runtime_bucket_priority() -> dict[str, list[str]]:
    """Derive bucket priority from runtime pricing, fallback to frozen constants."""
    try:
        from pricing_runtime import get_runtime_pricing
        return get_runtime_pricing().credits.bucket_priority
    except Exception:
        return BUCKET_PRIORITY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def estimate_credits(
    estimated_minutes: float | None,
    service_mode: str = "express",
    quality_tier: str = "standard",
) -> int:
    """Calculate estimated credit cost for a job.

    Returns 0 if estimated_minutes is None (duration unknown at creation time).
    """
    if estimated_minutes is None or estimated_minutes <= 0:
        return 0
    rates = _get_runtime_debit_rates()
    rate = rates.get((service_mode, quality_tier), DEFAULT_DEBIT_RATE)
    return max(1, round(estimated_minutes * rate))


async def get_user_buckets(
    db: AsyncSession,
    user_id,
    *,
    include_expired: bool = False,
    for_update: bool = False,
) -> list[CreditsBucket]:
    """Return all active buckets for a user, ordered by created_at.

    for_update=True: acquire row-level locks via SELECT FOR UPDATE so concurrent
    writers serialize at the DB layer. Callers that mutate bucket.reserved or
    bucket.remaining (shadow_reserve, shadow_capture's additional-debit branch)
    MUST pass for_update=True; otherwise two concurrent jobs can read the same
    bucket state and both commit — losing one write.

    Caller must be inside a transaction. No-op on SQLite (no row locking).
    """
    now = datetime.now(timezone.utc)
    stmt = select(CreditsBucket).where(
        CreditsBucket.user_id == user_id,
    ).order_by(CreditsBucket.created_at)
    if for_update:
        stmt = stmt.with_for_update()

    result = await db.execute(stmt)
    buckets = list(result.scalars().all())

    if not include_expired:
        buckets = [
            b for b in buckets
            if b.expires_at is None or b.expires_at > now
        ]

    return buckets


def _pick_buckets_by_priority(
    buckets: list[CreditsBucket],
    service_mode: str,
) -> list[CreditsBucket]:
    """Sort buckets by consumption priority for the given service mode."""
    bp = _get_runtime_bucket_priority()
    priority_order = bp.get(service_mode, bp.get("express", BUCKET_PRIORITY["express"]))
    type_rank = {t: i for i, t in enumerate(priority_order)}
    return sorted(buckets, key=lambda b: type_rank.get(b.bucket_type, 99))


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


async def shadow_grant(
    db: AsyncSession,
    *,
    user_id,
    bucket_type: str,
    amount: int,
    source_label: str | None = None,
    expires_at: datetime | None = None,
    related_order_id=None,
    related_subscription_id=None,
    reason_code: str = "grant",
) -> CreditsBucket | None:
    """Create a new credits bucket with an initial grant.

    Shadow mode: failures are logged and None is returned.
    """
    if bucket_type not in VALID_BUCKET_TYPES:
        logger.warning("shadow_grant: invalid bucket_type=%s", bucket_type)
        return None

    try:
        bucket = CreditsBucket(
            user_id=user_id,
            bucket_type=bucket_type,
            granted=amount,
            remaining=amount,
            reserved=0,
            expires_at=expires_at,
            source_label=source_label,
            related_order_id=related_order_id,
            related_subscription_id=related_subscription_id,
        )
        db.add(bucket)
        await db.flush()

        entry = CreditsLedger(
            user_id=user_id,
            bucket_id=bucket.id,
            direction="grant",
            credits_delta=amount,
            balance_after=amount,
            related_order_id=related_order_id,
            related_subscription_id=related_subscription_id,
            reason_code=reason_code,
        )
        db.add(entry)

        logger.info(
            "shadow_grant: user=%s type=%s amount=%d bucket=%s",
            user_id, bucket_type, amount, bucket.id,
        )
        return bucket
    except Exception:
        logger.exception("shadow_grant failed: user=%s type=%s", user_id, bucket_type)
        return None


async def shadow_reserve(
    db: AsyncSession,
    *,
    user_id,
    job_id: str,
    estimated_credits: int,
    service_mode: str = "express",
    reason_code: str = "job_reserve",
) -> list[CreditsLedger]:
    """Reserve estimated credits across buckets (highest-priority first).

    Returns the ledger entries created. Empty list on failure or insufficient balance.
    Shadow mode: failures are logged, empty list returned.
    """
    if estimated_credits <= 0:
        return []

    try:
        # T1: acquire row locks on every bucket — concurrent reserves for the
        # same user otherwise double-claim (two txns both read available=N,
        # both commit bucket.reserved+=take, second write overwrites the first).
        buckets = await get_user_buckets(db, user_id, for_update=True)
        ordered = _pick_buckets_by_priority(buckets, service_mode)

        remaining_to_reserve = estimated_credits
        entries: list[CreditsLedger] = []

        for bucket in ordered:
            if remaining_to_reserve <= 0:
                break
            available = bucket.remaining - bucket.reserved
            if available <= 0:
                continue

            take = min(available, remaining_to_reserve)
            bucket.reserved += take
            remaining_to_reserve -= take

            entry = CreditsLedger(
                user_id=user_id,
                bucket_id=bucket.id,
                direction="reserve",
                credits_delta=-take,
                balance_after=bucket.remaining - bucket.reserved,
                related_job_id=job_id,
                reason_code=reason_code,
            )
            db.add(entry)
            entries.append(entry)

        if remaining_to_reserve > 0:
            # Insufficient balance — roll back in-memory changes
            for bucket in ordered:
                # The changes haven't been flushed, but let's be explicit
                pass
            logger.info(
                "shadow_reserve: insufficient credits for user=%s job=%s "
                "needed=%d shortfall=%d",
                user_id, job_id, estimated_credits, remaining_to_reserve,
            )
            # Still record what we could reserve (shadow mode — no gating)

        logger.info(
            "shadow_reserve: user=%s job=%s estimated=%d reserved=%d entries=%d",
            user_id, job_id, estimated_credits,
            estimated_credits - remaining_to_reserve, len(entries),
        )
        return entries
    except Exception:
        logger.exception("shadow_reserve failed: user=%s job=%s", user_id, job_id)
        return []


async def shadow_capture(
    db: AsyncSession,
    *,
    user_id,
    job_id: str,
    actual_credits: int,
    service_mode: str = "express",
    reason_code: str = "job_capture",
    reserve_reason_code: str | None = None,
) -> list[CreditsLedger]:
    """Finalize actual credits for a completed job.

    Invariant: every reserve entry for this job MUST be fully settled into
    either a capture entry, a release entry, or a split of both. No dangling
    ``bucket.reserved`` is allowed after this function returns.

    Algorithm (actual <= reserved):
      1. Compute per-entry release allocation in reverse priority order
         (last-reserved bucket absorbs excess first).
      2. Iterate ALL reserve entries: for each, write capture for consumed
         portion + release for refunded portion. Clear ``bucket.reserved``.

    Algorithm (actual > reserved):
      1. Capture all reserved entries fully.
      2. Debit additional from available buckets by priority.

    Shadow mode: failures are logged, empty list returned.
    """
    if actual_credits < 0:
        return []

    try:
        # Find all reserve entries for this job
        reserve_stmt = select(CreditsLedger).where(
            CreditsLedger.user_id == user_id,
            CreditsLedger.related_job_id == job_id,
            CreditsLedger.direction == "reserve",
        )
        if reserve_reason_code is not None:
            reserve_stmt = reserve_stmt.where(CreditsLedger.reason_code == reserve_reason_code)
        result = await db.execute(reserve_stmt)
        reserve_entries = list(result.scalars().all())

        total_reserved = sum(abs(e.credits_delta) for e in reserve_entries)
        entries: list[CreditsLedger] = []

        if actual_credits <= total_reserved:
            # --- Phase 1: compute per-entry release allocation ---
            # Release excess from last-reserved entries first (reverse priority).
            excess = total_reserved - actual_credits
            # Map: reserve_entry index → amount to release from that entry
            release_map: dict[int, int] = {}
            for idx in range(len(reserve_entries) - 1, -1, -1):
                if excess <= 0:
                    break
                re = reserve_entries[idx]
                give_back = min(excess, abs(re.credits_delta))
                release_map[idx] = give_back
                excess -= give_back

            # --- Phase 2: settle EVERY reserve entry (no dangling) ---
            for idx, re in enumerate(reserve_entries):
                bucket_result = await db.execute(
                    select(CreditsBucket)
                    .where(CreditsBucket.id == re.bucket_id)
                    .with_for_update()
                )
                bucket = bucket_result.scalar_one_or_none()
                if bucket is None:
                    continue

                entry_reserved = abs(re.credits_delta)
                release_amount = release_map.get(idx, 0)
                consumed = entry_reserved - release_amount

                # Clear this entry's reservation from bucket
                bucket.reserved -= entry_reserved
                # Deduct consumed portion from remaining balance
                bucket.remaining -= consumed

                if consumed > 0:
                    entry = CreditsLedger(
                        user_id=user_id,
                        bucket_id=bucket.id,
                        direction="capture",
                        credits_delta=-consumed,
                        balance_after=bucket.remaining,
                        related_job_id=job_id,
                        reason_code=reason_code,
                    )
                    db.add(entry)
                    entries.append(entry)

                if release_amount > 0:
                    release_entry = CreditsLedger(
                        user_id=user_id,
                        bucket_id=bucket.id,
                        direction="release",
                        credits_delta=release_amount,
                        balance_after=bucket.remaining,
                        related_job_id=job_id,
                        reason_code="capture_excess_release",
                    )
                    db.add(release_entry)
                    entries.append(release_entry)
        else:
            # actual > reserved — capture all reserved, then debit additional
            for re in reserve_entries:
                bucket_result = await db.execute(
                    select(CreditsBucket)
                    .where(CreditsBucket.id == re.bucket_id)
                    .with_for_update()
                )
                bucket = bucket_result.scalar_one_or_none()
                if bucket is None:
                    continue

                consumed = abs(re.credits_delta)
                bucket.reserved -= consumed
                bucket.remaining -= consumed

                entry = CreditsLedger(
                    user_id=user_id,
                    bucket_id=bucket.id,
                    direction="capture",
                    credits_delta=-consumed,
                    balance_after=bucket.remaining,
                    related_job_id=job_id,
                    reason_code=reason_code,
                )
                db.add(entry)
                entries.append(entry)

            # Additional debit for the excess
            additional = actual_credits - total_reserved
            last_bucket: CreditsBucket | None = None
            if additional > 0:
                buckets = await get_user_buckets(db, user_id, for_update=True)
                ordered = _pick_buckets_by_priority(buckets, service_mode)
                for bucket in ordered:
                    last_bucket = bucket
                    if additional <= 0:
                        break
                    available = bucket.remaining - bucket.reserved
                    if available <= 0:
                        continue
                    take = min(available, additional)
                    bucket.remaining -= take
                    additional -= take

                    entry = CreditsLedger(
                        user_id=user_id,
                        bucket_id=bucket.id,
                        direction="capture",
                        credits_delta=-take,
                        balance_after=bucket.remaining,
                        related_job_id=job_id,
                        reason_code="capture_additional",
                    )
                    db.add(entry)
                    entries.append(entry)

                # Shadow credits are an accounting surface while legacy quota
                # gates may still allow a job or clone to finish beyond the
                # currently available bucket balance. Keep actual consumption
                # visible instead of silently under-reporting it.
                if additional > 0 and last_bucket is not None:
                    last_bucket.remaining -= additional
                    entry = CreditsLedger(
                        user_id=user_id,
                        bucket_id=last_bucket.id,
                        direction="capture",
                        credits_delta=-additional,
                        balance_after=last_bucket.remaining,
                        related_job_id=job_id,
                        reason_code="capture_overdraft",
                    )
                    db.add(entry)
                    entries.append(entry)

        logger.info(
            "shadow_capture: user=%s job=%s actual=%d reserved=%d entries=%d",
            user_id, job_id, actual_credits, total_reserved, len(entries),
        )
        return entries
    except Exception:
        logger.exception("shadow_capture failed: user=%s job=%s", user_id, job_id)
        return []


async def shadow_release(
    db: AsyncSession,
    *,
    user_id,
    job_id: str,
    reason_code: str = "job_release",
    reserve_reason_code: str | None = None,
) -> list[CreditsLedger]:
    """Release all reserved credits for a job (failed / cancelled).

    Shadow mode: failures are logged, empty list returned.
    """
    try:
        reserve_stmt = select(CreditsLedger).where(
            CreditsLedger.user_id == user_id,
            CreditsLedger.related_job_id == job_id,
            CreditsLedger.direction == "reserve",
        )
        if reserve_reason_code is not None:
            reserve_stmt = reserve_stmt.where(CreditsLedger.reason_code == reserve_reason_code)
        result = await db.execute(reserve_stmt)
        reserve_entries = list(result.scalars().all())

        entries: list[CreditsLedger] = []
        for re in reserve_entries:
            bucket_result = await db.execute(
                select(CreditsBucket)
                .where(CreditsBucket.id == re.bucket_id)
                .with_for_update()
            )
            bucket = bucket_result.scalar_one_or_none()
            if bucket is None:
                continue

            release_amount = abs(re.credits_delta)
            bucket.reserved -= release_amount

            entry = CreditsLedger(
                user_id=user_id,
                bucket_id=bucket.id,
                direction="release",
                credits_delta=release_amount,
                balance_after=bucket.remaining - bucket.reserved,
                related_job_id=job_id,
                reason_code=reason_code,
            )
            db.add(entry)
            entries.append(entry)

        logger.info(
            "shadow_release: user=%s job=%s entries=%d",
            user_id, job_id, len(entries),
        )
        return entries
    except Exception:
        logger.exception("shadow_release failed: user=%s job=%s", user_id, job_id)
        return []


async def shadow_rollback(
    db: AsyncSession,
    *,
    user_id,
    bucket_id,
    reason_code: str = "refund_rollback",
    related_order_id=None,
) -> CreditsLedger | None:
    """Rollback a bucket grant (e.g. subscription refund).

    Sets remaining to 0 and records a rollback ledger entry.
    Shadow mode: failures are logged, None returned.
    """
    try:
        result = await db.execute(
            select(CreditsBucket)
            .where(
                CreditsBucket.id == bucket_id,
                CreditsBucket.user_id == user_id,
            )
            .with_for_update()
        )
        bucket = result.scalar_one_or_none()
        if bucket is None:
            logger.warning("shadow_rollback: bucket %s not found for user %s", bucket_id, user_id)
            return None

        rollback_amount = bucket.remaining
        bucket.remaining = 0
        bucket.reserved = 0

        entry = CreditsLedger(
            user_id=user_id,
            bucket_id=bucket.id,
            direction="rollback",
            credits_delta=-rollback_amount,
            balance_after=0,
            related_order_id=related_order_id,
            reason_code=reason_code,
        )
        db.add(entry)

        logger.info(
            "shadow_rollback: user=%s bucket=%s amount=%d",
            user_id, bucket_id, rollback_amount,
        )
        return entry
    except Exception:
        logger.exception("shadow_rollback failed: user=%s bucket=%s", user_id, bucket_id)
        return None


# ---------------------------------------------------------------------------
# Live grant helpers — idempotent bucket creation for real user flows
# ---------------------------------------------------------------------------


async def ensure_free_bucket(db: AsyncSession, user_id) -> CreditsBucket | None:
    """Ensure the user has a free credits bucket. Idempotent.

    Called during login / credits read. Creates the bucket if it doesn't exist.
    Shadow mode: failures are logged, never block auth.
    """
    try:
        result = await db.execute(
            select(CreditsBucket).where(
                CreditsBucket.user_id == user_id,
                CreditsBucket.bucket_type == "free",
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

        grants = _get_runtime_grant_amounts()
        amount = grants.get("free", GRANT_AMOUNTS["free"])
        return await shadow_grant(
            db, user_id=user_id, bucket_type="free", amount=amount,
            source_label="free", reason_code="free_registration",
        )
    except Exception:
        logger.exception("ensure_free_bucket failed: user=%s", user_id)
        return None


async def ensure_trial_bucket(
    db: AsyncSession,
    user_id,
    trial_ends_at: datetime | None,
) -> CreditsBucket | None:
    """Ensure the user has a trial credits bucket. Idempotent.

    Called when trial is granted. Creates the bucket if it doesn't exist.
    Shadow mode: failures are logged, never block trial grant.
    """
    try:
        result = await db.execute(
            select(CreditsBucket).where(
                CreditsBucket.user_id == user_id,
                CreditsBucket.bucket_type == "trial",
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

        grants = _get_runtime_grant_amounts()
        amount = grants.get("trial", GRANT_AMOUNTS["trial"])
        return await shadow_grant(
            db, user_id=user_id, bucket_type="trial", amount=amount,
            source_label="trial", expires_at=trial_ends_at,
            reason_code="trial_grant",
        )
    except Exception:
        logger.exception("ensure_trial_bucket failed: user=%s", user_id)
        return None


async def ensure_subscription_bucket(
    db: AsyncSession,
    user_id,
    plan_code: str,
    related_order_id=None,
    related_subscription_id=None,
    expires_at: datetime | None = None,
) -> CreditsBucket | None:
    """Create a subscription credits bucket for a new billing period.

    Unlike free/trial, subscription buckets are created per billing period,
    so this is NOT idempotent in the same way — it creates a new bucket each
    time a new subscription period starts. Callers must guard against
    duplicate calls (e.g. via idempotent webhook processing).

    Shadow mode: failures are logged, never block subscription settlement.
    """
    try:
        grants = _get_runtime_grant_amounts()
        amount = grants.get(plan_code, grants.get("plus", GRANT_AMOUNTS.get("plus", 3500)))
        return await shadow_grant(
            db, user_id=user_id, bucket_type="subscription", amount=amount,
            source_label=plan_code, expires_at=expires_at,
            related_order_id=related_order_id,
            related_subscription_id=related_subscription_id,
            reason_code="subscription_grant",
        )
    except Exception:
        logger.exception("ensure_subscription_bucket failed: user=%s plan=%s", user_id, plan_code)
        return None


async def ensure_subscription_bucket_from_v2(
    db: AsyncSession,
    user_id,
) -> CreditsBucket | None:
    """Lazy-ensure a subscription bucket based on existing V2 active subscription.

    Queries the ``subscriptions`` table for the user's current active row.
    If an active subscription exists but no matching subscription bucket
    (keyed on ``related_subscription_id``) has been created yet, creates one.

    This covers the gap for pre-existing paid users who subscribed before
    V3-1 shadow grants were wired into the payment settlement path.

    Idempotent: if a bucket already exists for the same subscription ID,
    returns the existing bucket without creating a duplicate.

    Shadow mode: failures are logged, never block credits read.
    """
    try:
        from models import Subscription

        sub_result = await db.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.status == "active",
            )
        )
        active_sub = sub_result.scalar_one_or_none()
        if active_sub is None:
            return None

        # Check if we already have a bucket for this subscription
        existing_result = await db.execute(
            select(CreditsBucket).where(
                CreditsBucket.user_id == user_id,
                CreditsBucket.bucket_type == "subscription",
                CreditsBucket.related_subscription_id == active_sub.id,
            )
        )
        existing = existing_result.scalar_one_or_none()
        if existing is not None:
            return existing

        # No bucket for this subscription yet — create one
        plan_code = active_sub.plan_code
        grants = _get_runtime_grant_amounts()
        amount = grants.get(plan_code, grants.get("plus", GRANT_AMOUNTS.get("plus", 3500)))
        expires_at = active_sub.current_period_end

        return await shadow_grant(
            db, user_id=user_id, bucket_type="subscription", amount=amount,
            source_label=plan_code, expires_at=expires_at,
            related_subscription_id=active_sub.id,
            reason_code="subscription_grant_backfill",
        )
    except Exception:
        logger.exception(
            "ensure_subscription_bucket_from_v2 failed: user=%s", user_id
        )
        return None


# ---------------------------------------------------------------------------
# Shadow integration helper — safe wrapper for job pipeline hooks
# ---------------------------------------------------------------------------


async def ensure_credit_buckets_for_user(
    db: AsyncSession,
    *,
    user=None,
    user_id=None,
) -> None:
    """Ensure buckets needed by job and clone credit paths exist.

    This mirrors the lazy setup done by the credits read endpoint so metering
    does not depend on the user opening the billing page before creating work.
    """
    uid = user_id or getattr(user, "id", None)
    if uid is None:
        return
    await ensure_free_bucket(db, uid)
    if user is not None:
        try:
            from plan_catalog import is_user_in_active_trial

            if is_user_in_active_trial(user):
                await ensure_trial_bucket(db, uid, getattr(user, "trial_ends_at", None))
        except Exception:
            logger.exception("ensure_credit_buckets_for_user trial ensure failed: user=%s", uid)
    await ensure_subscription_bucket_from_v2(db, uid)


async def shadow_safe(coro_fn, *args, **kwargs) -> Any:
    """Call a shadow credits coroutine, catching all exceptions.

    Ensures V2 production paths are never blocked by shadow ledger failures.
    Returns the coroutine result or None on failure.
    """
    try:
        return await coro_fn(*args, **kwargs)
    except Exception:
        logger.exception("shadow_safe: %s failed (args suppressed for brevity)", coro_fn.__name__)
        return None
