"""S2 审校效果监控 API: 聚合看板 + 单任务详情."""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from auth import get_current_user
from config import settings
from database import async_session
from internal_auth import internal_headers
from models import Job, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-s2-monitor"])

# Job API upstream URL comes from gateway/config.py (settings.job_api_upstream,
# env var AVT_JOB_API_UPSTREAM). Read at call time, not cached at import.
JOBS_STORE_DIR = Path("/opt/aivideotrans/data/jobs")
# Gateway bind-mount path for project data (read-only)
PROJECTS_DATA_DIR = Path("/opt/aivideotrans/data/projects")

ELIGIBLE_STATUSES = {"succeeded", "failed"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _derive_project_dir_from_manifest(manifest_path: str | None) -> str | None:
    """Derive the project directory from Job API manifest_path.

    manifest_path is the container-internal path like:
      /opt/aivideotrans/app/projects/{user_id}/{job_id}/manifest.json
    The gateway reads from the data bind mount:
      /opt/aivideotrans/data/projects/{user_id}/{job_id}/
    """
    if not manifest_path:
        return None
    marker = "/projects/"
    idx = manifest_path.find(marker)
    if idx < 0:
        return None
    relative = manifest_path[idx + len(marker):]
    parts = relative.rstrip("/").split("/")
    if len(parts) >= 2:
        return str(PROJECTS_DATA_DIR / parts[0] / parts[1])
    return None


def _require_admin(user: User | None) -> None:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if getattr(user, "role", None) != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")


def _safe_read_json(path: Path) -> dict | None:
    """Read a JSON file, return None on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _scan_attempt_files(transcript_dir: Path, pass_name: str) -> list[dict]:
    """Scan attempt files for a given pass, e.g. s2_pass1_attempt*.json."""
    results: list[dict] = []
    pattern = re.compile(rf"^s2_{pass_name}_attempt\d+_\w+\.json$")
    if not transcript_dir.is_dir():
        return results
    for f in sorted(transcript_dir.iterdir()):
        if pattern.match(f.name):
            data = _safe_read_json(f)
            if data:
                if "response_text" in data and isinstance(data["response_text"], str):
                    full_len = len(data["response_text"])
                    data["response_text_preview"] = data["response_text"][:500]
                    data["response_text_length"] = full_len
                    del data["response_text"]
                results.append(data)
    return results


def _determine_orchestrator_mode(
    has_pass1: bool, has_pass2: bool, has_review: bool,
) -> str:
    if has_pass1 or has_pass2:
        return "three_pass"
    if has_review:
        return "legacy_or_old"
    return "no_s2_data"


def _extract_job_s2_summary(project_dir: str | None) -> dict | None:
    """Read S2 result artifacts for one job. Returns summary dict or None."""
    if not project_dir:
        return None

    transcript_dir = Path(project_dir) / "transcript"
    if not transcript_dir.is_dir():
        return None

    review = _safe_read_json(transcript_dir / "s2_review_result.json")
    pass1 = _safe_read_json(transcript_dir / "s2_pass1_result.json")
    pass2 = _safe_read_json(transcript_dir / "s2_pass2_result.json")
    pass3 = _safe_read_json(transcript_dir / "s2_pass3_result.json")

    mode = _determine_orchestrator_mode(
        has_pass1=pass1 is not None,
        has_pass2=pass2 is not None,
        has_review=review is not None,
    )

    if mode == "no_s2_data":
        return None

    summary: dict = {"orchestrator_mode": mode}

    # Pass 1 fields
    if pass1:
        summary["pass1_model"] = pass1.get("review_model")
        summary["pass1_skipped"] = pass1.get("skipped", False)
        summary["pass1_model_downgrade"] = pass1.get("fallback_used", False)
        summary["pass1_corrections"] = pass1.get("corrections_applied", 0)
        summary["pass1_sanity"] = pass1.get("sanity_applied", 0)
        summary["pass1_violations"] = len(pass1.get("contract_violations", []))
        summary["pass1_has_audio"] = pass1.get("has_audio", False)
        summary["pass1_duration_ms"] = pass1.get("duration_ms")
        summary["pass1_attempts_count"] = pass1.get("attempts_count")
        summary["pass1_parse_failures"] = pass1.get("parse_failures")
    else:
        summary["pass1_skipped"] = False
        summary["pass1_missing"] = mode == "three_pass"
        summary["pass1_model_downgrade"] = False

    # Pass 2 fields
    if pass2:
        summary["pass2_model"] = pass2.get("review_model")
        summary["pass2_model_downgrade"] = pass2.get("fallback_used", False)
        summary["pass2_corrections"] = pass2.get("corrections_applied", 0)
        glossary = pass2.get("glossary", {})
        summary["pass2_glossary_terms"] = len(glossary) if isinstance(glossary, dict) else 0
        summary["pass2_violations"] = len(pass2.get("contract_violations", []))
        summary["pass2_duration_ms"] = pass2.get("duration_ms")
        summary["pass2_attempts_count"] = pass2.get("attempts_count")
        summary["pass2_parse_failures"] = pass2.get("parse_failures")

    # Pass 3 fields
    if pass3:
        summary["pass3_success"] = True
        profiles = pass3.get("speaker_profiles", {})
        summary["pass3_profiles"] = len(profiles) if isinstance(profiles, dict) else 0
        clips = pass3.get("clips_extracted", [])
        summary["pass3_clips"] = len(clips) if isinstance(clips, list) else 0
        summary["pass3_violations"] = len(pass3.get("contract_violations", []))
        summary["pass3_duration_ms"] = pass3.get("duration_ms")
        summary["pass3_attempts_count"] = pass3.get("attempts_count")
        summary["pass3_parse_failures"] = pass3.get("parse_failures")
    else:
        summary["pass3_success"] = False
        summary["pass3_profiles"] = 0
        summary["pass3_clips"] = 0
        summary["pass3_violations"] = 0

    # Line counts from review result
    if review:
        lc = review.get("line_counts", {})
        summary["lines_before"] = lc.get("original", 0)
        summary["lines_after"] = lc.get("final", 0)
        speakers = review.get("speakers", {})
        summary["speakers_count"] = len(speakers) if isinstance(speakers, dict) else 0

    return summary


def _compute_aggregate(eligible_summaries: list[dict]) -> dict:
    """Compute aggregate stats from eligible job summaries."""
    three_pass = [s for s in eligible_summaries if s.get("orchestrator_mode") == "three_pass"]
    legacy_or_old = [s for s in eligible_summaries if s.get("orchestrator_mode") == "legacy_or_old"]
    no_s2 = [s for s in eligible_summaries if s.get("orchestrator_mode") is None]

    # Pass 1 stats: exclude skipped but include missing (anomaly should be visible)
    p1_skipped = [s for s in three_pass if s.get("pass1_skipped", False)]
    p1_missing = [s for s in three_pass if s.get("pass1_missing", False)]
    p1_jobs = [s for s in three_pass if not s.get("pass1_skipped", False) and not s.get("pass1_missing", False)]
    p1_downgrade = [s for s in p1_jobs if s.get("pass1_model_downgrade", False)]

    p1_models: dict[str, int] = {}
    for s in p1_jobs:
        m = s.get("pass1_model", "unknown")
        p1_models[m] = p1_models.get(m, 0) + 1

    p1_corrections = [s.get("pass1_corrections", 0) for s in p1_jobs]
    p1_sanity = [s.get("pass1_sanity", 0) for s in p1_jobs]
    p1_violations = sum(s.get("pass1_violations", 0) for s in p1_jobs)
    p1_durations = [s["pass1_duration_ms"] for s in p1_jobs if s.get("pass1_duration_ms") is not None]
    p1_parse_failures = sum(s.get("pass1_parse_failures", 0) or 0 for s in p1_jobs)
    p1_attempts = [s["pass1_attempts_count"] for s in p1_jobs if s.get("pass1_attempts_count") is not None]

    # Pass 2 stats (all three_pass + legacy_or_old that have pass2 data)
    p2_jobs = [s for s in eligible_summaries if "pass2_model" in s]
    p2_downgrade = [s for s in p2_jobs if s.get("pass2_model_downgrade", False)]

    p2_models: dict[str, int] = {}
    for s in p2_jobs:
        m = s.get("pass2_model", "unknown")
        p2_models[m] = p2_models.get(m, 0) + 1

    p2_corrections = [s.get("pass2_corrections", 0) for s in p2_jobs]
    p2_glossary = [s.get("pass2_glossary_terms", 0) for s in p2_jobs]
    p2_violations = sum(s.get("pass2_violations", 0) for s in p2_jobs)
    p2_durations = [s["pass2_duration_ms"] for s in p2_jobs if s.get("pass2_duration_ms") is not None]
    p2_parse_failures = sum(s.get("pass2_parse_failures", 0) or 0 for s in p2_jobs)
    p2_attempts = [s["pass2_attempts_count"] for s in p2_jobs if s.get("pass2_attempts_count") is not None]

    # Line change average
    line_changes = []
    for s in eligible_summaries:
        before = s.get("lines_before", 0)
        after = s.get("lines_after", 0)
        if before > 0:
            line_changes.append(after - before)

    # Pass 3 stats
    p3_total = len(three_pass)
    p3_missing = [s for s in three_pass if not s.get("pass3_success", False)]
    p3_profiles = [s.get("pass3_profiles", 0) for s in three_pass if s.get("pass3_success")]
    p3_clips = [s.get("pass3_clips", 0) for s in three_pass if s.get("pass3_success")]
    p3_violations = sum(s.get("pass3_violations", 0) for s in three_pass)
    p3_durations = [s["pass3_duration_ms"] for s in three_pass if s.get("pass3_duration_ms") is not None]
    p3_parse_failures = sum(s.get("pass3_parse_failures", 0) or 0 for s in three_pass)

    def _avg(lst: list) -> float:
        return round(sum(lst) / len(lst), 1) if lst else 0.0

    def _rate(part: int, whole: int) -> float:
        return round(part / whole * 100, 1) if whole > 0 else 0.0

    return {
        "eligible_total": len(eligible_summaries),
        "three_pass_count": len(three_pass),
        "legacy_or_old_count": len(legacy_or_old),
        "no_s2_data_count": len(no_s2),

        "pass1": {
            "total": len(p1_jobs),
            "skipped_count": len(p1_skipped),
            "missing_artifact_count": len(p1_missing),
            "model_downgrade_count": len(p1_downgrade),
            "model_downgrade_rate_pct": _rate(len(p1_downgrade), len(p1_jobs)),
            "avg_corrections": _avg(p1_corrections),
            "avg_sanity_applied": _avg(p1_sanity),
            "total_contract_violations": p1_violations,
            "models_used": p1_models,
            "avg_duration_ms": _avg(p1_durations),
            "total_parse_failures": p1_parse_failures,
            "avg_attempts_to_success": _avg(p1_attempts),
        },
        "pass2": {
            "total": len(p2_jobs),
            "model_downgrade_count": len(p2_downgrade),
            "model_downgrade_rate_pct": _rate(len(p2_downgrade), len(p2_jobs)),
            "avg_corrections": _avg(p2_corrections),
            "avg_glossary_terms": _avg(p2_glossary),
            "avg_line_change": _avg(line_changes),
            "total_contract_violations": p2_violations,
            "models_used": p2_models,
            "avg_duration_ms": _avg(p2_durations),
            "total_parse_failures": p2_parse_failures,
            "avg_attempts_to_success": _avg(p2_attempts),
        },
        "pass3": {
            "total": p3_total,
            "missing_count": len(p3_missing),
            "success_rate_pct": _rate(p3_total - len(p3_missing), p3_total),
            "avg_profiles_generated": _avg(p3_profiles),
            "avg_clips_extracted": _avg(p3_clips),
            "total_contract_violations": p3_violations,
            "avg_duration_ms": _avg(p3_durations),
            "total_parse_failures": p3_parse_failures,
        },
    }


def _compute_daily_trends(eligible_with_summary: list[dict]) -> list[dict]:
    """Group eligible jobs by date and compute per-day metrics."""
    by_date: dict[str, list[dict]] = defaultdict(list)
    for entry in eligible_with_summary:
        created = entry.get("created_at", "")
        if not created:
            continue
        date_str = created[:10]  # "2026-04-10T..." -> "2026-04-10"
        by_date[date_str].append(entry)

    trends: list[dict] = []
    for date_str in sorted(by_date.keys()):
        jobs = by_date[date_str]
        with_data = [j for j in jobs if j.get("orchestrator_mode") is not None]
        three_pass = [j for j in with_data if j.get("orchestrator_mode") == "three_pass"]
        p3_success = [j for j in three_pass if j.get("pass3_success")]
        p2_corrections = [j.get("pass2_corrections", 0) for j in with_data if "pass2_model" in j]

        trends.append({
            "date": date_str,
            "job_count": len(jobs),
            "three_pass_count": len(three_pass),
            "legacy_count": len(with_data) - len(three_pass),
            "pass3_success_rate_pct": round(len(p3_success) / len(three_pass) * 100, 1) if three_pass else None,
            "avg_corrections_p2": round(sum(p2_corrections) / len(p2_corrections), 1) if p2_corrections else 0,
        })

    return trends


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

@router.get("/s2-stats")
async def get_s2_stats(
    user: User | None = Depends(get_current_user),
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    service_mode: str = Query("all"),
    review_model: str = Query(""),
) -> dict:
    """S2 审校效果聚合看板 + 任务摘要列表."""
    _require_admin(user)

    # 1. Fetch all jobs from Job API
    async with httpx.AsyncClient(timeout=15, headers=internal_headers()) as client:
        try:
            resp = await client.get(f"{settings.job_api_upstream}/jobs")
            resp.raise_for_status()
            data = resp.json()
            upstream_jobs: list[dict] = data.get("jobs", data) if isinstance(data, dict) else data
        except Exception as exc:
            logger.error("Failed to fetch jobs from Job API: %s", exc)
            raise HTTPException(status_code=502, detail="无法获取任务列表")

    # 2. DB enrichment (project_dir, status, etc.)
    async with async_session() as db:
        rows = (await db.execute(
            select(Job.job_id, Job.user_id, Job.status, Job.project_dir)
        )).all()

    db_lookup: dict[str, dict] = {}
    for row in rows:
        db_lookup[row.job_id] = {
            "db_status": row.status,
            "project_dir": row.project_dir,
        }

    # 3. Merge + time filter
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    filtered_jobs: list[dict] = []
    for job in upstream_jobs:
        jid = job.get("job_id") or job.get("id", "")
        created_str = job.get("created_at", "")
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            created = datetime.min.replace(tzinfo=timezone.utc)

        if created < cutoff:
            continue

        db_info = db_lookup.get(jid, {})
        status = db_info.get("db_status") or job.get("status", "unknown")
        project_dir = db_info.get("project_dir") or _derive_project_dir_from_manifest(
            job.get("manifest_path")
        )
        filtered_jobs.append({
            "job_id": jid,
            "video_title": job.get("video_title", ""),
            "service_mode": job.get("service_mode", ""),
            "status": status,
            "created_at": created_str,
            "project_dir": project_dir,
        })

    # 3b. service_mode filter (before eligible split)
    if service_mode and service_mode != "all":
        filtered_jobs = [j for j in filtered_jobs if j["service_mode"] == service_mode]

    # 4. Separate eligible vs not-eligible
    eligible_jobs = [j for j in filtered_jobs if j["status"] in ELIGIBLE_STATUSES]
    not_eligible_jobs = [j for j in filtered_jobs if j["status"] not in ELIGIBLE_STATUSES]

    # 5. Read S2 artifacts for all eligible jobs (for aggregate)
    eligible_with_summary: list[dict] = []
    for job in eligible_jobs:
        s2 = _extract_job_s2_summary(job["project_dir"])
        entry = {
            "job_id": job["job_id"],
            "video_title": job["video_title"],
            "service_mode": job["service_mode"],
            "status": job["status"],
            "created_at": job["created_at"],
            "eligible": True,
        }
        if s2:
            entry.update(s2)
        else:
            entry["orchestrator_mode"] = None
            entry["note"] = "无 S2 审校数据"
        eligible_with_summary.append(entry)

    # 5b. review_model filter (needs artifact data, so after extraction)
    if review_model:
        eligible_with_summary = [
            e for e in eligible_with_summary
            if e.get("pass1_model") == review_model
            or e.get("pass2_model") == review_model
        ]

    # 6. Compute aggregate on ALL eligible jobs (before pagination)
    summaries_for_agg = [
        e for e in eligible_with_summary if e.get("orchestrator_mode") is not None
    ]
    no_s2_eligible = [e for e in eligible_with_summary if e.get("orchestrator_mode") is None]
    aggregate = _compute_aggregate(summaries_for_agg)
    aggregate["no_s2_data_count"] = len(no_s2_eligible)

    # 6b. Compute daily trends
    daily_trends = _compute_daily_trends(eligible_with_summary)

    # 6c. Collect filter options for frontend dropdowns
    all_service_modes = sorted(set(j["service_mode"] for j in filtered_jobs if j["service_mode"]))
    all_review_models = sorted(set(
        m for e in eligible_with_summary
        for m in [e.get("pass1_model"), e.get("pass2_model")]
        if m and m != "(skipped)"
    ))

    # 7. Build full job list (eligible + not-eligible), sorted by created_at desc
    not_eligible_entries = [
        {
            "job_id": j["job_id"],
            "video_title": j["video_title"],
            "service_mode": j["service_mode"],
            "status": j["status"],
            "created_at": j["created_at"],
            "eligible": False,
            "orchestrator_mode": None,
            "note": f"状态为 {j['status']}，不参与 S2 质量统计",
        }
        for j in not_eligible_jobs
    ]
    all_entries = eligible_with_summary + not_eligible_entries
    all_entries.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    # 8. Paginate the job list only
    paginated = all_entries[offset:offset + limit]

    # Strip project_dir from response (internal path)
    for entry in paginated:
        entry.pop("project_dir", None)

    return {
        "filter": {
            "days": days, "limit": limit, "offset": offset,
            "service_mode": service_mode, "review_model": review_model,
        },
        "filter_options": {
            "service_modes": all_service_modes,
            "review_models": all_review_models,
        },
        "total_jobs_in_range": len(filtered_jobs),
        "jobs_eligible": len(eligible_jobs),
        "jobs_not_eligible": len(not_eligible_jobs),
        "aggregate": aggregate,
        "daily_trends": daily_trends,
        "jobs": paginated,
    }


@router.get("/s2-stats/{job_id}")
async def get_s2_job_detail(
    job_id: str,
    user: User | None = Depends(get_current_user),
) -> dict:
    """单任务 S2 审校详情: result + attempt 链 + audit + speaker_diff."""
    _require_admin(user)

    # Resolve project_dir: DB first, then manifest_path from Job API
    project_dir: str | None = None
    async with async_session() as db:
        project_dir = (await db.execute(
            select(Job.project_dir).where(Job.job_id == job_id)
        )).scalar_one_or_none()

    if not project_dir:
        async with httpx.AsyncClient(timeout=10, headers=internal_headers()) as client:
            try:
                resp = await client.get(f"{settings.job_api_upstream}/jobs/{job_id}")
                if resp.status_code == 200:
                    job_data = resp.json()
                    project_dir = _derive_project_dir_from_manifest(job_data.get("manifest_path"))
            except Exception:
                pass

    if not project_dir:
        raise HTTPException(status_code=404, detail="任务不存在或无 project_dir")

    transcript_dir = Path(project_dir) / "transcript"
    if not transcript_dir.is_dir():
        raise HTTPException(status_code=404, detail="无转录目录")

    return {
        "job_id": job_id,
        "pass1": {
            "result": _safe_read_json(transcript_dir / "s2_pass1_result.json"),
            "attempts": _scan_attempt_files(transcript_dir, "pass1"),
        },
        "pass2": {
            "result": _safe_read_json(transcript_dir / "s2_pass2_result.json"),
            "attempts": _scan_attempt_files(transcript_dir, "pass2"),
        },
        "pass3": {
            "result": _safe_read_json(transcript_dir / "s2_pass3_result.json"),
            "attempts": _scan_attempt_files(transcript_dir, "pass3"),
        },
        "review_result": _safe_read_json(transcript_dir / "s2_review_result.json"),
        "audit": _safe_read_json(transcript_dir / "s2_review_audit.json"),
        "speaker_diff": _safe_read_json(transcript_dir / "s2_review_speaker_diff.json"),
    }
