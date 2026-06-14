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

from models import CreditsBucket, CreditsLedger, Job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost model constants (frozen V3 parameters — backward compat snapshots)
# ---------------------------------------------------------------------------

# Points per source minute by (service_mode, quality_tier)
# Task #24 (P2 launch blocker #2, 2026-05-24): smart.standard=100 added
# so that any path falling back to these frozen constants (e.g. when
# pricing_runtime is missing/corrupt) correctly charges smart at the
# spec rate. Without it, smart silently fell back to DEFAULT_DEBIT_RATE
# = 10, under-reserving 10×.
DEBIT_RATES: dict[tuple[str, str], int] = {
    ("express", "standard"): 10,
    # Phase 2a free tier — free never debits (credits=0). Frozen mirror of the
    # pricing_runtime default; without it (free, standard) falls to
    # DEFAULT_DEBIT_RATE=10 and free users would be wrongly charged 10/min.
    ("free", "standard"): 0,
    ("studio", "standard"): 15,
    ("studio", "high"): 30,
    ("studio", "flagship"): 50,
    ("smart", "standard"): 100,
}

DEFAULT_DEBIT_RATE = 10  # fallback

# Bucket consumption priority per service mode
# Task #24: smart added (paid-first, same as studio) — without it,
# _pick_buckets_by_priority's fallback ``bp.get(service_mode, bp.get("express"))``
# made smart consume free quota first.
BUCKET_PRIORITY: dict[str, list[str]] = {
    "express": ["free", "subscription", "topup", "trial"],
    "studio": ["trial", "subscription", "topup", "free"],
    "smart": ["trial", "subscription", "topup", "free"],
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

ADMIN_CREDITS_GRANT = 1_000_000
ADMIN_CREDITS_SOURCE_LABEL = "admin_grant"


class InsufficientCreditsError(Exception):
    """Raised when a live credit reservation cannot be fully covered."""

    def __init__(self, *, required: int, available: int) -> None:
        super().__init__(
            f"insufficient credits: required={required}, available={available}"
        )
        self.required = required
        self.available = available


# ---------------------------------------------------------------------------
# Runtime pricing accessors — derive from pricing_runtime, fallback to frozen
# ---------------------------------------------------------------------------


def _get_runtime_debit_rates() -> dict[tuple[str, str], int]:
    """Runtime pricing OVERLAID on the frozen constants.

    Frozen ``DEBIT_RATES`` is the baseline (spec-correct defaults); runtime is
    the source of truth only for the keys it actually defines. A stale runtime
    snapshot missing a newer key (e.g. ``free.standard`` / ``smart.standard``)
    therefore falls back to the frozen value, NOT ``DEFAULT_DEBIT_RATE`` — so a
    free job is never silently charged 10/min from an old pricing_runtime.json
    (CodeX P1).
    """
    merged: dict[tuple[str, str], int] = dict(DEBIT_RATES)
    try:
        from pricing_runtime import get_runtime_pricing
        credits = get_runtime_pricing().credits
        for key, value in credits.debit_rates.items():
            parts = key.split(".", 1)
            if len(parts) == 2:
                merged[(parts[0], parts[1])] = value
    except Exception:
        return dict(DEBIT_RATES)
    return merged


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
    # A genuinely-free mode (rate 0 — e.g. the Phase 2a free tier) costs 0. The
    # min-1-credit floor below only guards sub-1 rounding for PAID modes; it must
    # NOT turn a 0 rate into a 1-credit charge. This is the single debit-compute
    # point (reserve / terminal settle / shadow all call estimate_credits).
    if rate <= 0:
        return 0
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


def _bucket_available(bucket: CreditsBucket) -> int:
    # Historical bucket rows can contain negative ``reserved`` after repeated
    # best-effort settlement attempts. Never let that inflate spendable balance.
    return max(0, int(bucket.remaining or 0) - max(0, int(bucket.reserved or 0)))


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


_MINIMAX_HD_MODELS = {"hd", "speech-2.8-hd", "speech-02-hd"}
_MINIMAX_TURBO_MODELS = {"turbo", "speech-2.8-turbo", "speech-02-turbo"}


def _provider_model_tier(provider: str | None, model: str | None) -> str | None:
    provider_key = _norm(provider)
    model_key = _norm(model)
    is_minimax = provider_key == "minimax" or model_key in _MINIMAX_HD_MODELS or model_key in _MINIMAX_TURBO_MODELS
    if not is_minimax:
        return None
    if model_key in _MINIMAX_HD_MODELS:
        return "flagship"
    if model_key in _MINIMAX_TURBO_MODELS:
        return "high"
    return "high"


def _tier_from_tts_execution_distribution(snapshot: dict[str, Any]) -> str | None:
    """Infer tier from actual UsageMeter TTS events summarized into snapshot.

    This is the strongest signal because it reflects the provider/model that
    actually synthesized audio, not only the user's intended selection.
    """
    strongest: str | None = None
    for field in (
        "tts_billed_chars_by_provider_model",
        "tts_call_count_by_provider_model",
    ):
        dist = snapshot.get(field)
        if not isinstance(dist, dict):
            continue
        for raw_key, raw_count in dist.items():
            try:
                count = int(raw_count or 0)
            except (TypeError, ValueError):
                count = 0
            if count <= 0:
                continue
            parts = str(raw_key or "").split(":", 1)
            provider = parts[0] if parts else ""
            model = parts[1] if len(parts) > 1 else ""
            tier = _provider_model_tier(provider, model)
            if tier == "flagship":
                return "flagship"
            if tier == "high":
                strongest = "high"
    return strongest


def infer_quality_tier_from_execution(
    *,
    service_mode: str | None,
    tts_provider: str | None,
    tts_model: str | None,
    snapshot: dict[str, Any] | None = None,
) -> str:
    """Infer debit tier from the user's selected and executed TTS model.

    Studio MiniMax has tiered billing: turbo=high, hd=flagship. The actual
    UsageMeter execution distribution wins over planned job fields; job fields
    are only fallback signals. Other current providers stay standard unless a
    trustworthy snapshot says otherwise.
    """
    service = _norm(service_mode or (snapshot or {}).get("service_mode") or "express")
    if service != "studio":
        return "standard"

    snap = snapshot if isinstance(snapshot, dict) else {}

    execution_tier = _tier_from_tts_execution_distribution(snap)
    if execution_tier is not None:
        return execution_tier

    any_minimax = False
    any_turbo = False
    for item in snap.get("per_speaker_provider") or []:
        if not isinstance(item, dict):
            continue
        sp_provider = _norm(item.get("tts_provider") or item.get("provider"))
        sp_model = _norm(
            item.get("minimax_model")
            or item.get("tts_model")
            or item.get("model")
        )
        if sp_provider != "minimax" and sp_model not in _MINIMAX_HD_MODELS and sp_model not in _MINIMAX_TURBO_MODELS:
            continue
        any_minimax = True
        if sp_model in _MINIMAX_HD_MODELS:
            return "flagship"
        if sp_model in _MINIMAX_TURBO_MODELS:
            any_turbo = True

    if any_turbo or any_minimax:
        return "high"

    planned_tier = _provider_model_tier(
        tts_provider or snap.get("tts_provider"),
        tts_model or snap.get("tts_model"),
    )
    if planned_tier is not None:
        return planned_tier

    snapshot_tier = _norm(snap.get("quality_tier"))
    if snapshot_tier in {"standard", "high", "flagship"}:
        return snapshot_tier
    return "standard"


def should_settle_job_credits(job: Any) -> bool:
    if getattr(job, "copy_of_job_id", None):
        return False
    try:
        if int(getattr(job, "edit_generation", 0) or 0) > 0:
            return False
    except (TypeError, ValueError):
        return False
    return True


def _snapshot_int(snapshot: dict[str, Any], key: str) -> int:
    try:
        return int(snapshot.get(key) or 0)
    except (TypeError, ValueError):
        return 0


async def _has_job_credit_reserve(db: AsyncSession, *, user_id, job_id: str) -> bool:
    result = await db.execute(
        select(CreditsLedger)
        .where(
            CreditsLedger.user_id == user_id,
            CreditsLedger.related_job_id == job_id,
            CreditsLedger.direction == "reserve",
            CreditsLedger.reason_code == "job_reserve",
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def estimate_actual_job_credits(job: Any) -> tuple[int, str, float | None, str]:
    snapshot = getattr(job, "metering_snapshot", None)
    if not isinstance(snapshot, dict):
        snapshot = {}
    service_mode = _norm(
        getattr(job, "service_mode", None)
        or snapshot.get("service_mode")
        or "express"
    )
    quality_tier = infer_quality_tier_from_execution(
        service_mode=service_mode,
        tts_provider=getattr(job, "tts_provider", None) or snapshot.get("tts_provider"),
        tts_model=getattr(job, "tts_model", None) or snapshot.get("tts_model"),
        snapshot=snapshot,
    )

    minutes = getattr(job, "actual_minutes", None)
    if minutes is None:
        source_duration = getattr(job, "source_duration_seconds", None)
        if source_duration:
            minutes = float(source_duration) / 60.0
    if minutes is None:
        minutes = getattr(job, "estimated_minutes", None)

    credits = estimate_credits(
        float(minutes) if minutes else None,
        service_mode=service_mode,
        quality_tier=quality_tier,
    )
    return credits, quality_tier, float(minutes) if minutes else None, service_mode


def _settlement_reason_codes(reason_code: str, reserve_reason_code: str | None) -> set[str]:
    """Return the set of reason_codes that count as "the same job-reserve
    settlement event" for idempotency dedup in ``_has_existing_settlement``.

    A retry that observes a job in the same terminal status and re-attempts
    settlement must be recognised as already-done — otherwise sweepers,
    list-jobs polling and detail polling double-bill.

    The legacy set covers Express/Studio job_reserve → job_capture/job_release
    plus the capture_additional / capture_overdraft / capture_excess_release
    correction codes. Smart MVP P2 (plan §5.2 末段) adds a parallel set of
    smart-distinct reason_codes that the F4 dispatcher writes — they belong
    in the same idempotency family because they all settle the same single
    job_reserve and a repeat settle attempt must collapse onto the original
    write, not duplicate it.
    """
    legacy_job_reserve_codes = {
        "job_capture",
        "job_release",
        "capture_additional",
        "capture_overdraft",
        "capture_excess_release",
        # P3e-3c-1（CodeX 复核 P2）：智能版预览跳分钟 settle 走 shadow_release(
        # reason_code="smart_preview_minute_release") 释放 job_reserve（理论无
        # job_reserve → no-op）。归入同一 job_reserve 幂等族，否则重复 terminal
        # settle（list-jobs/detail/sweeper 多次观察终态）在 fail-safe 释放路径上会
        # 重复写 release ledger（不扣钱、不留 reserved，但非幂等）。
        "smart_preview_minute_release",
    }
    # Smart MVP P2 (plan §5.2 末段) — F4 dispatcher reason_codes. Three
    # credits_policy paths × the three-step partial-capture flow add up to
    # five distinct codes, all settling the same job_reserve.
    smart_job_reserve_codes = {
        "smart_refund_full",
        "smart_capture_full",
        "smart_fail_and_refund_release",
        "smart_fail_and_refund_clone_reversal",
        "smart_fail_and_refund_partial_capture",
    }
    if (
        reserve_reason_code == "job_reserve"
        or reason_code == "job_capture"
        or reason_code in smart_job_reserve_codes
    ):
        return legacy_job_reserve_codes | smart_job_reserve_codes
    return {reason_code}


async def _has_existing_settlement(
    db: AsyncSession,
    *,
    user_id,
    job_id: str,
    reason_code: str,
    reserve_reason_code: str | None,
) -> bool:
    reason_codes = _settlement_reason_codes(reason_code, reserve_reason_code)
    result = await db.execute(
        select(CreditsLedger)
        .where(
            CreditsLedger.user_id == user_id,
            CreditsLedger.related_job_id == job_id,
            CreditsLedger.direction.in_(["capture", "release"]),
            CreditsLedger.reason_code.in_(reason_codes),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


async def revoke_buckets_for_order(
    db: AsyncSession,
    *,
    user_id,
    related_order_id,
    reason_code: str = "refund_revoke",
) -> int:
    """退款回收（R7）：把与某订单关联的 bucket 可用余额清零。

    只动 ``remaining``——``reserved`` 留给在途任务既有的 capture/release
    闭环自行收敛，不在这里抢状态。每个被回收的 bucket 写一条
    ``direction='revoke'`` 负值 ledger 保审计可追。

    Shadow 语义（与 shadow_grant 同风格）：任何异常自吞返回 0，绝不让
    credits 回收失败阻断退款结算主流程（发票/订单真值优先落库）。
    """
    try:
        result = await db.execute(
            select(CreditsBucket)
            .where(
                CreditsBucket.user_id == user_id,
                CreditsBucket.related_order_id == related_order_id,
                CreditsBucket.remaining > 0,
            )
            .with_for_update()
        )
        buckets = list(result.scalars().all())
        revoked = 0
        for bucket in buckets:
            delta = -int(bucket.remaining)
            bucket.remaining = 0
            db.add(CreditsLedger(
                user_id=user_id,
                bucket_id=bucket.id,
                direction="revoke",
                credits_delta=delta,
                balance_after=0,
                related_order_id=related_order_id,
                reason_code=reason_code,
            ))
            revoked += 1
        if revoked:
            logger.info(
                "revoke_buckets_for_order: user=%s order=%s buckets=%d",
                user_id, related_order_id, revoked,
            )
        return revoked
    except Exception:
        logger.warning(
            "revoke_buckets_for_order failed (user=%s order=%s)",
            user_id, related_order_id, exc_info=True,
        )
        return 0


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
            available = _bucket_available(bucket)
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


async def reserve_credits_or_raise(
    db: AsyncSession,
    *,
    user_id,
    job_id: str,
    estimated_credits: int,
    service_mode: str = "express",
    reason_code: str = "job_reserve",
) -> list[CreditsLedger]:
    """Reserve credits and fail atomically when the user cannot cover the cost.

    This is the live gating counterpart to ``shadow_reserve``. It keeps the
    same bucket priority and ledger shape, but it refuses partial reserves so a
    job or paid action can be stopped before downstream work begins.
    """
    if estimated_credits <= 0:
        return []

    buckets = await get_user_buckets(db, user_id, for_update=True)
    ordered = _pick_buckets_by_priority(buckets, service_mode)
    available_total = sum(_bucket_available(bucket) for bucket in ordered)
    if available_total < estimated_credits:
        logger.info(
            "reserve_credits_or_raise: insufficient credits for user=%s job=%s "
            "needed=%d available=%d",
            user_id, job_id, estimated_credits, available_total,
        )
        raise InsufficientCreditsError(
            required=estimated_credits,
            available=available_total,
        )

    remaining_to_reserve = estimated_credits
    entries: list[CreditsLedger] = []
    for bucket in ordered:
        if remaining_to_reserve <= 0:
            break
        available = _bucket_available(bucket)
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

    return entries


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
        if await _has_existing_settlement(
            db,
            user_id=user_id,
            job_id=job_id,
            reason_code=reason_code,
            reserve_reason_code=reserve_reason_code,
        ):
            logger.info(
                "shadow_capture: job=%s reason=%s already settled, skipping",
                job_id,
                reason_code,
            )
            return []

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

                # Clear this entry's reservation from bucket. Clamp because
                # older repeated settlement attempts may already have driven
                # the mutable bucket counter below zero.
                bucket.reserved = max(0, int(bucket.reserved or 0) - entry_reserved)
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
                bucket.reserved = max(0, int(bucket.reserved or 0) - consumed)
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
                    available = _bucket_available(bucket)
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
        if await _has_existing_settlement(
            db,
            user_id=user_id,
            job_id=job_id,
            reason_code=reason_code,
            reserve_reason_code=reserve_reason_code,
        ):
            logger.info(
                "shadow_release: job=%s reason=%s already settled, skipping",
                job_id,
                reason_code,
            )
            return []

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
            bucket.reserved = max(0, int(bucket.reserved or 0) - release_amount)

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


async def settle_job_credit_ledger(
    db: AsyncSession,
    job: Any,
    terminal_status: str,
) -> list[CreditsLedger]:
    """Settle per-minute job credits for a terminal job.

    The debit tier is inferred from the selected/final TTS execution model, not
    from subscription identity. This function is idempotent via ledger guards in
    ``shadow_capture`` / ``shadow_release``.
    """
    if not should_settle_job_credits(job):
        return []

    job_id = str(getattr(job, "job_id", "") or "")
    user_id = getattr(job, "user_id", None)
    if not job_id or user_id is None:
        return []

    # Serialize all terminal credit settlement attempts for this job. Multiple
    # request paths can observe the same terminal state (detail polling,
    # list-jobs, R2 sweeper); without the row lock two transactions can both
    # pass the idempotency check and double-capture the user's credits.
    lock_result = await db.execute(
        select(Job)
        .where(Job.job_id == job_id, Job.user_id == user_id)
        .with_for_update()
    )
    locked_job = lock_result.scalar_one_or_none()
    if locked_job is not None:
        job = locked_job

    snapshot = dict(getattr(job, "metering_snapshot", None) or {})

    # P3e-3c-1 钱-关键（对抗性/CodeX P0）：智能版 3min 预览只扣 600 克隆点（独立
    # reason_code smart_clone_capture_* 经 settle_smart_clone_reservation 照常 capture）、
    # **不扣分钟/job 点**。create/late 已跳 minute reserve（reason_code job_reserve）；
    # 但下方 succeeded 分支会按 actual_minutes/source_duration 重算 actual_credits 调
    # shadow_capture，而 shadow_capture 在 total_reserved=0 时进 actual>reserved 分支
    # **从余额额外 debit**（capture_additional / capture_overdraft，**非**纯
    # capture-of-reserve）→ 即便无分钟 reserve 也会扣分钟。故对预览显式走 release：
    # 无 job_reserve → no-op；万一有（skip 失守）→ 释放退回，**绝不** capture 分钟
    # （fail-safe，方向只会是 release/no-op）。在 has_credit_intent / credits_policy
    # 分发**之前**短路，credits_estimated 是否非零都不影响。
    if (dict(getattr(job, "smart_state", None) or {})).get("smart_preview_mode") is True:
        return await shadow_release(
            db,
            user_id=user_id,
            job_id=job_id,
            reason_code="smart_preview_minute_release",
            reserve_reason_code="job_reserve",
        )

    has_credit_intent = (
        _snapshot_int(snapshot, "credits_estimated") > 0
        or await _has_job_credit_reserve(db, user_id=user_id, job_id=job_id)
    )
    if not has_credit_intent:
        # Legacy jobs created before credit reservation should not be charged
        # retroactively just because a sweeper or detail poll sees them again.
        return []

    # F4 (Smart MVP P2 skeleton, plan 2026-05-13 §5.2 末段): smart_state
    # dispatcher MUST be evaluated BEFORE the legacy succeeded/failed
    # branches. Smart `fail_and_refunded` jobs may carry job.status="failed"
    # (so they would otherwise hit the `failed → release_full` legacy branch
    # and never apply the partial-capture settlement). Likewise smart
    # `degraded_delivery_with_report` jobs land status="succeeded" and need
    # the explicit smart capture_full path even though the dollar amount
    # currently matches the legacy succeeded branch — that may diverge once
    # smart credits_policy gains nuances. Always read credits_policy first;
    # fall through only when there's no smart_state or no policy.
    smart_state = dict(getattr(job, "smart_state", None) or {})
    credits_policy = smart_state.get("credits_policy") if smart_state else None
    if credits_policy:
        return await _settle_smart_job_credit_ledger(
            db,
            job=job,
            user_id=user_id,
            job_id=job_id,
            credits_policy=str(credits_policy),
            terminal_status=terminal_status,
        )

    if terminal_status == "succeeded":
        actual_credits, quality_tier, minutes, service_mode = estimate_actual_job_credits(job)
        if actual_credits <= 0:
            return []

        snapshot["credits_actual"] = actual_credits
        snapshot["quality_tier"] = quality_tier
        snapshot["credits_actual_quality_tier"] = quality_tier
        snapshot["credits_actual_minutes"] = round(float(minutes or 0.0), 6)
        snapshot["credits_actual_source"] = "final_tts_model"
        job.metering_snapshot = snapshot
        if minutes is not None:
            job.actual_minutes = float(minutes)

        await ensure_credit_buckets_for_user(
            db,
            user_id=user_id,
            role=getattr(job, "role_snapshot", None),
        )
        return await shadow_capture(
            db,
            user_id=user_id,
            job_id=job_id,
            actual_credits=actual_credits,
            service_mode=service_mode,
            reason_code="job_capture",
            reserve_reason_code="job_reserve",
        )

    if terminal_status in {"failed", "cancelled"}:
        return await shadow_release(
            db,
            user_id=user_id,
            job_id=job_id,
            reason_code="job_release",
            reserve_reason_code="job_reserve",
        )

    return []


# ---------------------------------------------------------------------------
# F4 — Smart MVP P2 settle dispatcher + stub helpers (plan 2026-05-13 §5.2)
# ---------------------------------------------------------------------------
#
# Status: SKELETON. Three credits_policy branches map to existing
# shadow_release / shadow_capture for now plus two new stub helpers
# (refund_captured_voice_clone, partial_capture_actual_cost). The stubs
# carry the correct call shape and a NotImplementedError safety guard so
# real settlement land in subsequent PRs without changing the dispatcher
# wiring. Tests in tests/test_smart_skeleton_acceptance.py lock the
# dispatcher contract so the stubs cannot silently regress.

async def _settle_smart_job_credit_ledger(
    db: AsyncSession,
    *,
    job: Any,
    user_id,
    job_id: str,
    credits_policy: str,
    terminal_status: str,
) -> list[CreditsLedger]:
    """Smart credits_policy → settlement-action dispatcher.

    Plan §4.3 mapping table + §5.2 末段 three-step partial capture flow.
    See module docstring for skeleton boundary.
    """
    if credits_policy == "refund_full":
        # speaker gate fail / early downgrade / system bug — release the
        # whole reserve, nothing else captured yet by definition.
        return await shadow_release(
            db,
            user_id=user_id,
            job_id=job_id,
            reason_code="smart_refund_full",
            reserve_reason_code="job_reserve",
        )

    if credits_policy == "capture_full":
        # degraded_delivery_with_report — same dollar amount as legacy
        # succeeded but we route through a smart-distinct reason_code so
        # the audit trail makes the policy decision visible.
        actual_credits, _quality_tier, _minutes, service_mode = estimate_actual_job_credits(job)
        if actual_credits <= 0:
            return []
        return await shadow_capture(
            db,
            user_id=user_id,
            job_id=job_id,
            actual_credits=actual_credits,
            service_mode=service_mode,
            reason_code="smart_capture_full",
            reserve_reason_code="job_reserve",
        )

    if credits_policy == "capture_actual_cost_capped_at_studio_price":
        # Codex 第四十轮 P1.2 hard-gate (2026-05-16): the documented
        # three-step ``fail_and_refund`` flow (release reserve → refund
        # captured clone → partial capture) per plan §5.2 cannot run
        # safely today because:
        #
        #   - ``refund_captured_voice_clone`` is a STUB returning [].
        #     Its own docstring warns "no ledger entries written.
        #     Skeleton boundary; replace before Smart paths that rely
        #     on captured clone refunds go live."
        #   - ``partial_capture_actual_cost`` is similarly stub.
        #
        # Pre-hard-gate behavior: dispatcher silently aggregated the
        # [] returns and the caller's post-settle backfill stamped
        # ``settled_at`` on bogus accounting state (reservation
        # released but clone capture never reversed → user's ledger
        # inconsistent, internal margin tracking wrong).
        #
        # Defense in depth: Fix 1's SmartConsent validator
        # (gateway/smart_consent.py) rejects
        # ``on_budget_exhausted=fail_and_refund`` at job-creation, so
        # this branch should never be reached via the normal user
        # path. If it IS reached (admin override / bug / future
        # refactor introduced the policy somewhere), log loudly + no
        # ledger entries. Replace this gate with the real
        # implementation when refund_captured_voice_clone +
        # partial_capture_actual_cost are no longer stubs.
        logger.error(
            "settle_smart: credits_policy=%r is not implemented yet "
            "(STUB refund_captured_voice_clone / "
            "partial_capture_actual_cost). "
            "job=%s user=%s — returning [] (no ledger entries). "
            "Replace STUB implementations before re-enabling this policy "
            "(see refund_captured_voice_clone docstring).",
            credits_policy, job_id, user_id,
        )
        return []

    logger.warning(
        "settle_smart: unrecognised credits_policy=%r for job=%s; "
        "falling through to terminal_status=%r legacy branch",
        credits_policy, job_id, terminal_status,
    )
    return []


async def refund_captured_voice_clone(
    db: AsyncSession,
    *,
    user_id,
    job_id: str,
    reason_code: str = "voice_clone_capture_reversal",
) -> list[CreditsLedger]:
    """STUB — reverse already-captured ``voice_clone_capture`` ledger entries.

    Real implementation (subsequent PR) selects all
    ``direction='capture' AND reason_code='voice_clone_capture'`` rows
    for this job and writes matching ``direction='reversal'`` entries
    that bring the user's bucket back to pre-clone-capture state.

    Skeleton currently returns an empty list — the dispatcher contract
    is locked by acceptance tests but the refund itself is no-op until
    voice-clone settlement gets its real implementation. Production
    must NOT enable Smart paths that rely on captured clone refunds
    until this stub is replaced.
    """
    logger.warning(
        "refund_captured_voice_clone STUB invoked for user=%s job=%s reason=%s — "
        "no ledger entries written. Skeleton boundary; replace before "
        "Smart paths that rely on captured clone refunds go live.",
        user_id, job_id, reason_code,
    )
    return []


async def partial_capture_actual_cost(
    db: AsyncSession,
    *,
    job: Any,
    user_id,
    job_id: str,
    reason_code: str = "smart_fail_and_refund_partial_capture",
) -> list[CreditsLedger]:
    """STUB — partial capture for Smart fail_and_refund (plan §5.2 step 3).

    Real implementation (subsequent PR):
      1. Read UsageMeter.summarize() RMB cost from project_dir/usage_events.jsonl
      2. credits = max(1, ceil(cost_rmb / point_price_rmb))
      3. Cap at source_minutes × studio.standard rate
      4. Write a ``direction='capture'`` ledger row with the partial amount

    Skeleton returns an empty list and logs a warning so an accidental
    production trip is loud rather than silent. The dispatcher contract
    is locked by acceptance tests.
    """
    logger.warning(
        "partial_capture_actual_cost STUB invoked for user=%s job=%s reason=%s — "
        "no ledger entry written. Skeleton boundary; replace before Smart "
        "fail_and_refund settlement is enabled in production.",
        user_id, job_id, reason_code,
    )
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

    Unlike free/trial, subscription buckets are created per billing period —
    each new period gets a new bucket. 同一结算（同一 ``related_order_id``）
    则幂等：webhook / 对账 sweeper / 用户轮询 refresh 三个并发入口都可能
    触发同一订单的 paid 结算，订单行锁是主防线，这里按 related_order_id
    查重是纵深防御（Codex review 2026-06-13 P1）。

    Shadow mode: failures are logged, never block subscription settlement.
    """
    try:
        if related_order_id is not None:
            existing = (
                await db.execute(
                    select(CreditsBucket).where(
                        CreditsBucket.user_id == user_id,
                        CreditsBucket.bucket_type == "subscription",
                        CreditsBucket.related_order_id == related_order_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
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


async def ensure_admin_credits_bucket(db: AsyncSession, user_id) -> CreditsBucket | None:
    """Ensure administrators have a long-lived 1,000,000 point bucket."""
    try:
        # P0-4 (audit 2026-05-07): row lock to prevent concurrent reserve from over-allocating free quota
        # (here: protects the read-modify-write top-up branch below from lost updates
        # when two requests both observe granted < ADMIN_CREDITS_GRANT and both add delta).
        result = await db.execute(
            select(CreditsBucket).where(
                CreditsBucket.user_id == user_id,
                CreditsBucket.bucket_type == "manual_adjustment",
                CreditsBucket.source_label == ADMIN_CREDITS_SOURCE_LABEL,
            ).with_for_update()
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            return await shadow_grant(
                db,
                user_id=user_id,
                bucket_type="manual_adjustment",
                amount=ADMIN_CREDITS_GRANT,
                source_label=ADMIN_CREDITS_SOURCE_LABEL,
                reason_code="admin_grant",
            )

        if int(existing.granted or 0) < ADMIN_CREDITS_GRANT:
            delta = ADMIN_CREDITS_GRANT - int(existing.granted or 0)
            existing.granted = int(existing.granted or 0) + delta
            existing.remaining = int(existing.remaining or 0) + delta
            entry = CreditsLedger(
                user_id=user_id,
                bucket_id=existing.id,
                direction="grant",
                credits_delta=delta,
                balance_after=existing.remaining,
                reason_code="admin_grant_topup",
            )
            db.add(entry)
        return existing
    except Exception:
        logger.exception("ensure_admin_credits_bucket failed: user=%s", user_id)
        return None


# ---------------------------------------------------------------------------
# Shadow integration helper — safe wrapper for job pipeline hooks
# ---------------------------------------------------------------------------


async def ensure_credit_buckets_for_user(
    db: AsyncSession,
    *,
    user=None,
    user_id=None,
    role: str | None = None,
) -> None:
    """Ensure buckets needed by job and clone credit paths exist.

    This mirrors the lazy setup done by the credits read endpoint so metering
    does not depend on the user opening the billing page before creating work.
    """
    uid = user_id or getattr(user, "id", None)
    if uid is None:
        return
    user_role = str(role or getattr(user, "role", "") or "").lower()
    if user_role == "admin":
        await ensure_admin_credits_bucket(db, uid)
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
