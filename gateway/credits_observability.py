"""V3-3 Shadow Observability Baseline — admin-only read surfaces.

Provides a minimal, read-only JSON surface for maintainers to verify that
shadow credits data (buckets, ledger, job metering) is being written correctly
during the V3 pilot period.

Endpoints:
- GET /api/admin/credits/summary           — aggregate overview of buckets, ledger, metering
- GET /api/admin/credits/cost-metrics      — core cost calibration metrics (window param)
- GET /api/admin/credits/provider-breakdown — provider distribution by job default engine
- GET /api/admin/credits/outliers          — outlier jobs: delta, rewrite, unsettled, missing

Auth: admin role required (same pattern as admin_settings.py).

This module does NOT gate job execution, modify data, or replace V2 truth.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import Float, Integer, cast, func, select, String
from sqlalchemy.ext.asyncio import AsyncSession

from admin_auth import _require_admin
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
    "metering_snapshot.rewrite_count": {
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

# Historical production data shows this snapshot key is not consistently
# populated, so admin observability must distinguish snapshot values from
# ledger-derived values instead of treating the JSONB key as complete.
FIELD_STATUS["metering_snapshot.credits_actual"] = {
    "status": "LIVE_PARTIAL",
    "source": "snapshot when populated; otherwise admin views derive actual credits from capture ledger rows",
}
FIELD_STATUS["metering_snapshot.pre_tts_rewrite_events"] = {
    "status": "LIVE_PARTIAL",
    "source": "Pipeline S6 reports structured pre-TTS rewrite audit events when that path fires",
}
FIELD_STATUS["metering_snapshot.harmful_pre_tts_contradiction_count"] = {
    "status": "LIVE_PARTIAL",
    "source": "Pipeline S6 derives harmful pre-TTS contradictions from final alignment outcome",
}
FIELD_STATUS["metering_snapshot.short_segment_needs_review_count"] = {
    "status": "LIVE_PARTIAL",
    "source": "Pipeline S6 reports 2s-8s short-segment needs_review counts",
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def _parse_window(window: str = "7") -> tuple[int, datetime]:
    """Parse window query param → (days, cutoff). Range 1-90, default 7."""
    try:
        days = max(1, min(90, int(window)))
    except (ValueError, TypeError):
        days = 7
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return days, cutoff


def _safe_jsonb_float(col, key):
    """NULLIF(col->>'key', '')::float — safe cast, returns NULL on empty."""
    return cast(func.nullif(col.op("->>")(key), ""), Float)


def _safe_jsonb_int(col, key):
    """NULLIF(col->>'key', '')::int — safe cast, returns NULL on empty."""
    return cast(func.nullif(col.op("->>")(key), ""), Integer)


async def _get_unsettled_job_ids(
    db: AsyncSession,
    cutoff: datetime | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Job IDs with reserve but no capture/release.

    Args:
        cutoff: if given, only consider ledger entries created after this time.
                /summary uses None (full history); /cost-metrics and /outliers
                pass the window cutoff so the result matches the displayed window.

    Returns (reserve_ids, settle_ids, unsettled_ids).
    """
    reserve_where = [
        CreditsLedger.direction == "reserve",
        CreditsLedger.related_job_id.isnot(None),
    ]
    settle_where = [
        CreditsLedger.direction.in_(["capture", "release"]),
        CreditsLedger.related_job_id.isnot(None),
        CreditsLedger.reason_code != "capture_additional",
    ]
    if cutoff is not None:
        reserve_where.append(CreditsLedger.created_at > cutoff)
        settle_where.append(CreditsLedger.created_at > cutoff)

    reserve_result = await db.execute(
        select(func.distinct(CreditsLedger.related_job_id)).where(*reserve_where)
    )
    reserve_ids = {r[0] for r in reserve_result.all()}
    settle_result = await db.execute(
        select(func.distinct(CreditsLedger.related_job_id)).where(*settle_where)
    )
    settle_ids = {r[0] for r in settle_result.all()}
    unsettled_ids = reserve_ids - settle_ids
    return reserve_ids, settle_ids, unsettled_ids


