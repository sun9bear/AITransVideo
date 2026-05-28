"""User entitlements API: plan limits, quota, allowed service modes.

Trial-aware (P3 fix): when a user is within an active trial window,
``get_effective_plan_gate`` from ``plan_catalog`` elevates their capabilities
(Studio, Plus-tier duration/concurrency) while ``user.plan_code`` stays "free".

Smart kill switch (Task #23, P2 launch blocker #1):
``get_effective_allowed_service_modes(user)`` is the single source of
truth for the smart kill switch. All three gates (this file's two
branches + ``job_intercept.py`` create gate) MUST use it, or admin
toggle / env var becomes meaningless.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from models import User
from plan_catalog import get_effective_plan_gate, is_user_in_active_trial

logger = logging.getLogger(__name__)
router = APIRouter(tags=["entitlements"])


def get_effective_allowed_service_modes(
    user: User | None, *, settings=None
) -> list[str]:
    """Compute the user's effective allowed_service_modes after applying
    the two-layer Smart kill switch.

    Layers (BOTH must be True for ``smart`` to appear):
      1. Env: ``Settings.enable_smart_mode`` (``AVT_ENABLE_SMART_MODE``)
      2. Admin runtime: ``AdminSettings.smart_mode_enabled`` (hot-flip)

    Admin users do NOT auto-bypass the kill switch — same logic as
    regular users (per Codex F2 fix, P2 plan §4.1). Without this, any
    admin would still get smart even when ops flipped the toggle off.

    Inputs:
      user: The User row (may be None — anonymous returns empty list).
      settings: Optional override for tests. Defaults to live config.

    Returns:
      A NEW list (caller can mutate safely). Always preserves
      express / studio order from the plan; only removes smart when the
      kill switch is off.

    Defensive: if admin_settings is unreadable for any reason, the helper
    fails CLOSED (treats admin toggle as False → smart removed). This
    matches the kill switch contract — a broken admin store should never
    accidentally enable a feature.
    """
    if user is None:
        return []

    # Plan-level base list. Admin users still use their plan_code's plan
    # so an admin who upgraded their own account to "free" doesn't
    # accidentally bypass any non-smart gates (e.g., studio in free plan).
    plan_info = get_effective_plan_gate(user)
    base = list(plan_info.get("allowed_service_modes", ()))

    # Special-case: legacy admin behavior allowed admin to see all
    # non-smart modes regardless of plan. Preserve that — kill switch
    # only affects ``smart``, not express/studio.
    role = getattr(user, "role", "user") or "user"
    if role == "admin":
        for mode in ("express", "studio"):
            if mode not in base:
                base.append(mode)

    # Now apply the kill switch to ``smart`` regardless of source (plan
    # or admin-augmented).
    if settings is None:
        from config import settings as _global_settings
        settings = _global_settings

    env_enabled = bool(getattr(settings, "enable_smart_mode", False))
    admin_enabled = False
    try:
        from admin_settings import load_settings as _load_admin_settings
        admin_enabled = bool(
            getattr(_load_admin_settings(), "smart_mode_enabled", False)
        )
    except Exception as exc:
        # Fail-closed: unreadable admin_settings → smart removed.
        logger.warning(
            "smart kill switch: admin_settings unreadable, treating as "
            "disabled (smart removed). cause=%s", exc,
        )
        admin_enabled = False

    if not (env_enabled and admin_enabled) and "smart" in base:
        base.remove("smart")

    return base


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

    # Single source of truth for allowed_service_modes — applies the
    # Smart kill switch (env + admin runtime toggle). Without this,
    # admin always saw the hardcoded ["express","studio","smart"] list
    # regardless of toggle state, and the frontend smart card showed up
    # for non-admin Plus/Pro users even with smart disabled at the env
    # level — both bypassed the kill switch.
    effective_modes = get_effective_allowed_service_modes(user)

    if is_admin:
        return {
            "role": role,
            "plan_code": plan,
            "limits": {
                "max_duration_minutes": None,
                "max_concurrent_jobs": None,
                "allowed_service_modes": effective_modes,
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
            "allowed_service_modes": effective_modes,
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


@router.get("/api/me/express-auto-clone-availability")
async def get_express_auto_clone_availability(
    user: User | None = Depends(get_current_user),
) -> dict:
    """Phase 4.3a §8.4：返回当前用户是否能看到 Express 自动 clone 入口。

    Frontend G 阶段调用此 endpoint 决定 ``TranslationForm`` 是否渲染
    "自动克隆主说话人音色（实验性）" checkbox。返回 ``available=true``
    时前端才显示，**但前端仅是展示层**——真正的授权边界在 pipeline
    Layer 1-3 + budget endpoint（spec §2 / §2.5）。

    返回 schema：
        {
            "available": bool,        # 是否渲染 checkbox UI
            "reason": str,            # 见下 5 个 reason 之一
        }

    全部可能的 reason 值（5 个）：
        - "ok"                          available=True，可渲染入口
        - "unauthenticated"             available=False，user 为 None
        - "admin_settings_unavailable"  available=False，admin_settings load 失败
                                        （fail-closed，避免 admin 配置坏时
                                         意外开放入口）
        - "admin_flag_off"              available=False，admin 主开关 False
        - "not_in_allowlist"            available=False，已登录普通用户
                                        但 user_id ∉ allowlist

    授权规则（与 spec §2 Layer 1 + Layer 3 对齐）：
        - 未登录 → available=False, reason="unauthenticated"
        - admin_settings load 失败 → available=False,
          reason="admin_settings_unavailable"（fail-closed）
        - admin 用户 + flag=True → available=True
        - admin 用户 + flag=False → available=False, reason="admin_flag_off"
          （admin 关闭自己的功能也认；让 admin 能从 UI 验证 flag 是否生效）
        - admin_flag=False → available=False, reason="admin_flag_off"
        - admin_flag=True 但 user_id ∉ allowlist → available=False,
          reason="not_in_allowlist"
        - admin_flag=True 且 user_id ∈ allowlist → available=True

    **不**返回 allowlist 内容（隐私）。**不**做 daily_cap / active_temp_cap
    检查（那是 pipeline budget endpoint 的事，spec §2.5）。

    NOTE: Phase 4.3a 阶段尚无 `general_availability` 二段灰度，仅靠
    enabled flag + allowlist 双门控；Phase 4.3 全量时会扩展。
    """
    if user is None:
        return {
            "available": False,
            "reason": "unauthenticated",
        }

    role = (getattr(user, "role", "user") or "user").strip().lower()
    user_id_str = str(getattr(user, "id", "") or "").strip()

    # admin 自动 bypass allowlist（spec §2 Layer 3）；同时仍然受
    # admin_flag 控制——admin 关 flag 时也看不到入口（自闭功能）。
    is_admin = role == "admin"

    # 读 admin_settings（与 entitlements 同模式：每次请求重读，hot-reloadable）
    try:
        from admin_settings import load_settings
        admin = load_settings()
    except Exception as exc:
        logger.warning(
            "express auto-clone availability: admin_settings load failed: %s",
            exc,
        )
        # Fail-closed：admin_settings 读不到时一律 unavailable
        return {
            "available": False,
            "reason": "admin_settings_unavailable",
        }

    flag_enabled = bool(
        getattr(admin, "express_cosyvoice_auto_clone_enabled", False)
    )
    allowlist_enabled = (
        getattr(admin, "express_cosyvoice_auto_clone_allowlist_enabled", True)
        is not False
    )
    allowlist = list(
        getattr(admin, "express_cosyvoice_auto_clone_user_allowlist", []) or []
    )

    if not flag_enabled:
        return {
            "available": False,
            "reason": "admin_flag_off",
        }

    if is_admin or not allowlist_enabled or (user_id_str and user_id_str in allowlist):
        return {
            "available": True,
            "reason": "ok",
        }

    return {
        "available": False,
        "reason": "not_in_allowlist",
    }
