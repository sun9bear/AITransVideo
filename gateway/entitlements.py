"""User entitlements API: plan limits, quota, allowed service modes.

Trial-aware (P3 fix): when a user is within an active trial window,
``get_effective_plan_gate`` from ``plan_catalog`` elevates their capabilities
(Studio, Plus-tier duration/concurrency) while ``user.plan_code`` stays "free".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from models import User
from plan_catalog import get_effective_plan_gate, is_user_in_active_trial

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
    in_trial = is_user_in_active_trial(user)

    # Use trial-aware plan gate so Trial users get Studio + elevated limits.
    plan_info = get_effective_plan_gate(user)

    quota_total = getattr(user, "free_jobs_quota_total", 5)
    quota_used = getattr(user, "free_jobs_quota_used", 0)

    if is_admin:
        return {
            "role": role,
            "plan_code": plan,
            "limits": {
                "max_duration_minutes": None,
                "max_concurrent_jobs": None,
                # Smart MVP P2 launch (2026-05-16): admin sees smart in
                # the frontend mode picker too. Backend gate already
                # bypasses for admin (job_intercept.py:777
                # ``if user and not is_admin: ...``), but the frontend
                # ``smart card visible`` check uses this list and
                # without smart here the smart button shows as "即将开放".
                "allowed_service_modes": ["express", "studio", "smart"],
                "free_jobs_quota_total": None,
                "free_jobs_quota_used": None,
                "free_jobs_quota_remaining": None,
            },
            "ui": {
                "show_admin_badge": True,
                "allow_upgrade": False,
                "in_trial": False,
            },
        }

    return {
        "role": role,
        "plan_code": plan,
        "limits": {
            "max_duration_minutes": plan_info["max_duration_minutes"],
            "max_concurrent_jobs": plan_info["max_concurrent_jobs"],
            "allowed_service_modes": plan_info["allowed_service_modes"],
            "free_jobs_quota_total": quota_total if plan == "free" and not in_trial else None,
            "free_jobs_quota_used": quota_used if plan == "free" and not in_trial else None,
            "free_jobs_quota_remaining": (quota_total - quota_used) if plan == "free" and not in_trial else None,
        },
        "ui": {
            "show_admin_badge": False,
            "allow_upgrade": plan != "pro",
            "in_trial": in_trial,
        },
    }
