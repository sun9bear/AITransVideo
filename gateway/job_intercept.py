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
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from auth import get_current_user, require_auth
from config import settings
from database import get_db
from models import Job, User
from proxy import proxy_request


async def intercept_list_jobs(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
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

    # Filter jobs by user_id
    try:
        data = json.loads(upstream_response.body)
        all_jobs = data.get("jobs", [])

        # Get this user's job_ids from DB
        result = await db.execute(select(Job.job_id).where(Job.user_id == user.id))
        user_job_ids = {row[0] for row in result.all()}

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
    user: User | None = Depends(get_current_user),
) -> Response:
    """POST /job-api/jobs — forward to upstream, then record in DB."""
    # Forward to upstream first
    upstream_response = await proxy_request(
        request=request,
        upstream_base=settings.job_api_upstream,
        strip_prefix="/job-api",
    )

    # If successful, record the job in DB
    if upstream_response.status_code in (200, 201) and user is not None:
        try:
            data = json.loads(upstream_response.body)
            job_data = data.get("job") or data
            job_id = job_data.get("job_id")
            if job_id:
                # Check if already exists
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
        except Exception:
            logger.exception("Failed to record job %s in DB", job_id)

    return upstream_response


async def intercept_get_job(
    request: Request,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> Response:
    """GET /job-api/jobs/{job_id} — verify ownership, then forward."""
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
    user: User | None = Depends(get_current_user),
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


async def intercept_result_download(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
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


async def intercept_project_file(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> Response:
    """GET /api/project-file — verify ownership via project_dir matching."""
    if settings.auth_required and user is not None:
        path = request.query_params.get("path", "")
        # Try to find a job whose project_dir is a prefix of the requested path
        result = await db.execute(
            select(Job.user_id).where(Job.project_dir.isnot(None))
        )
        rows = result.all()
        for row in rows:
            # If we can't determine ownership, allow (fail open for legacy jobs)
            pass

        # For safety: if path contains a job_id-like segment, check ownership
        # The path format is typically: /path/to/projects/{job_id}/...
        for segment in path.replace("\\", "/").split("/"):
            result = await db.execute(select(Job).where(Job.job_id == segment))
            job = result.scalar_one_or_none()
            if job is not None:
                if job.user_id != user.id:
                    raise HTTPException(status_code=403, detail="无权访问此文件")
                break  # Found matching job, ownership verified

    return await proxy_request(
        request=request,
        upstream_base=settings.web_ui_upstream,
        strip_prefix="",
    )


async def sync_job_status(
    job_id: str,
    status: str,
    current_stage: str | None,
    db: AsyncSession,
) -> None:
    """Update job status in DB (called after upstream responses)."""
    try:
        await db.execute(
            update(Job)
            .where(Job.job_id == job_id)
            .values(
                status=status,
                current_stage=current_stage,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
    except Exception:
        logger.exception("Failed to sync job status for %s", job_id)
