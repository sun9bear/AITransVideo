"""V3-3 Shadow Observability Baseline — admin-only read surfaces.

Provides a minimal, read-only JSON surface for maintainers to verify that
shadow credits data (buckets, ledger, job metering) is being written correctly
during the V3 pilot period.

Endpoints:
- GET /api/admin/credits/summary   — aggregate overview of buckets, ledger, metering

Auth: admin role required (same pattern as admin_settings.py).

This module does NOT gate job execution, modify data, or replace V2 truth.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, cast, func, select, String
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import CreditsBucket, CreditsLedger, Job, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/credits", tags=["admin-credits"])


# ---------------------------------------------------------------------------
# Field status reference — LIVE vs RESERVED
# ---------------------------------------------------------------------------

# This constant is returned in the summary response so maintainers can see
# at a glance which metering fields are actually being populated vs. which
# are only schema placeholders awaiting Pipeline-side callbacks.
FIELD_STATUS = {
    # --- LIVE: written by Gateway in V3-0/V3-1/V3-2 ---
    "estimated_minutes": {
        "status": "LIVE",
        "source": "intercept_create_job (estimated_duration_seconds / 60)",
    },
    "actual_minutes": {
        "status": "LIVE",
        "source": "update_source_metadata / terminal settle (source_duration_seconds / 60)",
    },
    "metering_snapshot.credits_estimated": {
        "status": "LIVE",
        "source": "intercept_create_job → estimate_credits()",
    },
    "metering_snapshot.credits_actual": {
        "status": "LIVE",
        "source": "intercept_list_jobs terminal settle → estimate_credits()",
    },
    "metering_snapshot.service_mode": {
        "status": "LIVE",
        "source": "intercept_create_job → job policy",
    },
    "metering_snapshot.tts_provider": {
        "status": "LIVE",
        "source": "intercept_create_job → job policy",
    },
    "metering_snapshot.tts_model": {
        "status": "LIVE",
        "source": "intercept_create_job → job policy",
    },
    # --- LIVE (written by Pipeline via POST /job-api/jobs/{job_id}/metering, V3-4) ---
    "metering_snapshot.final_cn_chars": {
        "status": "LIVE",
        "source": "Pipeline S6 completion → _report_job_metering() → POST /metering",
    },
    "metering_snapshot.rewrite_triggered": {
        "status": "LIVE",
        "source": "Pipeline S6 completion → _report_job_metering() → POST /metering",
    },
    # --- LIVE (partial: MiniMax/CosyVoice/VolcEngine; MiMo excluded) ---
    "metering_snapshot.tts_billed_chars": {
        "status": "LIVE_PARTIAL",
        "source": "TTS generator _generate_one() → TTSResult.billed_chars → Pipeline S6 → POST /metering",
        "coverage": {
            "minimax": "LIVE — 2 × cn_chars (frozen doc: 1 汉字 = 2 计费字符)",
            "cosyvoice": "LIVE — 2 × cn_chars (frozen doc: 阿里云百炼同口径)",
            "volcengine": "LIVE — 1 × cn_chars (direct char billing)",
            "mimo": "NOT_COVERED — token-based billing, truthful billed_chars unavailable",
        },
    },
    # --- LIVE (V3-6: from compute_job_policy → job snapshot → metering_snapshot) ---
    "metering_snapshot.quality_tier": {
        "status": "LIVE",
        "source": "compute_job_policy() → metering_snapshot at create time; settle reads saved tier; current value: 'standard' for all jobs",
    },
}


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if (getattr(user, "role", None) or "user") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@router.get("/summary")
async def credits_shadow_summary(
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict:
    """Aggregate shadow credits overview for admin/maintainer verification.

    Returns bucket stats, ledger stats, job metering coverage, and
    field status (LIVE vs RESERVED).
    """
    _require_admin(user)

    # --- Bucket summary: count + remaining + reserved per type ---
    bucket_result = await db.execute(
        select(
            CreditsBucket.bucket_type,
            func.count().label("count"),
            func.coalesce(func.sum(CreditsBucket.granted), 0).label("total_granted"),
            func.coalesce(func.sum(CreditsBucket.remaining), 0).label("total_remaining"),
            func.coalesce(func.sum(CreditsBucket.reserved), 0).label("total_reserved"),
        ).group_by(CreditsBucket.bucket_type)
    )
    bucket_rows = bucket_result.all()
    bucket_summary = [
        {
            "bucket_type": row.bucket_type,
            "count": row.count,
            "total_granted": row.total_granted,
            "total_remaining": row.total_remaining,
            "total_reserved": row.total_reserved,
        }
        for row in bucket_rows
    ]

    # --- Ledger summary: count per direction ---
    ledger_result = await db.execute(
        select(
            CreditsLedger.direction,
            func.count().label("count"),
        ).group_by(CreditsLedger.direction)
    )
    ledger_rows = ledger_result.all()
    ledger_summary = {row.direction: row.count for row in ledger_rows}
    ledger_total = sum(ledger_summary.values())

    # --- Recent ledger entries (last 10) ---
    recent_result = await db.execute(
        select(
            CreditsLedger.direction,
            CreditsLedger.credits_delta,
            CreditsLedger.balance_after,
            CreditsLedger.related_job_id,
            CreditsLedger.reason_code,
            CreditsLedger.created_at,
        )
        .order_by(CreditsLedger.created_at.desc())
        .limit(10)
    )
    recent_entries = [
        {
            "direction": r.direction,
            "credits_delta": r.credits_delta,
            "balance_after": r.balance_after,
            "related_job_id": r.related_job_id,
            "reason_code": r.reason_code,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in recent_result.all()
    ]

    # --- Job metering coverage ---
    total_jobs_result = await db.execute(select(func.count()).select_from(Job))
    total_jobs = total_jobs_result.scalar() or 0

    has_estimated_result = await db.execute(
        select(func.count()).select_from(Job).where(Job.estimated_minutes.isnot(None))
    )
    has_estimated = has_estimated_result.scalar() or 0

    has_actual_result = await db.execute(
        select(func.count()).select_from(Job).where(Job.actual_minutes.isnot(None))
    )
    has_actual = has_actual_result.scalar() or 0

    has_snapshot_result = await db.execute(
        select(func.count()).select_from(Job).where(Job.metering_snapshot.isnot(None))
    )
    has_snapshot = has_snapshot_result.scalar() or 0

    # credits_estimated / credits_actual coverage (JSONB key presence)
    has_credits_est_result = await db.execute(
        select(func.count()).select_from(Job).where(
            Job.metering_snapshot.op("?")("credits_estimated")
        )
    )
    has_credits_est = has_credits_est_result.scalar() or 0

    has_credits_act_result = await db.execute(
        select(func.count()).select_from(Job).where(
            Job.metering_snapshot.op("?")("credits_actual")
        )
    )
    has_credits_act = has_credits_act_result.scalar() or 0

    metering_summary = {
        "total_jobs": total_jobs,
        "with_estimated_minutes": has_estimated,
        "with_actual_minutes": has_actual,
        "with_metering_snapshot": has_snapshot,
        "with_credits_estimated": has_credits_est,
        "with_credits_actual": has_credits_act,
    }

    # --- Reserve/capture/release closeness check (set-diff) ---
    # Collect distinct job-id sets, then compute the actual difference.
    reserve_ids_result = await db.execute(
        select(func.distinct(CreditsLedger.related_job_id)).where(
            CreditsLedger.direction == "reserve",
            CreditsLedger.related_job_id.isnot(None),
        )
    )
    reserve_job_ids: set[str] = {row[0] for row in reserve_ids_result.all()}

    settle_ids_result = await db.execute(
        select(func.distinct(CreditsLedger.related_job_id)).where(
            CreditsLedger.direction.in_(["capture", "release"]),
            CreditsLedger.related_job_id.isnot(None),
            CreditsLedger.reason_code != "capture_additional",
        )
    )
    settle_job_ids: set[str] = {row[0] for row in settle_ids_result.all()}

    unsettled_ids = reserve_job_ids - settle_job_ids
    closeness = {
        "jobs_with_reserve": len(reserve_job_ids),
        "jobs_with_settle": len(settle_job_ids & reserve_job_ids),
        "jobs_unsettled": len(unsettled_ids),
        "unsettled_job_ids_sample": sorted(unsettled_ids)[:10],
        "note": (
            "healthy — all reserved jobs have capture/release"
            if len(unsettled_ids) == 0
            else f"partial: {len(unsettled_ids)} job(s) have reserve but no capture/release yet (may be in-progress)"
        ),
        "methodology": "set-diff: reserve_job_ids MINUS settle_job_ids (excludes capture_additional)",
    }

    return {
        "buckets": bucket_summary,
        "ledger": {
            "by_direction": ledger_summary,
            "total_entries": ledger_total,
            "recent": recent_entries,
        },
        "metering": metering_summary,
        "reserve_capture_closeness": closeness,
        "field_status": FIELD_STATUS,
    }
