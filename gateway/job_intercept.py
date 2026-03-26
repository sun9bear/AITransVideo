"""Job API intercept layer — inject user_id, filter by ownership.

Gateway intercepts job-related requests to:
1. Inject user_id when creating a job
2. Filter job listings by user_id
3. Verify job ownership for single-job operations
4. Sync job metadata to PostgreSQL (dual-write)

The upstream Job API (8877) and Web UI (8876) remain unchanged.
"""

from __future__ import annotations

import json
import logging
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from auth import require_auth
from config import settings
from database import get_db
from models import Job, User
from proxy import proxy_request


async def intercept_list_jobs(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET /job-api/jobs — forward to upstream, then filter by user_id."""
    upstream_response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )

    # If auth not required or no user, return as-is
    if not settings.auth_required or user is None:
        return upstream_response

    # Filter jobs by user_id, with auto-reconciliation for orphan jobs
    try:
        data = json.loads(upstream_response.body)
        all_jobs = data.get("jobs", [])

        # Get all job_ids in DB (any user)
        result_all = await db.execute(select(Job.job_id))
        all_db_job_ids = {row[0] for row in result_all.all()}

        # Get this user's job_ids
        result_user = await db.execute(select(Job.job_id).where(Job.user_id == user.id))
        user_job_ids = {row[0] for row in result_user.all()}

        # Log orphan jobs but do NOT auto-claim
        orphan_ids = [j.get("job_id") for j in all_jobs if j.get("job_id") and j.get("job_id") not in all_db_job_ids]
        if orphan_ids:
            print(f"[GATEWAY] ⚠ {len(orphan_ids)} orphan job(s) not in DB: {orphan_ids[:5]}", flush=True)

        # Sync status from upstream to DB for this user's jobs
        upstream_by_id = {j.get("job_id"): j for j in all_jobs if j.get("job_id")}
        for jid in user_job_ids:
            upstream_job = upstream_by_id.get(jid)
            if upstream_job:
                upstream_status = upstream_job.get("status", "")
                upstream_stage = upstream_job.get("current_stage")
                try:
                    await db.execute(
                        select(Job).where(Job.job_id == jid)  # just to trigger lazy load
                    )
                    from sqlalchemy import update
                    await db.execute(
                        update(Job).where(Job.job_id == jid).values(
                            status=upstream_status,
                            current_stage=upstream_stage,
                        )
                    )
                except Exception:
                    pass
        try:
            await db.commit()
        except Exception:
            await db.rollback()

        # Only return jobs that belong to this user in DB
        filtered_jobs = [j for j in all_jobs if j.get("job_id") in user_job_ids]
        print(f"[GATEWAY] list_jobs: upstream={len(all_jobs)}, db_user={len(user_job_ids)}, returning={len(filtered_jobs)}", flush=True)
        data["jobs"] = filtered_jobs

        return Response(
            content=json.dumps(data, ensure_ascii=False),
            status_code=200,
            headers={"content-type": "application/json"},
        )
    except Exception as exc:
        import traceback
        print(f"[GATEWAY] ❌ Failed to filter jobs: {exc}", flush=True)
        print(f"[GATEWAY] ❌ Traceback: {traceback.format_exc()}", flush=True)
        return upstream_response


FREE_USER_MAX_CONCURRENT = 1
PLUS_USER_MAX_CONCURRENT = 3
PRO_USER_MAX_CONCURRENT = 10


async def intercept_create_job(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """POST /job-api/jobs — check per-user concurrency, forward to upstream, record in DB."""
    # Per-user concurrency check
    if user:
        active_count_result = await db.execute(
            select(func.count()).where(
                Job.user_id == user.id,
                Job.status.in_(["queued", "running", "waiting_for_review"]),
            )
        )
        active_count = active_count_result.scalar() or 0
        # TODO: check user tier for different limits
        max_concurrent = FREE_USER_MAX_CONCURRENT
        if active_count >= max_concurrent:
            return Response(
                content=json.dumps({
                    "detail": f"当前有未完成的任务（{active_count}个），请先完成或取消后再创建新翻译。"
                }, ensure_ascii=False),
                status_code=409,
                headers={"content-type": "application/json"},
            )

    # Forward to upstream
    upstream_response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )

    # If successful, record the job in DB
    job_id = None
    print(f"[GATEWAY] intercept_create_job: upstream status={upstream_response.status_code}, user={user.id if user else None}", flush=True)
    if upstream_response.status_code in (200, 201, 202) and user is not None:
        try:
            raw_body = upstream_response.body
            print(f"[GATEWAY] intercept_create_job: response body length={len(raw_body)}, first 200 chars={raw_body[:200]}", flush=True)
            data = json.loads(raw_body)
            # Upstream may return {"job": {...}} or {"job_id": "...", ...}
            job_data = data.get("job") or data
            job_id = job_data.get("job_id")
            print(f"[GATEWAY] intercept_create_job: parsed job_id={job_id}", flush=True)
            if job_id:
                existing = await db.execute(select(Job).where(Job.job_id == job_id))
                if existing.scalar_one_or_none() is None:
                    job = Job(
                        job_id=job_id,
                        user_id=user.id,
                        source_type=job_data.get("source_type", "youtube_url"),
                        source_ref=job_data.get("youtube_url") or job_data.get("source_ref", ""),
                        title=job_data.get("title", ""),
                        speakers=job_data.get("speakers", "auto"),
                        status=job_data.get("status", "queued"),
                        current_stage=job_data.get("current_stage"),
                        project_dir=job_data.get("project_dir"),
                    )
                    db.add(job)
                    await db.commit()
                    print(f"[GATEWAY] ✅ Job {job_id} recorded in DB for user {user.id}", flush=True)
                else:
                    print(f"[GATEWAY] Job {job_id} already in DB, skipping", flush=True)
            else:
                print(f"[GATEWAY] ⚠ No job_id in upstream response. Keys: {list(job_data.keys())[:10]}", flush=True)
        except Exception as exc:
            import traceback
            print(f"[GATEWAY] ❌ Failed to record job {job_id} in DB: {exc}", flush=True)
            print(f"[GATEWAY] ❌ Traceback: {traceback.format_exc()}", flush=True)
            try:
                await db.rollback()
            except Exception:
                pass
    elif upstream_response.status_code not in (200, 201, 202):
        print(f"[GATEWAY] intercept_create_job: upstream rejected with status {upstream_response.status_code}", flush=True)

    return upstream_response


async def intercept_get_job(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET /job-api/jobs/{job_id} — verify ownership, then forward. No auto-claim."""
    await _verify_job_ownership(job_id, db, user)
    return await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )


