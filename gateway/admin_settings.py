"""Admin settings API: read/write global platform configuration + job management."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete as sa_delete

from auth import get_current_user
from config import settings as app_settings
from database import async_session
from models import Job, User

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path("/opt/aivideotrans/config/admin_settings.json")

router = APIRouter(prefix="/api/admin", tags=["admin"])


# --- Settings schema ---

class AdminSettings(BaseModel):
    tts_provider: str = "minimax"          # "minimax" or "mimo"
    review_model: str = "gemini"           # "gemini" or "mimo_omni"
    translation_model: str = "deepseek"    # "deepseek" or "gemini"
    skip_translation_config_for_users: bool = True  # Normal users skip translation config step
    skip_all_reviews_for_free_users: bool = True   # Free users: fully automatic pipeline
    free_user_max_duration_minutes: float = 10.0   # Max video duration for free users (minutes)
    enable_pre_tts_rewrite: bool = True            # Pre-TTS rewrite to match target duration


# --- Helpers ---

def _is_admin(user: User) -> bool:
    """Simple admin check: email == 'admin' or display_name == 'Admin'."""
    return user.email == "admin" or user.display_name == "Admin"


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def load_settings() -> AdminSettings:
    """Load settings from JSON file, returning defaults if missing."""
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return AdminSettings(**data)
        except Exception:
            logger.warning("Failed to parse %s, using defaults", SETTINGS_FILE)
    return AdminSettings()


def save_settings(s: AdminSettings) -> None:
    """Persist settings to JSON file, creating parent dirs if needed."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(s.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Admin settings saved to %s", SETTINGS_FILE)


# --- Endpoints ---

@router.get("/settings")
async def get_admin_settings(
    user: User | None = Depends(get_current_user),
) -> dict:
    _require_admin(user)
    return {"settings": load_settings().model_dump()}


@router.post("/settings")
async def update_admin_settings(
    body: AdminSettings,
    user: User | None = Depends(get_current_user),
) -> dict:
    _require_admin(user)
    save_settings(body)
    return {"settings": body.model_dump()}


# ---------------------------------------------------------------------------
# Job management endpoints
# ---------------------------------------------------------------------------

JOB_API_BASE = "http://localhost:8877"
WEB_UI_API_BASE = "http://localhost:8876"
JOBS_STORE_DIR = Path("/opt/aivideotrans/data/jobs")


@router.get("/jobs")
async def list_all_jobs(
    user: User | None = Depends(get_current_user),
) -> dict:
    """List ALL jobs across all users (admin only)."""
    _require_admin(user)

    # Fetch jobs from upstream Job API
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(f"{JOB_API_BASE}/jobs")
            resp.raise_for_status()
            upstream_jobs: list[dict] = resp.json()
        except Exception as exc:
            logger.error("Failed to fetch jobs from Job API: %s", exc)
            raise HTTPException(status_code=502, detail="无法获取任务列表")

    # Build a lookup of user info from PostgreSQL
    async with async_session() as db:
        rows = (await db.execute(
            select(Job.job_id, Job.user_id, Job.status, Job.project_dir,
                   User.email, User.display_name)
            .outerjoin(User, Job.user_id == User.id)
        )).all()

    db_lookup: dict[str, dict] = {}
    for row in rows:
        db_lookup[row.job_id] = {
            "user_id": str(row.user_id),
            "db_status": row.status,
            "project_dir": row.project_dir,
            "owner_email": row.email,
            "owner_display_name": row.display_name,
        }

    # Merge upstream job data with owner info
    result = []
    for job in upstream_jobs:
        jid = job.get("job_id") or job.get("id", "")
        owner = db_lookup.get(jid, {})
        result.append({**job, **owner})

    return {"jobs": result}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Cancel a job: stop processing, clean up files, mark cancelled in DB."""
    _require_admin(user)

    async with httpx.AsyncClient(timeout=30) as client:
        # (a) Tell web UI API to cancel
        try:
            await client.post(
                f"{WEB_UI_API_BASE}/api/job/cancel",
                json={"job_id": job_id},
            )
        except Exception as exc:
            logger.warning("Web UI cancel call failed for %s: %s", job_id, exc)

        # (b) Get project_dir from Job API
        project_dir: str | None = None
        try:
            resp = await client.get(f"{JOB_API_BASE}/jobs/{job_id}")
            if resp.status_code == 200:
                project_dir = resp.json().get("project_dir")
        except Exception as exc:
            logger.warning("Failed to fetch job info for %s: %s", job_id, exc)

    # (c) Delete project directory if it exists
    if project_dir and os.path.isdir(project_dir):
        shutil.rmtree(project_dir, ignore_errors=True)
        logger.info("Removed project dir: %s", project_dir)

    # (d) Update status in PostgreSQL
    async with async_session() as db:
        row = (await db.execute(
            select(Job).where(Job.job_id == job_id)
        )).scalar_one_or_none()
        if row:
            row.status = "cancelled"
            await db.commit()

    # (e) Delete job JSON file from Job API store
    job_file = JOBS_STORE_DIR / f"{job_id}.json"
    if job_file.exists():
        job_file.unlink(missing_ok=True)
        logger.info("Removed job file: %s", job_file)

    return {"success": True, "job_id": job_id}


@router.post("/jobs/{job_id}/delete")
async def delete_job(
    job_id: str,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Fully delete a job: cancel + remove PostgreSQL record."""
    _require_admin(user)

    async with httpx.AsyncClient(timeout=30) as client:
        # Cancel via web UI API
        try:
            await client.post(
                f"{WEB_UI_API_BASE}/api/job/cancel",
                json={"job_id": job_id},
            )
        except Exception as exc:
            logger.warning("Web UI cancel call failed for %s: %s", job_id, exc)

        # Get project_dir from Job API
        project_dir: str | None = None
        try:
            resp = await client.get(f"{JOB_API_BASE}/jobs/{job_id}")
            if resp.status_code == 200:
                project_dir = resp.json().get("project_dir")
        except Exception as exc:
            logger.warning("Failed to fetch job info for %s: %s", job_id, exc)

    # Delete project directory
    if project_dir and os.path.isdir(project_dir):
        shutil.rmtree(project_dir, ignore_errors=True)
        logger.info("Removed project dir: %s", project_dir)

    # Delete job JSON file
    job_file = JOBS_STORE_DIR / f"{job_id}.json"
    if job_file.exists():
        job_file.unlink(missing_ok=True)
        logger.info("Removed job file: %s", job_file)

    # Delete PostgreSQL record
    async with async_session() as db:
        await db.execute(sa_delete(Job).where(Job.job_id == job_id))
        await db.commit()

    return {"success": True, "deleted": True}
