"""User entitlements API: plan limits, quota, allowed service modes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from job_intercept import PLAN_CATALOG
from models import User

router = APIRouter(tags=["entitlements"])


@router.get("/api/me/entitlements")
async def get_entitlements(
    user: User | None = Depends(get_current_user),
) -> dict:
    """Return current user's plan limits and quota for frontend display."""
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    role = getattr(user, "role", "user") or "user"
    plan = getattr(user, "plan_code", "free") or "free"
    is_admin = role == "admin"

    plan_info = PLAN_CATALOG.get(plan, PLAN_CATALOG["free"])

    quota_total = getattr(user, "free_jobs_quota_total", 5)
    quota_used = getattr(user, "free_jobs_quota_used", 0)

    if is_admin:
        return {
            "role": role,
            "plan_code": plan,
            "limits": {
                "max_duration_minutes": None,
                "max_concurrent_jobs": None,
                "allowed_service_modes": ["express", "studio"],
                "free_jobs_quota_total": None,
                "free_jobs_quota_used": None,
                "free_jobs_quota_remaining": None,
            },
            "ui": {
                "show_admin_badge": True,
                "allow_upgrade": False,
            },
        }

    return {
        "role": role,
        "plan_code": plan,
        "limits": {
            "max_duration_minutes": plan_info["max_duration_minutes"],
            "max_concurrent_jobs": plan_info["max_concurrent_jobs"],
            "allowed_service_modes": plan_info["allowed_service_modes"],
            "free_jobs_quota_total": quota_total if plan == "free" else None,
            "free_jobs_quota_used": quota_used if plan == "free" else None,
            "free_jobs_quota_remaining": (quota_total - quota_used) if plan == "free" else None,
        },
        "ui": {
            "show_admin_badge": False,
            "allow_upgrade": plan != "pro",
        },
    }
