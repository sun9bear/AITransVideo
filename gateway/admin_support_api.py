"""Admin API for the customer support system.

Endpoints:

- ``GET  /api/admin/support/settings`` — current settings + budget state.
- ``POST /api/admin/support/settings`` — admin save (limited to the
  10-field whitelist defined by ``SupportAdminSettings``).
- ``GET  /api/admin/support/model-options`` — text-only models from
  llm_registry, excluding disabled ones.
- ``GET  /api/admin/support/overview`` — counts for the admin dashboard.
- ``GET  /api/admin/support/handoffs`` — list pending / closed handoffs.
- ``POST /api/admin/support/handoffs/{id}/close`` — operator close.

Plan §7.2 — admin UI is intentionally minimal in P1; templates and
external channel configuration are NOT exposed here.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Same sys.path manipulation as ``admin_settings.py`` — gateway containers
# can't import src/ via a package path, so we prepend the candidate dirs
# before importing llm_registry below.
for _candidate in [
    Path(__file__).resolve().parent.parent / "src",
    Path("/opt/aivideotrans/app/src"),
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import (
    SupportAIUsage,
    SupportConversation,
    SupportHandoffRequest,
    User,
)
from support_admin_settings import (
    invalidate_cache as invalidate_support_cache,
    load_support_settings,
    save_support_settings,
)
from support_budget import current_budget_month, get_budget_status
from support_models import (
    AdminSupportOverview,
    AdminSupportSettingsResponse,
    SupportAdminSettings,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/admin/support", tags=["admin-support"])


# Reuse the gateway-wide admin gate from admin_settings.
def _is_admin(user: User) -> bool:
    return (getattr(user, "role", None) or "user") == "admin"


def _require_admin(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _settings_to_model(merged: dict[str, Any]) -> SupportAdminSettings:
    """Project a merged-settings dict back into the admin Pydantic model.

    Anything outside the 10-field whitelist is dropped — the model itself
    rejects unknown keys.
    """
    return SupportAdminSettings(
        support_enabled=bool(merged.get("support_enabled", False)),
        support_anonymous_enabled=bool(merged.get("support_anonymous_enabled", False)),
        support_ai_enabled=bool(merged.get("support_ai_enabled", False)),
        support_ai_model=str(merged.get("support_ai_model", "deepseek")),
        support_ai_max_output_tokens=int(merged.get("support_ai_max_output_tokens", 400)),
        support_ai_monthly_budget_usd=float(merged.get("support_ai_monthly_budget_usd", 50)),
        support_ai_input_usd_per_1m_tokens=float(
            merged.get("support_ai_input_usd_per_1m_tokens", 0.14)
        ),
        support_ai_output_usd_per_1m_tokens=float(
            merged.get("support_ai_output_usd_per_1m_tokens", 0.28)
        ),
        support_budget_exhausted_message=str(
            merged.get(
                "support_budget_exhausted_message",
                "AI 客服当前繁忙，你可以先查看常见问题，或转人工客服处理。",
            )
        ),
        support_sensitive_keywords=list(
            merged.get("support_sensitive_keywords", [])
        ),
        support_ops_email=str(merged.get("support_ops_email", "sxz999@proton.me")),
    )


def _list_text_models() -> list[dict[str, Any]]:
    """Return non-disabled models eligible for the ``support_chat`` prompt key.

    Plan §7.2 — admin support page MUST respect main admin's
    ``disabled_models`` list. We delegate to
    ``services.llm_registry.get_available_models_for_prompt("support_chat")``
    so the same eligibility logic the rest of the gateway uses applies
    here. ``support_chat`` does not require audio capability, so all
    enabled text + multimodal models are returned.

    Codex review round 2 (2026-05-08, nit): each row also carries
    ``support_implemented`` to flag whether the underlying provider has
    real wiring in support_ai. The frontend uses this to render a
    "未接入客服" badge so admins are not misled when picking a model
    whose provider currently silently falls back to fake.
    """
    try:
        from services.llm_registry import (
            MODEL_REGISTRY,
            get_available_models_for_prompt,
        )
    except Exception:
        logger.exception("llm_registry import failed; returning empty model options")
        return []
    try:
        rows = list(get_available_models_for_prompt("support_chat") or [])
    except Exception:
        logger.exception("get_available_models_for_prompt failed for support_chat")
        return []
    try:
        from support_ai import _IMPLEMENTED_REAL_PROVIDERS
    except Exception:
        _IMPLEMENTED_REAL_PROVIDERS = set()
    enriched: list[dict[str, Any]] = []
    for row in rows:
        info = MODEL_REGISTRY.get(row.get("value", ""), {}) if isinstance(row, dict) else {}
        provider = (info.get("provider") or "").strip().lower()
        enriched.append(
            {
                **row,
                "provider": provider,
                "support_implemented": provider in _IMPLEMENTED_REAL_PROVIDERS,
            }
        )
    return enriched


@router.get("/settings", response_model=AdminSupportSettingsResponse)
async def get_admin_settings(
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AdminSupportSettingsResponse:
    _require_admin(user)
    merged = load_support_settings(force_reload=True)
    cap = float(merged.get("support_ai_monthly_budget_usd", 50.0))
    budget = await get_budget_status(db, cap_usd=cap)
    return AdminSupportSettingsResponse(
        settings=_settings_to_model(merged),
        budget_state=budget.state,
        spent_this_month_usd=budget.spent_usd,
        available_models=_list_text_models(),
    )


@router.post("/settings", response_model=AdminSupportSettingsResponse)
async def update_admin_settings(
    body: SupportAdminSettings,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AdminSupportSettingsResponse:
    _require_admin(user)
    save_support_settings(body)
    invalidate_support_cache()
    merged = load_support_settings(force_reload=True)
    cap = float(merged.get("support_ai_monthly_budget_usd", 50.0))
    budget = await get_budget_status(db, cap_usd=cap)
    return AdminSupportSettingsResponse(
        settings=_settings_to_model(merged),
        budget_state=budget.state,
        spent_this_month_usd=budget.spent_usd,
        available_models=_list_text_models(),
    )


@router.get("/model-options")
async def model_options(
    user: User | None = Depends(get_current_user),
) -> dict[str, list[dict[str, Any]]]:
    _require_admin(user)
    return {"models": _list_text_models()}


@router.get("/overview", response_model=AdminSupportOverview)
async def overview(
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AdminSupportOverview:
    _require_admin(user)
    merged = load_support_settings()
    cap = float(merged.get("support_ai_monthly_budget_usd", 50.0))
    budget = await get_budget_status(db, cap_usd=cap)

    today = datetime.now(timezone.utc).date()
    today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    total_conv = (await db.execute(
        select(func.count()).select_from(SupportConversation)
    )).scalar() or 0
    today_conv = (await db.execute(
        select(func.count())
        .select_from(SupportConversation)
        .where(SupportConversation.created_at >= today_start)
    )).scalar() or 0
    total_handoffs = (await db.execute(
        select(func.count()).select_from(SupportHandoffRequest)
    )).scalar() or 0
    pending_handoffs = (await db.execute(
        select(func.count())
        .select_from(SupportHandoffRequest)
        .where(SupportHandoffRequest.status.in_(("pending", "created")))
    )).scalar() or 0

    month = current_budget_month()
    ai_msgs_month = (await db.execute(
        select(func.count())
        .select_from(SupportAIUsage)
        .where(SupportAIUsage.budget_month == month, SupportAIUsage.route == "llm")
    )).scalar() or 0
    template_hits_month = (await db.execute(
        select(func.count())
        .select_from(SupportAIUsage)
        .where(
            SupportAIUsage.budget_month == month,
            SupportAIUsage.route.in_(("template", "faq")),
        )
    )).scalar() or 0

    return AdminSupportOverview(
        conversations_total=int(total_conv),
        conversations_today=int(today_conv),
        handoff_requests_total=int(total_handoffs),
        handoff_requests_pending=int(pending_handoffs),
        ai_messages_this_month=int(ai_msgs_month),
        template_hits_this_month=int(template_hits_month),
        spent_this_month_usd=budget.spent_usd,
        budget_state=budget.state,
    )


@router.get("/handoffs")
async def list_handoffs(
    status: str | None = None,
    limit: int = 50,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[dict[str, Any]]]:
    _require_admin(user)
    stmt = select(SupportHandoffRequest).order_by(
        SupportHandoffRequest.created_at.desc()
    ).limit(max(1, min(int(limit or 50), 200)))
    if status:
        stmt = stmt.where(SupportHandoffRequest.status == status)
    rows = list((await db.execute(stmt)).scalars())
    items = []
    for r in rows:
        items.append(
            {
                "id": str(r.id),
                "conversation_id": str(r.conversation_id),
                "provider": r.provider,
                "reason": r.reason,
                "summary": r.summary,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
        )
    return {"items": items}


@router.post("/handoffs/{handoff_id}/close")
async def close_handoff(
    handoff_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    _require_admin(user)
    import uuid as _uuid

    try:
        hid = _uuid.UUID(handoff_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="工单不存在")
    row = await db.get(SupportHandoffRequest, hid)
    if row is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    row.status = "closed"
    row.updated_at = datetime.now(timezone.utc)
    convo = await db.get(SupportConversation, row.conversation_id)
    if convo is not None:
        convo.status = "closed"
        convo.handoff_state = "closed"
        convo.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "closed"}