async def intercept_job_subresource(
    request: Request,
    job_id: str,
    subpath: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET/POST /job-api/jobs/{job_id}/{subpath} — verify ownership, then forward.

    Covers: logs, artifacts, result-summary, continue, etc.
    """
    await _verify_job_ownership(job_id, db, user)
    return await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )


async def _verify_job_ownership(
    job_id: str,
    db: AsyncSession,
    user: User | None,
) -> None:
    """Check that authenticated user owns the job. Raises 403 if not."""
    if not settings.auth_required or user is None:
        return
    result = await db.execute(
        select(Job).where(Job.job_id == job_id, Job.user_id == user.id)
    )
    if result.scalar_one_or_none() is None:
        result2 = await db.execute(select(Job).where(Job.job_id == job_id))
        if result2.scalar_one_or_none() is not None:
            raise HTTPException(status_code=403, detail="无权访问此任务")
        else:
            logger.warning("Job %s not found in DB — allowing access (legacy job?)", job_id)


async def intercept_result_download(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET /api/result-download — verify job ownership before proxying."""
    job_id = request.query_params.get("job_id")
    if job_id:
        await _verify_job_ownership(job_id, db, user)

    return await proxy_request(
        request=request,
        upstream_base=settings.web_ui_upstream,
        strip_prefix="",
    )


async def intercept_delete_job(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """POST /api/job/delete — verify ownership, proxy to web_ui, then clean PostgreSQL."""
    # Parse job_id from request body
    body = await request.body()
    job_id = None
    try:
        data = json.loads(body)
        job_id = data.get("job_id", "").strip()
    except Exception:
        pass

    # Verify ownership if we have a job_id
    if job_id and settings.auth_required and user is not None:
        await _verify_job_ownership(job_id, db, user)

    # Forward to upstream web_ui
    upstream_response = await proxy_request(
        request=request,
        upstream_base=settings.web_ui_upstream,
        strip_prefix="",
    )

    # If upstream succeeded, also remove from PostgreSQL
    if upstream_response.status_code == 200 and job_id:
        try:
            await db.execute(delete(Job).where(Job.job_id == job_id))
            await db.commit()
            logger.info("Deleted job %s from PostgreSQL", job_id)
        except Exception:
            logger.exception("Failed to delete job %s from PostgreSQL", job_id)

    return upstream_response


async def intercept_project_file(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET /api/project-file — verify ownership via path segment matching.

    Security: fail-closed — if no job_id segment matches, deny access.
    """
    if settings.auth_required and user is not None:
        path = request.query_params.get("path", "")
        if not path:
            raise HTTPException(status_code=400, detail="缺少 path 参数")

        # Collect non-empty path segments, then batch-query DB
        segments = [s for s in path.replace("\\", "/").split("/") if s]
        if not segments:
            raise HTTPException(status_code=403, detail="无法验证文件归属，拒绝访问")

        result = await db.execute(
            select(Job.job_id, Job.user_id).where(Job.job_id.in_(segments))
        )
        matched_jobs = result.all()

        ownership_verified = False
        for job_id, owner_id in matched_jobs:
            if owner_id != user.id:
                raise HTTPException(status_code=403, detail="无权访问此文件")
            ownership_verified = True
            break

        # Fail-closed: no matching job_id found → deny
        if not ownership_verified:
            raise HTTPException(status_code=403, detail="无法验证文件归属，拒绝访问")

    return await proxy_request(
        request=request,
        upstream_base=settings.web_ui_upstream,
        strip_prefix="",
    )


