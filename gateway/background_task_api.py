"""Gateway-native REST API for background export tasks.

Endpoints (all require auth + job ownership):
- POST /api/jobs/{job_id}/tasks                         — create task
- GET  /api/jobs/{job_id}/tasks/{task_id}               — query task
- GET  /api/jobs/{job_id}/tasks/latest?type=...&fingerprint=...  — restore state
- GET  /api/jobs/{job_id}/tasks/{task_id}/download      — download zip (materials_pack only)

Router must be registered BEFORE any job-api proxy catch-all so that
``/api/jobs/...`` requests land here, not at the Job API on :8877.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import background_task_queue as queue
from auth import require_auth
from background_task_executors import TASK_EXECUTORS
from database import async_session, get_db
from models import Job, User

logger = logging.getLogger(__name__)

# P1-13 (audit 2026-05-07): cap concurrent background tasks at 2 to
# prevent N users simultaneously clicking "generate video" /
# "materials pack" from spawning N parallel ffmpeg processes
# (each consuming 1-2 CPU cores + 0.5-2 GB RAM). With ~4 cores in
# typical deploy, anything > 2 starves polling requests for 5-10s.
# Semaphore is module-level so it persists across requests; the
# event loop is single-threaded so no separate lock needed.
_BACKGROUND_TASK_SEMAPHORE = asyncio.Semaphore(2)

router = APIRouter()


# ---- request/response models ------------------------------------------------

class CreateTaskRequest(BaseModel):
    task_type: str = Field(..., description="materials_pack | generate_video")
    params: dict[str, Any] = Field(default_factory=dict)


# ---- helpers ----------------------------------------------------------------

async def _require_job_ownership(
    db: AsyncSession,
    *,
    job_id: str,
    user: User,
) -> Job:
    result = await db.execute(select(Job).where(Job.job_id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.user_id != user.id and getattr(user, "role", "user") != "admin":
        raise HTTPException(status_code=403, detail="无权访问")
    return job


def _resolve_project_dir(job: Job) -> Path:
    if not job.project_dir:
        raise HTTPException(status_code=404, detail="项目目录不存在")
    project_dir = Path(job.project_dir)
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail="项目目录不存在")
    return project_dir


def _launch_executor(
    *,
    task_type: str,
    task_id: str,
    job_id: str,
    project_dir: Path,
    params: dict[str, Any],
) -> None:
    executor = TASK_EXECUTORS.get(task_type)
    if executor is None:
        # Should have been caught at validation, but defensive-guard the
        # background thread entry too.
        raise HTTPException(status_code=400, detail=f"未知任务类型: {task_type}")

    # P1-13 (audit 2026-05-07): gate concurrent ffmpeg spawns. The
    # executor coroutine is wrapped so that no more than 2 background
    # tasks run their ffmpeg / heavy work at once across the whole
    # gateway process. Tasks beyond the cap are queued (still scheduled
    # on the loop) and acquire as slots free up.
    executor_coro = executor(
        task_id=task_id,
        job_id=job_id,
        project_dir=project_dir,
        params=params,
    )

    async def _gated_executor() -> None:
        async with _BACKGROUND_TASK_SEMAPHORE:
            await executor_coro

    asyncio.create_task(
        _gated_executor(),
        name=f"bgtask-{task_type}-{task_id}",
    )


# ---- endpoints --------------------------------------------------------------

@router.post("/api/jobs/{job_id}/tasks")
async def create_task_endpoint(
    job_id: str,
    payload: CreateTaskRequest,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    if payload.task_type not in TASK_EXECUTORS:
        raise HTTPException(status_code=400, detail=f"未知任务类型: {payload.task_type}")

    job = await _require_job_ownership(db, job_id=job_id, user=user)
    project_dir = _resolve_project_dir(job)

    # Task ownership follows the JOB, not the caller. This matters because
    # ``_require_job_ownership`` lets admins access any job: if an admin
    # triggers materials_pack or generate_video on a user's behalf, the
    # task must still be readable by the job owner on next page load.
    # Storing ``user_id=user.id`` (caller) would hide it from the owner and
    # make dedupe-vs-restore inconsistent (the dedupe key ignores user_id
    # so the owner's retry would collide but their /latest lookup would
    # return null).
    task_id, created = await queue.create_task(
        db,
        job_id=job_id,
        user_id=job.user_id,
        task_type=payload.task_type,
        params=payload.params,
    )
    await db.commit()

    if created:
        _launch_executor(
            task_type=payload.task_type,
            task_id=task_id,
            job_id=job_id,
            project_dir=project_dir,
            params=payload.params,
        )

    # Unscoped fetch is safe here because we just confirmed job ownership
    # above and we validate task.job_id below whenever needed.
    task = await queue.get_task(db, task_id=task_id)
    return {"task_id": task_id, "created": created, "task": task}


@router.get("/api/jobs/{job_id}/tasks/latest")
async def latest_task_endpoint(
    job_id: str,
    type: str = Query(..., description="task_type filter"),
    fingerprint: str | None = Query(None, description="params fingerprint"),
    active_only: bool = Query(
        False,
        description=(
            "If true, return only pending/running tasks. "
            "Default false returns latest task of any status so the UI can "
            "restore 'completed → downloadable' state after page refresh."
        ),
    ),
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any] | None:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    job = await _require_job_ownership(db, job_id=job_id, user=user)

    if type not in TASK_EXECUTORS:
        raise HTTPException(status_code=400, detail=f"未知任务类型: {type}")

    # Scope by the JOB's owner — see rationale on create_task_endpoint.
    # This lets both the owner and admins see the same task.
    lookup_kwargs = dict(
        job_id=job_id,
        user_id=job.user_id,
        task_type=type,
        params_fingerprint=fingerprint,
    )
    if active_only:
        task = await queue.get_latest_active(db, **lookup_kwargs)
    else:
        task = await queue.get_latest(db, **lookup_kwargs)
    return task  # nullable


@router.get("/api/jobs/{job_id}/tasks/{task_id}")
async def get_task_endpoint(
    job_id: str,
    task_id: str,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    await _require_job_ownership(db, job_id=job_id, user=user)

    # No user_id filter — job ownership was verified above, and we gate on
    # task.job_id below to prevent cross-job task_id lookups.
    task = await queue.get_task(db, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("job_id") != job_id:
        raise HTTPException(status_code=404, detail="任务不属于该 job")
    return task


@router.get("/api/jobs/{job_id}/tasks/{task_id}/download")
async def download_task_artifact(
    job_id: str,
    task_id: str,
    user: User | None = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    job = await _require_job_ownership(db, job_id=job_id, user=user)
    project_dir = _resolve_project_dir(job)

    task = await queue.get_task(db, task_id=task_id)
    if task is None or task.get("job_id") != job_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get("task_type") != "materials_pack":
        raise HTTPException(status_code=400, detail="此任务类型不支持下载")
    if task.get("status") != "completed":
        raise HTTPException(status_code=409, detail="任务未完成")

    result = task.get("result") or {}
    zip_path_str = result.get("zip_path")
    if not zip_path_str:
        raise HTTPException(status_code=404, detail="产物路径缺失")

    zip_path = Path(zip_path_str)
    # Safety: must be inside project_dir
    try:
        zip_path.resolve(strict=False).relative_to(project_dir.resolve(strict=False))
    except ValueError:
        raise HTTPException(status_code=400, detail="产物路径越界")

    if not zip_path.is_file():
        raise HTTPException(status_code=404, detail="产物文件已不存在")

    filename = str(result.get("filename") or zip_path.name)
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=filename,
    )


__all__ = ["router"]
