"""Credits read-only API — V3-2 user-visible surfaces.

Exposes shadow credits data (buckets, ledger) as read-only JSON endpoints.
These endpoints consume V3-1 shadow data and present it to the frontend.
They do NOT gate job execution or replace V2 billing/entitlements truth.

Endpoints:
- GET /api/me/credits         — current balance, bucket breakdown, trial info
- GET /api/me/credits-ledger  — recent ledger entries
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from credits_service import (
    ensure_free_bucket,
    ensure_subscription_bucket_from_v2,
    ensure_trial_bucket,
    estimate_credits,
)
from database import get_db
from models import CreditsBucket, CreditsLedger, User
from plan_catalog import is_user_in_active_trial

logger = logging.getLogger(__name__)

router = APIRouter(tags=["credits"])


# ---------------------------------------------------------------------------
# GET /api/me/credits
# ---------------------------------------------------------------------------


def _serialize_bucket(bucket: CreditsBucket) -> dict:
    return {
        "id": str(bucket.id),
        "type": bucket.bucket_type,
        "remaining": bucket.remaining,
        "reserved": bucket.reserved,
        "granted": bucket.granted,
        "expires_at": bucket.expires_at.isoformat() if bucket.expires_at else None,
        "source_label": bucket.source_label,
    }


@router.get("/api/me/credits")
async def get_my_credits(
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict:
    """Return current user's credits balance and bucket breakdown.

    This is a read-only view of V3-1 shadow credits. It does NOT
    represent the V2 billing/quota truth — those continue to be
    served by ``/api/me/entitlements`` and ``/api/me/subscription``.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    now = datetime.now(timezone.utc)

    # Lazy-ensure free + trial + subscription buckets exist (shadow, best-effort)
    try:
        await ensure_free_bucket(db, user.id)
        if is_user_in_active_trial(user):
            trial_ends = getattr(user, "trial_ends_at", None)
            await ensure_trial_bucket(db, user.id, trial_ends)
        # Backfill subscription bucket from V2 active subscription truth
        await ensure_subscription_bucket_from_v2(db, user.id)
        await db.commit()
    except Exception:
        logger.warning("credits read: lazy bucket ensure failed (non-fatal)")
        try:
            await db.rollback()
        except Exception:
            pass

    result = await db.execute(
        select(CreditsBucket)
        .where(CreditsBucket.user_id == user.id)
        .order_by(CreditsBucket.created_at)
    )
    all_buckets = list(result.scalars().all())

    # Filter to non-expired buckets for the active view
    active_buckets = [
        b for b in all_buckets
        if b.expires_at is None or b.expires_at > now
    ]

    total_available = sum(
        max(0, b.remaining - b.reserved) for b in active_buckets
    )

    # Find trial bucket expiration
    trial_bucket = next(
        (b for b in active_buckets if b.bucket_type == "trial"),
        None,
    )
    in_trial = is_user_in_active_trial(user)
    trial_expires_at = None
    if trial_bucket and trial_bucket.expires_at:
        trial_expires_at = trial_bucket.expires_at.isoformat()

    return {
        "total_available": total_available,
        "buckets": [_serialize_bucket(b) for b in active_buckets],
        "in_trial": in_trial,
        "trial_expires_at": trial_expires_at,
    }


# ---------------------------------------------------------------------------
# GET /api/me/credits-ledger
# ---------------------------------------------------------------------------


def _serialize_ledger_entry(entry: CreditsLedger) -> dict:
    return {
        "id": str(entry.id),
        "direction": entry.direction,
        "credits_delta": entry.credits_delta,
        "balance_after": entry.balance_after,
        "related_job_id": entry.related_job_id,
        "reason_code": entry.reason_code,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


@router.get("/api/me/credits-ledger")
async def get_my_credits_ledger(
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Return recent credits ledger entries for the current user.

    Newest first, capped at ``limit``. No complex filtering or pagination
    framework — just enough for the billing page baseline history.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    result = await db.execute(
        select(CreditsLedger)
        .where(CreditsLedger.user_id == user.id)
        .order_by(CreditsLedger.created_at.desc())
        .limit(limit)
    )
    entries = list(result.scalars().all())

    return {
        "entries": [_serialize_ledger_entry(e) for e in entries],
        "count": len(entries),
    }


# ---------------------------------------------------------------------------
# GET /api/credits/estimate — public credits cost preview
# ---------------------------------------------------------------------------


@router.get("/api/credits/estimate")
async def estimate_job_credits(
    minutes: float = Query(ge=0, le=300),
    service_mode: str = Query(default="express"),
    quality_tier: str = Query(default="standard"),
) -> dict:
    """Return estimated credits cost for a given duration/mode/quality.

    Public endpoint (no auth required) for the workspace cost preview.
    Does NOT reserve or debit anything — purely informational.
    """
    credits = estimate_credits(minutes, service_mode, quality_tier)
    return {
        "estimated_credits": credits,
        "minutes": minutes,
        "service_mode": service_mode,
        "quality_tier": quality_tier,
    }
