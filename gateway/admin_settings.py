"""Admin settings API: read/write global platform configuration + job management."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select, delete as sa_delete

from auth import get_current_user
from config import settings as app_settings
from database import async_session, get_db
from models import AdminAuditLog, Job, User

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path("/opt/aivideotrans/config/admin_settings.json")

router = APIRouter(prefix="/api/admin", tags=["admin"])


# --- Settings schema ---

_VALID_ENDPOINT_MODES = {"international", "mainland"}


class AdminSettings(BaseModel):
    tts_provider: str = "minimax"          # "minimax" or "mimo"
    review_model: str = "gemini_pro"       # "gemini_pro" (highest quality) | "gemini" (low cost) | "mimo_omni"
    translation_model: str = "deepseek"    # "deepseek" or "gemini"
    skip_translation_config_for_users: bool = True  # Normal users skip translation config step
    skip_all_reviews_for_free_users: bool = True   # Free users: fully automatic pipeline
    free_user_max_duration_minutes: float = 10.0   # Max video duration for free users (minutes)
    enable_pre_tts_rewrite: bool = True            # Pre-TTS rewrite to match target duration
    express_tts_provider: str = "cosyvoice"        # Default TTS provider for express mode
    studio_tts_provider: str = "minimax"           # Default TTS provider for studio mode
    cosyvoice_runtime_endpoint_mode: str = "international"  # CosyVoice runtime: "international" or "mainland"
    cosyvoice_offline_endpoint_mode: str = "mainland"       # CosyVoice offline: "international" or "mainland"
    voice_clone_cost_credits: int = 500                     # Credits cost per voice clone operation

    @field_validator("cosyvoice_runtime_endpoint_mode", "cosyvoice_offline_endpoint_mode")
    @classmethod
    def validate_endpoint_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in _VALID_ENDPOINT_MODES:
            raise ValueError(f"端点模式必须是 {sorted(_VALID_ENDPOINT_MODES)} 之一，收到: {v!r}")
        return normalized


# --- Helpers ---

def _is_admin(user: User) -> bool:
    """Check admin via role field only.

    After running Alembic 002, all users get role='user' by default.
    To bootstrap an admin: UPDATE users SET role='admin' WHERE email='your-admin@example.com';
    """
    return (getattr(user, "role", None) or "user") == "admin"


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
        # (a) Cancel via Job API
        try:
            await client.post(f"{JOB_API_BASE}/jobs/{job_id}/cancel")
        except Exception as exc:
            logger.warning("Job API cancel call failed for %s: %s", job_id, exc)

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

    # (d) Update status in PostgreSQL + release quota
    async with async_session() as db:
        row = (await db.execute(
            select(Job).where(Job.job_id == job_id)
        )).scalar_one_or_none()
        if row:
            row.status = "cancelled"
            # Release reserved quota
            from quota import release_quota
            await release_quota(db, row)
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
        # Cancel via Job API then delete
        try:
            await client.post(f"{JOB_API_BASE}/jobs/{job_id}/cancel")
        except Exception as exc:
            logger.warning("Job API cancel call failed for %s: %s", job_id, exc)

        try:
            await client.delete(f"{JOB_API_BASE}/jobs/{job_id}")
        except Exception as exc:
            logger.warning("Job API delete call failed for %s: %s", job_id, exc)

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

    # Release quota then delete PostgreSQL record
    async with async_session() as db:
        row = (await db.execute(
            select(Job).where(Job.job_id == job_id)
        )).scalar_one_or_none()
        if row:
            from quota import release_quota
            await release_quota(db, row)
        await db.execute(sa_delete(Job).where(Job.job_id == job_id))
        await db.commit()

    return {"success": True, "deleted": True}


# ---------------------------------------------------------------------------
# User management endpoints
# ---------------------------------------------------------------------------

VALID_ROLES = {"user", "admin"}
VALID_PLAN_CODES = {"free", "plus", "pro"}


@router.get("/users")
async def list_users(
    user: User | None = Depends(get_current_user),
) -> dict:
    """List all users with role, plan, quota, and active job count."""
    _require_admin(user)

    async with async_session() as db:
        # Users
        users_result = await db.execute(select(User).order_by(User.created_at.desc()))
        users = users_result.scalars().all()

        # Active job counts per user
        from sqlalchemy import func
        active_counts_result = await db.execute(
            select(Job.user_id, func.count())
            .where(Job.status.in_(["queued", "running", "waiting_for_review"]))
            .group_by(Job.user_id)
        )
        active_counts = {str(row[0]): row[1] for row in active_counts_result.all()}

        # Total job counts per user
        total_counts_result = await db.execute(
            select(Job.user_id, func.count())
            .group_by(Job.user_id)
        )
        total_counts = {str(row[0]): row[1] for row in total_counts_result.all()}

    return {
        "users": [
            {
                "id": str(u.id),
                "email": u.email,
                "display_name": u.display_name,
                "role": u.role,
                "plan_code": u.plan_code,
                "free_jobs_quota_total": u.free_jobs_quota_total,
                "free_jobs_quota_used": u.free_jobs_quota_used,
                "is_active": u.is_active,
                "active_jobs": active_counts.get(str(u.id), 0),
                "total_jobs": total_counts.get(str(u.id), 0),
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ]
    }


class UpdateEntitlementsRequest(BaseModel):
    role: str | None = None
    plan_code: str | None = None
    free_jobs_quota_total: int | None = None
    free_jobs_quota_used: int | None = None


@router.patch("/users/{user_id}/entitlements")
async def update_user_entitlements(
    user_id: str,
    body: UpdateEntitlementsRequest,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Update a user's role, plan_code, or quota. Writes audit log."""
    admin = _require_admin(user)

    async with async_session() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        target = result.scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404, detail="用户不存在")

        changes = []

        # --- Role change ---
        if body.role is not None and body.role != target.role:
            if body.role not in VALID_ROLES:
                raise HTTPException(status_code=400, detail=f"无效的 role: {body.role}")
            # Guard: prevent demoting the last admin
            if target.role == "admin" and body.role != "admin":
                from sqlalchemy import func as sa_func
                admin_count_result = await db.execute(
                    select(sa_func.count()).where(User.role == "admin")
                )
                admin_count = admin_count_result.scalar() or 0
                if admin_count <= 1:
                    raise HTTPException(
                        status_code=409,
                        detail="无法降级：系统中至少需要保留一个管理员。"
                    )
            old_val = target.role
            target.role = body.role
            changes.append(("update_role", "role", old_val, body.role))

        # --- Plan change ---
        if body.plan_code is not None and body.plan_code != target.plan_code:
            if body.plan_code not in VALID_PLAN_CODES:
                raise HTTPException(status_code=400, detail=f"无效的 plan_code: {body.plan_code}")
            old_val = target.plan_code
            target.plan_code = body.plan_code
            changes.append(("update_plan_code", "plan_code", old_val, body.plan_code))

        # --- Quota adjustments with boundary validation ---
        new_total = body.free_jobs_quota_total if body.free_jobs_quota_total is not None else target.free_jobs_quota_total
        new_used = body.free_jobs_quota_used if body.free_jobs_quota_used is not None else target.free_jobs_quota_used
        if new_total < 0:
            raise HTTPException(status_code=400, detail="free_jobs_quota_total 不能为负数")
        if new_used < 0:
            raise HTTPException(status_code=400, detail="free_jobs_quota_used 不能为负数")
        if new_used > new_total:
            raise HTTPException(
                status_code=400,
                detail=f"free_jobs_quota_used ({new_used}) 不能大于 free_jobs_quota_total ({new_total})"
            )

        if body.free_jobs_quota_total is not None and body.free_jobs_quota_total != target.free_jobs_quota_total:
            old_val = str(target.free_jobs_quota_total)
            target.free_jobs_quota_total = body.free_jobs_quota_total
            changes.append(("adjust_quota", "free_jobs_quota_total", old_val, str(body.free_jobs_quota_total)))

        if body.free_jobs_quota_used is not None and body.free_jobs_quota_used != target.free_jobs_quota_used:
            old_val = str(target.free_jobs_quota_used)
            target.free_jobs_quota_used = body.free_jobs_quota_used
            changes.append(("adjust_quota", "free_jobs_quota_used", old_val, str(body.free_jobs_quota_used)))

        if not changes:
            return {"updated": False, "message": "无变更"}

        # Write audit log entries
        for action, field, old_v, new_v in changes:
            db.add(AdminAuditLog(
                admin_user_id=admin.id,
                target_user_id=target.id,
                action=action,
                field_name=field,
                old_value=old_v,
                new_value=new_v,
            ))

        await db.commit()
        logger.info("Admin %s updated user %s: %s", admin.email, target.email,
                     "; ".join(f"{f}: {o}->{n}" for _, f, o, n in changes))

        return {
            "updated": True,
            "user": {
                "id": str(target.id),
                "email": target.email,
                "role": target.role,
                "plan_code": target.plan_code,
                "free_jobs_quota_total": target.free_jobs_quota_total,
                "free_jobs_quota_used": target.free_jobs_quota_used,
            },
            "changes": [
                {"field": f, "old": o, "new": n}
                for _, f, o, n in changes
            ],
        }


@router.get("/users/{user_id}/audit-log")
async def get_user_audit_log(
    user_id: str,
    user: User | None = Depends(get_current_user),
) -> dict:
    """Get audit log entries for a specific user."""
    _require_admin(user)

    async with async_session() as db:
        result = await db.execute(
            select(AdminAuditLog, User.email.label("admin_email"))
            .outerjoin(User, AdminAuditLog.admin_user_id == User.id)
            .where(AdminAuditLog.target_user_id == user_id)
            .order_by(AdminAuditLog.created_at.desc())
            .limit(50)
        )
        rows = result.all()

    return {
        "entries": [
            {
                "id": str(entry.id),
                "admin_email": admin_email,
                "action": entry.action,
                "field_name": entry.field_name,
                "old_value": entry.old_value,
                "new_value": entry.new_value,
                "created_at": entry.created_at.isoformat() if entry.created_at else None,
            }
            for entry, admin_email in rows
        ]
    }
