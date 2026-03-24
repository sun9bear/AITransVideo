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
from sqlalchemy import delete, select
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

        # Auto-reconcile: if upstream has jobs not in DB at all, claim them for current user
        for j in all_jobs:
            jid = j.get("job_id")
            if jid and jid not in all_db_job_ids:
                try:
                    job = Job(
                        job_id=jid,
                        user_id=user.id,
                        source_type=j.get("source_type", "youtube_url"),
                        source_ref=j.get("youtube_url") or j.get("source_ref", ""),
                        title=j.get("title", ""),
                        speakers=j.get("speakers", "auto"),
                        status=j.get("status", "queued"),
                        current_stage=j.get("current_stage"),
                        project_dir=j.get("project_dir"),
                    )
                    db.add(job)
                    await db.commit()
                    user_job_ids.add(jid)
                    logger.info("Auto-reconciled orphan job %s → user %s", jid, user.id)
                except Exception:
                    logger.exception("Failed to auto-reconcile job %s", jid)
                    await db.rollback()

        filtered_jobs = [j for j in all_jobs if j.get("job_id") in user_job_ids]
        data["jobs"] = filtered_jobs

        return Response(
            content=json.dumps(data, ensure_ascii=False),
            status_code=200,
            headers={"content-type": "application/json"},
        )
    except Exception:
        logger.exception("Failed to filter jobs by user_id")
        return upstream_response


async def intercept_create_job(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """POST /job-api/jobs — forward to upstream, then record in DB."""
    # Forward to upstream first
    upstream_response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )

    # If successful, record the job in DB
    job_id = None
    logger.info("intercept_create_job: upstream status=%s, user=%s", upstream_response.status_code, user.id if user else None)
    if upstream_response.status_code in (200, 201, 202) and user is not None:
        try:
            data = json.loads(upstream_response.body)
            # Upstream may return {"job": {...}} or {"job_id": "...", ...}
            job_data = data.get("job") or data
            job_id = job_data.get("job_id")
            logger.info("intercept_create_job: parsed job_id=%s from upstream response", job_id)
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
                    logger.info("intercept_create_job: recorded job %s for user %s", job_id, user.id)
                else:
                    logger.info("intercept_create_job: job %s already in DB", job_id)
            else:
                logger.warning("intercept_create_job: no job_id in upstream response: %s", list(job_data.keys())[:10])
        except Exception:
            logger.exception("Failed to record job %s in DB", job_id)
    elif upstream_response.status_code not in (200, 201, 202):
        logger.info("intercept_create_job: upstream rejected with status %s", upstream_response.status_code)

    return upstream_response


async def intercept_get_job(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_auth),
) -> Response:
    """GET /job-api/jobs/{job_id} — auto-reconcile if missing, verify ownership, then forward."""
    # Auto-reconcile: if job_id not in DB but user is authenticated, claim it
    if user is not None:
        try:
            existing = await db.execute(select(Job).where(Job.job_id == job_id))
            if existing.scalar_one_or_none() is None:
                # Forward first to get job data, then record
                upstream_response = await proxy_request(
                    request=request,
                    upstream_base=settings.job_api_upstream,
                    strip_prefix="/job-api",
                )
                if upstream_response.status_code == 200:
                    try:
                        job_data = json.loads(upstream_response.body)
                        job = Job(
                            job_id=job_id,
                            user_id=user.id,
                            source_type=job_data.get("source_type", "youtube_url"),
                            source_ref=job_data.get("source_ref", ""),
                            title=job_data.get("title", ""),
                            speakers=job_data.get("speakers", "auto"),
                            status=job_data.get("status", "running"),
                            current_stage=job_data.get("current_stage"),
                            project_dir=job_data.get("project_dir"),
                        )
                        db.add(job)
                        await db.commit()
                        logger.info("Auto-reconciled job %s via get_job for user %s", job_id, user.id)
                    except Exception:
                        await db.rollback()
                        logger.exception("Failed to auto-reconcile job %s via get_job", job_id)
                return upstream_response
        except Exception:
            logger.exception("Error in get_job auto-reconcile check for %s", job_id)

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