async def _get_credits_actual_source_rollup(
    db: AsyncSession,
    cutoff: datetime | None = None,
) -> dict:
    """Classify actual credits provenance for admin observability."""
    job_where = []
    if cutoff is not None:
        job_where.append(Job.created_at > cutoff)

    job_result = await db.execute(
        select(
            Job.job_id,
            _safe_jsonb_int(Job.metering_snapshot, "credits_actual").label("snapshot_actual"),
        ).where(*job_where)
    )
    jobs = {row.job_id: row.snapshot_actual for row in job_result.all()}

    ledger_where = [
        CreditsLedger.direction == "capture",
        CreditsLedger.related_job_id.isnot(None),
    ]
    if cutoff is not None:
        ledger_where.append(CreditsLedger.created_at > cutoff)

    ledger_result = await db.execute(
        select(
            CreditsLedger.related_job_id.label("job_id"),
            func.coalesce(func.sum(func.abs(CreditsLedger.credits_delta)), 0).label("ledger_actual"),
        )
        .where(*ledger_where)
        .group_by(CreditsLedger.related_job_id)
    )
    ledger_by_job = {
        row.job_id: int(row.ledger_actual or 0)
        for row in ledger_result.all()
        if row.job_id
    }

    source_counts = {"snapshot": 0, "ledger_derived": 0, "missing": 0}
    snapshot_sum = 0
    ledger_derived_sum = 0
    effective_sum = 0
    for job_id, snapshot_actual in jobs.items():
        if snapshot_actual is not None:
            source_counts["snapshot"] += 1
            value = int(snapshot_actual)
            snapshot_sum += value
            effective_sum += value
        elif ledger_by_job.get(job_id):
            source_counts["ledger_derived"] += 1
            value = int(ledger_by_job[job_id])
            ledger_derived_sum += value
            effective_sum += value
        else:
            source_counts["missing"] += 1

    return {
        "source_counts": source_counts,
        "snapshot_sum": snapshot_sum,
        "ledger_derived_sum": ledger_derived_sum,
        "effective_sum": effective_sum,
        "ledger_capture_jobs": len(ledger_by_job),
        "methodology": (
            "snapshot if metering_snapshot.credits_actual exists; else "
            "sum(abs(capture.credits_delta)) by job; else missing"
        ),
    }


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
    reserve_job_ids, settle_job_ids, unsettled_ids = await _get_unsettled_job_ids(db)
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
    credits_actual_source = await _get_credits_actual_source_rollup(db)

    return {
        "buckets": bucket_summary,
        "ledger": {
            "by_direction": ledger_summary,
            "total_entries": ledger_total,
            "recent": recent_entries,
        },
        "metering": metering_summary,
        "credits_actual_source": credits_actual_source,
        "reserve_capture_closeness": closeness,
        "field_status": FIELD_STATUS,
    }


@router.get("/cost-metrics")
async def credits_cost_metrics(
    window: str = "7",
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict:
    """Core cost calibration metrics for shadow credits."""
    _require_admin(user)
    days, cutoff = _parse_window(window)
    window_filter = Job.created_at > cutoff
    has_snapshot = Job.metering_snapshot.isnot(None)

    # jobs total
    total_result = await db.execute(
        select(func.count()).select_from(Job).where(window_filter)
    )
    jobs_total = total_result.scalar() or 0

    # credits sums
    credits_result = await db.execute(
        select(
            func.coalesce(func.sum(_safe_jsonb_int(Job.metering_snapshot, "credits_estimated")), 0).label("est"),
            func.coalesce(func.sum(_safe_jsonb_int(Job.metering_snapshot, "credits_actual")), 0).label("act"),
        ).where(window_filter, has_snapshot)
    )
    row = credits_result.one()
    est_sum = int(row.est)
    act_sum = int(row.act)
    delta_pct = round(float((est_sum - act_sum) / act_sum * 100), 1) if act_sum else None

    # K 值（final_cn_chars / actual_minutes）— 百分位数
    k_expr = _safe_jsonb_float(Job.metering_snapshot, "final_cn_chars") / Job.actual_minutes
    k_filter = [
        window_filter,
        has_snapshot,
        Job.actual_minutes > 0,
        Job.metering_snapshot.op("->>")("final_cn_chars").isnot(None),
    ]
    k_result = await db.execute(
        select(
            func.avg(k_expr).label("avg"),
            func.percentile_cont(0.5).within_group(k_expr).label("p50"),
            func.percentile_cont(0.75).within_group(k_expr).label("p75"),
            func.percentile_cont(0.9).within_group(k_expr).label("p90"),
        ).where(*k_filter)
    )
    k_row = k_result.one()
    k_actual = {
        "avg": round(float(k_row.avg), 0) if k_row.avg else None,
        "p50": round(float(k_row.p50), 0) if k_row.p50 else None,
        "p75": round(float(k_row.p75), 0) if k_row.p75 else None,
        "p90": round(float(k_row.p90), 0) if k_row.p90 else None,
    }

    # Rewrite 率
    rewrite_result = await db.execute(
        select(
            func.count().label("total"),
            func.count().filter(
                Job.metering_snapshot.op("->>")("rewrite_triggered") == "true"
            ).label("with_rewrite"),
            func.avg(_safe_jsonb_int(Job.metering_snapshot, "rewrite_count")).label("avg_count"),
        ).where(window_filter, has_snapshot)
    )
    rw = rewrite_result.one()
    rewrite_rate_pct = round(float(rw.with_rewrite / rw.total * 100), 1) if rw.total else None
    rewrite_count_avg = round(float(rw.avg_count), 1) if rw.avg_count else None

    # 模式分布（使用 Job.service_mode 顶级列，有索引）
    mode_result = await db.execute(
        select(
            Job.service_mode,
            func.count().label("count"),
        ).where(window_filter).group_by(Job.service_mode)
    )
    service_mode_dist = {r.service_mode or "unknown": r.count for r in mode_result.all()}

    # TTS 计费字符覆盖率
    tts_result = await db.execute(
        select(
            func.count().label("total"),
            func.count().filter(
                Job.metering_snapshot.op("->>")("tts_billed_chars").isnot(None)
            ).label("with_tts"),
        ).where(window_filter, has_snapshot)
    )
    tts_row = tts_result.one()
    tts_coverage_pct = round(float(tts_row.with_tts / tts_row.total * 100), 1) if tts_row.total else None

    # 未闭环 job 数（按时间窗口过滤）
    _, _, unsettled = await _get_unsettled_job_ids(db, cutoff=cutoff)
    jobs_unsettled = len(unsettled)
    credits_actual_source = await _get_credits_actual_source_rollup(db, cutoff=cutoff)
    effective_actual_sum = int(credits_actual_source["effective_sum"])
    effective_delta_pct = (
        round(float((est_sum - effective_actual_sum) / effective_actual_sum * 100), 1)
        if effective_actual_sum
        else None
    )

    return {
        "window_days": days,
        "jobs_total": jobs_total,
        "credits_estimated_sum": est_sum,
        "credits_actual_sum": act_sum,
        "credits_actual_effective_sum": effective_actual_sum,
        "credits_actual_source": credits_actual_source,
        "estimate_actual_delta_pct": delta_pct,
        "estimate_effective_delta_pct": effective_delta_pct,
        "k_actual": k_actual,
        "rewrite_rate_pct": rewrite_rate_pct,
        "rewrite_count_avg": rewrite_count_avg,
        "service_mode_dist": service_mode_dist,
        "tts_billed_chars_coverage_pct": tts_coverage_pct,
        "jobs_unsettled": jobs_unsettled,
    }


@router.get("/provider-breakdown")
async def credits_provider_breakdown(
    window: str = "7",
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict:
    """Provider distribution by job default engine."""
    _require_admin(user)
    days, cutoff = _parse_window(window)

    # 使用 Job.tts_provider / Job.tts_model 顶级列（有索引，比 JSONB 取值快）
    # 注意：这是 job 创建时的默认 provider，非实际执行 provider
    result = await db.execute(
        select(
            Job.tts_provider.label("provider"),
            Job.tts_model.label("model"),
            func.count().label("job_count"),
            func.coalesce(func.sum(Job.actual_minutes), 0).label("total_minutes"),
            func.coalesce(func.sum(_safe_jsonb_int(Job.metering_snapshot, "tts_billed_chars")), 0).label("total_billed_chars"),
            func.avg(
                _safe_jsonb_float(Job.metering_snapshot, "tts_billed_chars") / func.nullif(Job.actual_minutes, 0)
            ).label("avg_billed_per_min"),
            func.avg(
                _safe_jsonb_float(Job.metering_snapshot, "credits_actual") / func.nullif(Job.actual_minutes, 0)
            ).label("avg_credits_per_min"),
        )
        .where(
            Job.created_at > cutoff,
            Job.metering_snapshot.isnot(None),
        )
        .group_by(Job.tts_provider, Job.tts_model)
        .order_by(func.count().desc())
    )

    providers = []
    for r in result.all():
        providers.append({
            "provider": r.provider or "unknown",
            "model": r.model or "unknown",
            "job_count": r.job_count,
            "total_minutes": round(float(r.total_minutes), 1) if r.total_minutes else 0,
            "total_billed_chars": int(r.total_billed_chars) if r.total_billed_chars else 0,
            "avg_billed_per_min": round(float(r.avg_billed_per_min), 0) if r.avg_billed_per_min else None,
            "avg_credits_per_min": round(float(r.avg_credits_per_min), 1) if r.avg_credits_per_min else None,
        })

    return {
        "window_days": days,
        "providers": providers,
    }


@router.get("/outliers")
async def credits_outliers(
    window: str = "7",
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> dict:
    """Outlier jobs: biggest estimate/actual delta, most rewrites, unsettled, missing fields."""
    _require_admin(user)
    days, cutoff = _parse_window(window)
    window_filter = Job.created_at > cutoff
    has_snapshot = Job.metering_snapshot.isnot(None)

    est_col = _safe_jsonb_int(Job.metering_snapshot, "credits_estimated")
    act_col = _safe_jsonb_int(Job.metering_snapshot, "credits_actual")

    # 1. 预估/实扣偏差最大 Top 10
    delta_result = await db.execute(
        select(
            Job.job_id,
            Job.title,
            Job.service_mode,
            est_col.label("credits_estimated"),
            act_col.label("credits_actual"),
            (est_col - act_col).label("delta"),
            Job.actual_minutes,
        )
        .where(window_filter, has_snapshot, est_col.isnot(None), act_col.isnot(None))
        .order_by(func.abs(est_col - act_col).desc())
        .limit(10)
    )
    estimate_actual_outliers = [
        {
            "job_id": r.job_id,
            "title": r.title or "",
            "service_mode": r.service_mode or "",
            "credits_estimated": r.credits_estimated,
            "credits_actual": r.credits_actual,
            "delta": r.delta,
            "actual_minutes": round(float(r.actual_minutes), 1) if r.actual_minutes else None,
        }
        for r in delta_result.all()
    ]

    # 2. Rewrite 次数最多 Top 10
    rw_col = _safe_jsonb_int(Job.metering_snapshot, "rewrite_count")
    rw_result = await db.execute(
        select(
            Job.job_id,
            Job.title,
            rw_col.label("rewrite_count"),
            Job.actual_minutes,
        )
        .where(window_filter, has_snapshot, rw_col.isnot(None))
        .order_by(rw_col.desc())
        .limit(10)
    )
    rewrite_top = [
        {
            "job_id": r.job_id,
            "title": r.title or "",
            "rewrite_count": int(r.rewrite_count) if r.rewrite_count else None,
            "actual_minutes": round(float(r.actual_minutes), 1) if r.actual_minutes else None,
        }
        for r in rw_result.all()
    ]

    # 3. 未闭环 jobs（按时间窗口过滤）
    _, _, unsettled = await _get_unsettled_job_ids(db, cutoff=cutoff)

    # 4. metering 缺字段 jobs（有 metering_snapshot 但缺关键字段）
    missing_result = await db.execute(
        select(
            Job.job_id,
            Job.metering_snapshot.op("->>")("final_cn_chars").label("final_cn_chars"),
            Job.metering_snapshot.op("->>")("credits_actual").label("credits_actual"),
        )
        .where(
            window_filter,
            has_snapshot,
            (Job.metering_snapshot.op("->>")("final_cn_chars").is_(None))
            | (Job.metering_snapshot.op("->>")("credits_actual").is_(None)),
        )
    )
    missing_fields_jobs = []
    for r in missing_result.all():
        missing = []
        if r.final_cn_chars is None:
            missing.append("final_cn_chars")
        if r.credits_actual is None:
            missing.append("credits_actual")
        missing_fields_jobs.append({"job_id": r.job_id, "missing": missing})

    return {
        "window_days": days,
        "estimate_actual_outliers": estimate_actual_outliers,
        "rewrite_top": rewrite_top,
        "unsettled_jobs": sorted(unsettled)[:20],
        "missing_fields_jobs": missing_fields_jobs,
    }
