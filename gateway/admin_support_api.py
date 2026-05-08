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

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from database import get_db
from models import (
    SupportAIUsage,
    SupportConversation,
    SupportHandoffRequest,
    SupportMessage,
    SystemAnnouncement,
    User,
)
from support_admin_settings import (
    invalidate_cache as invalidate_support_cache,
    load_support_settings,
    save_support_settings,
)
from support_budget import current_budget_month, get_budget_status
from support_models import (
    AdminReplyRequest,
    AdminReplyResponse,
    AdminSupportOverview,
    AdminSupportSettingsResponse,
    AnnouncementInput,
    AnnouncementListResponse,
    AnnouncementView,
    AudienceCatalogResponse,
    AudienceKindSpec,
    AudienceParamSpec,
    AudiencePreviewResponse,
    HeartbeatRequest,
    PresenceView,
    RecallAnnouncementResponse,
    SendAnnouncementResponse,
    SetPresenceStatusRequest,
    SupportAdminSettings,
    WeChatQrInfoResponse,
)
from support_presence import (
    get_my_presence,
    list_recent as list_recent_presence,
    record_heartbeat,
    set_status as set_presence_status,
)
from system_announcements_service import (
    AUDIENCE_KINDS,
    clone_for_resend,
    count_audience,
    recall_announcement,
    send_announcement,
    stats_for as announcement_stats_for,
)
from support_wechat_qr import (
    delete_qr,
    get_qr_metadata,
    public_url as qr_public_url,
    save_qr,
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
        support_admin_heartbeat_interval_seconds=int(
            merged.get("support_admin_heartbeat_interval_seconds", 30)
        ),
        support_admin_online_threshold_seconds=int(
            merged.get("support_admin_online_threshold_seconds", 60)
        ),
        support_handoff_offline_fallback_minutes=int(
            merged.get("support_handoff_offline_fallback_minutes", 5)
        ),
        support_offline_message=str(
            merged.get(
                "support_offline_message",
                "运营暂未在线，可扫码添加客服微信，我们尽快回复。",
            )
        ),
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


# ---------------------------------------------------------------------------
# Presence (heartbeat + status switch)
# ---------------------------------------------------------------------------


def _seconds_since(ts: datetime) -> int:
    return max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))


@router.post("/heartbeat", response_model=PresenceView)
async def heartbeat(
    body: HeartbeatRequest = HeartbeatRequest(),
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PresenceView:
    """Admin tab pings here every N seconds (default 30, configurable).

    The body's ``status`` field is optional — if provided, the
    heartbeat doubles as an explicit status set; if omitted, the
    existing status is preserved.
    """
    admin = _require_admin(user)
    if body.status is not None:
        row = await set_presence_status(db, user_id=admin.id, status=body.status)
    else:
        row = await record_heartbeat(db, user_id=admin.id)
    await db.commit()
    return PresenceView(
        user_id=str(row.user_id),
        status=row.status,
        last_heartbeat_at=row.last_heartbeat_at,
        seconds_since_last_heartbeat=_seconds_since(row.last_heartbeat_at),
    )


@router.post("/presence/status", response_model=PresenceView)
async def set_presence(
    body: SetPresenceStatusRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PresenceView:
    admin = _require_admin(user)
    row = await set_presence_status(db, user_id=admin.id, status=body.status)
    await db.commit()
    return PresenceView(
        user_id=str(row.user_id),
        status=row.status,
        last_heartbeat_at=row.last_heartbeat_at,
        seconds_since_last_heartbeat=_seconds_since(row.last_heartbeat_at),
    )


@router.get("/presence/me", response_model=PresenceView | None)
async def get_my_presence_endpoint(
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PresenceView | None:
    """Return the calling admin's own presence row (or None if no
    heartbeat has ever been recorded). Used by the topbar status
    switcher to restore state on page reload."""
    admin = _require_admin(user)
    row = await get_my_presence(db, user_id=admin.id)
    if row is None:
        return None
    return PresenceView(
        user_id=str(row.user_id),
        status=row.status,
        last_heartbeat_at=row.last_heartbeat_at,
        seconds_since_last_heartbeat=_seconds_since(row.last_heartbeat_at),
    )


@router.get("/presence/recent")
async def list_presence_recent(
    limit: int = 20,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[dict[str, Any]]]:
    _require_admin(user)
    rows = await list_recent_presence(db, limit=limit)
    return {
        "items": [
            {
                "user_id": str(r.user_id),
                "status": r.status,
                "last_heartbeat_at": r.last_heartbeat_at.isoformat(),
                "seconds_since_last_heartbeat": _seconds_since(r.last_heartbeat_at),
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# WeChat QR upload / serve / delete
# ---------------------------------------------------------------------------


@router.get("/wechat-qr", response_model=WeChatQrInfoResponse)
async def get_wechat_qr_info(
    user: User | None = Depends(get_current_user),
) -> WeChatQrInfoResponse:
    _require_admin(user)
    meta = get_qr_metadata()
    if meta is None:
        return WeChatQrInfoResponse(has_qr=False)
    return WeChatQrInfoResponse(
        has_qr=True,
        url=qr_public_url(),
        uploaded_at=meta["uploaded_at"],
        size_bytes=meta["size_bytes"],
    )


@router.post("/wechat-qr", response_model=WeChatQrInfoResponse)
async def upload_wechat_qr(
    file: UploadFile = File(...),
    user: User | None = Depends(get_current_user),
) -> WeChatQrInfoResponse:
    _require_admin(user)
    body = await file.read()
    try:
        save_qr(content_type=file.content_type or "", body=body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    meta = get_qr_metadata()
    assert meta is not None
    return WeChatQrInfoResponse(
        has_qr=True,
        url=qr_public_url(),
        uploaded_at=meta["uploaded_at"],
        size_bytes=meta["size_bytes"],
    )


@router.delete("/wechat-qr")
async def delete_wechat_qr(
    user: User | None = Depends(get_current_user),
) -> dict[str, bool]:
    _require_admin(user)
    return {"removed": delete_qr()}


# ---------------------------------------------------------------------------
# Admin reply to a support conversation (in-product chat)
# ---------------------------------------------------------------------------


@router.get("/conversations/{conversation_id}")
async def admin_get_conversation(
    conversation_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Admin-scoped read of a support conversation (full message log + user identity).

    Plain dict response so the admin UI can display the user's plan /
    page / job_id without going through the user-side ``/api/support``
    routes (which would 403 for cross-user reads)."""
    _require_admin(user)
    import uuid as _uuid

    try:
        cid = _uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="会话不存在")
    convo = await db.get(SupportConversation, cid)
    if convo is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    msgs_stmt = (
        select(SupportMessage)
        .where(SupportMessage.conversation_id == convo.id)
        .order_by(SupportMessage.created_at.asc())
    )
    msgs = list((await db.execute(msgs_stmt)).scalars())

    target_user = None
    if convo.user_id is not None:
        target_user = await db.get(User, convo.user_id)

    return {
        "conversation": {
            "id": str(convo.id),
            "user_id": str(convo.user_id) if convo.user_id else None,
            "anonymous_id": convo.anonymous_id,
            "channel": convo.channel,
            "entrypoint": convo.entrypoint,
            "page_url": convo.page_url,
            "job_id": convo.job_id,
            "status": convo.status,
            "handoff_state": convo.handoff_state,
            "handoff_provider": convo.handoff_provider,
            "created_at": convo.created_at.isoformat(),
            "updated_at": convo.updated_at.isoformat(),
        },
        "user": (
            {
                "id": str(target_user.id),
                "display_name": target_user.display_name,
                "email": target_user.email or "",
                "phone_number": target_user.phone_number,
                "plan_code": getattr(target_user, "plan_code", None),
                "role": getattr(target_user, "role", None),
            }
            if target_user
            else None
        ),
        "messages": [
            {
                "id": str(m.id),
                "sender": m.sender,
                "body": m.body,
                "created_at": m.created_at.isoformat(),
                "metadata": m.metadata_json,
            }
            for m in msgs
        ],
    }


@router.post(
    "/conversations/{conversation_id}/reply",
    response_model=AdminReplyResponse,
)
async def admin_reply_to_conversation(
    conversation_id: str,
    body: AdminReplyRequest,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AdminReplyResponse:
    """Admin posts a human reply into the conversation.

    Side effects:
    - Append a ``support_messages`` row with sender="human".
    - Conversation flips to status="waiting_human" (so the user-side
      AI doesn't resume) and handoff_state="created".
    - Dispatch ``support.human_replied`` notification (best-effort) so
      the user gets a red-dot in the notification bell.
    - Optionally mark the underlying handoff_request status="handled".
    """
    admin = _require_admin(user)
    import uuid as _uuid

    try:
        cid = _uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="会话不存在")
    convo = await db.get(SupportConversation, cid)
    if convo is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    msg = SupportMessage(
        conversation_id=convo.id,
        sender="human",
        body=body.body,
        metadata_json={
            "admin_user_id": str(admin.id),
            "admin_display_name": admin.display_name,
        },
    )
    db.add(msg)

    convo.message_count = (convo.message_count or 0) + 1
    convo.status = "waiting_human" if convo.status != "closed" else convo.status
    convo.handoff_state = (
        "created" if convo.handoff_state in ("none", "recommended", "requested") else convo.handoff_state
    )
    convo.updated_at = datetime.now(timezone.utc)
    await db.flush()

    handoff_status: str | None = None
    if body.mark_handled and convo.user_id is not None:
        # Mark any open handoff_request for this conversation as handled.
        stmt = (
            select(SupportHandoffRequest)
            .where(
                SupportHandoffRequest.conversation_id == convo.id,
                SupportHandoffRequest.status.in_(("pending", "created")),
            )
            .order_by(SupportHandoffRequest.created_at.desc())
            .limit(1)
        )
        h = (await db.execute(stmt)).scalar_one_or_none()
        if h is not None:
            h.status = "closed"
            h.updated_at = datetime.now(timezone.utc)
            handoff_status = "closed"

    notification_dispatched = False
    if convo.user_id is not None:
        try:
            from notification_dispatch_map import EVENT_SUPPORT_HUMAN_REPLIED
            from notifications_service import dispatch_event

            await dispatch_event(
                db,
                event_type=EVENT_SUPPORT_HUMAN_REPLIED,
                user_id=convo.user_id,
                payload={"summary": body.body[:80]},
                related_id=str(convo.id),
                dedupe_key=f"support.human_replied:{convo.id}:{msg.id}",
            )
            notification_dispatched = True
        except Exception:
            logger.exception("Failed to dispatch human_replied notification")

    await db.commit()
    return AdminReplyResponse(
        message_id=str(msg.id),
        notification_dispatched=notification_dispatched,
        handoff_status=handoff_status,
    )


# ---------------------------------------------------------------------------
# System announcements (admin broadcasts)
# ---------------------------------------------------------------------------


def _serialize_announcement(
    row: SystemAnnouncement,
    *,
    stats: dict[str, int] | None = None,
) -> AnnouncementView:
    return AnnouncementView(
        id=str(row.id),
        title=row.title,
        body=row.body,
        topic=row.topic,
        severity=row.severity,
        action_url=row.action_url,
        audience_kind=row.audience_kind,
        audience_params=row.audience_params,
        status=row.status,
        sent_at=row.sent_at,
        recipient_count=row.recipient_count,
        parent_id=str(row.parent_id) if row.parent_id else None,
        created_by_admin_id=(
            str(row.created_by_admin_id) if row.created_by_admin_id else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        stats=stats,
    )


@router.get(
    "/announcements/audience-kinds",
    response_model=AudienceCatalogResponse,
)
async def list_audience_kinds(
    user: User | None = Depends(get_current_user),
) -> AudienceCatalogResponse:
    _require_admin(user)
    kinds = [
        AudienceKindSpec(
            kind=k["kind"],
            label=k["label"],
            group=k["group"],
            params=[AudienceParamSpec(**p) for p in k.get("params", [])],
        )
        for k in AUDIENCE_KINDS
    ]
    return AudienceCatalogResponse(kinds=kinds)


@router.post(
    "/announcements/audience-preview",
    response_model=AudiencePreviewResponse,
)
async def preview_audience(
    body: AnnouncementInput,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AudiencePreviewResponse:
    _require_admin(user)
    try:
        n = await count_audience(
            db, kind=body.audience_kind, params=body.audience_params
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return AudiencePreviewResponse(
        audience_kind=body.audience_kind,
        audience_params=body.audience_params,
        count=n,
    )


@router.get(
    "/announcements",
    response_model=AnnouncementListResponse,
)
async def list_announcements(
    status: str | None = None,
    limit: int = 50,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnnouncementListResponse:
    _require_admin(user)
    stmt = select(SystemAnnouncement).order_by(
        SystemAnnouncement.created_at.desc()
    )
    if status:
        stmt = stmt.where(SystemAnnouncement.status == status)
    stmt = stmt.limit(max(1, min(int(limit or 50), 200)))
    rows = list((await db.execute(stmt)).scalars())

    views: list[AnnouncementView] = []
    for r in rows:
        if r.status == "sent":
            stats = await announcement_stats_for(db, announcement_id=r.id)
        else:
            stats = None
        views.append(_serialize_announcement(r, stats=stats))
    return AnnouncementListResponse(items=views)


@router.post(
    "/announcements",
    response_model=AnnouncementView,
)
async def create_announcement(
    body: AnnouncementInput,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnnouncementView:
    """Create a draft announcement. Send is a separate POST."""
    admin = _require_admin(user)
    # Validate audience_kind early so a bad value doesn't sit in draft
    # and surprise the admin at send time.
    from system_announcements_service import _validate_params

    try:
        _validate_params(body.audience_kind, body.audience_params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    row = SystemAnnouncement(
        title=body.title,
        body=body.body,
        topic=body.topic,
        severity=body.severity,
        action_url=body.action_url,
        audience_kind=body.audience_kind,
        audience_params=body.audience_params,
        created_by_admin_id=admin.id,
        status="draft",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _serialize_announcement(row)


@router.get(
    "/announcements/{announcement_id}",
    response_model=AnnouncementView,
)
async def get_announcement(
    announcement_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnnouncementView:
    _require_admin(user)
    import uuid as _uuid

    try:
        aid = _uuid.UUID(announcement_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="公告不存在")
    row = await db.get(SystemAnnouncement, aid)
    if row is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    stats = (
        await announcement_stats_for(db, announcement_id=row.id)
        if row.status == "sent"
        else None
    )
    return _serialize_announcement(row, stats=stats)


@router.patch(
    "/announcements/{announcement_id}",
    response_model=AnnouncementView,
)
async def update_announcement(
    announcement_id: str,
    body: AnnouncementInput,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnnouncementView:
    """Edit a draft. Sent announcements are immutable — clone instead."""
    _require_admin(user)
    import uuid as _uuid

    try:
        aid = _uuid.UUID(announcement_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="公告不存在")
    row = await db.get(SystemAnnouncement, aid)
    if row is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    if row.status == "sent":
        raise HTTPException(
            status_code=400,
            detail="已发送的公告不可编辑，请使用「复制为新公告」",
        )

    from system_announcements_service import _validate_params

    try:
        _validate_params(body.audience_kind, body.audience_params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    row.title = body.title
    row.body = body.body
    row.topic = body.topic
    row.severity = body.severity
    row.action_url = body.action_url
    row.audience_kind = body.audience_kind
    row.audience_params = body.audience_params
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return _serialize_announcement(row)


@router.delete("/announcements/{announcement_id}")
async def delete_draft_announcement(
    announcement_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Hard-delete a DRAFT announcement. Sent ones must be recalled."""
    _require_admin(user)
    import uuid as _uuid

    try:
        aid = _uuid.UUID(announcement_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="公告不存在")
    row = await db.get(SystemAnnouncement, aid)
    if row is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    if row.status == "sent":
        raise HTTPException(
            status_code=400, detail="已发送的公告不能删除，请使用撤回"
        )
    await db.delete(row)
    await db.commit()
    return {"deleted": "ok"}


@router.post(
    "/announcements/{announcement_id}/send",
    response_model=SendAnnouncementResponse,
)
async def send_announcement_endpoint(
    announcement_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SendAnnouncementResponse:
    admin = _require_admin(user)
    import uuid as _uuid

    try:
        aid = _uuid.UUID(announcement_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="公告不存在")
    row = await db.get(SystemAnnouncement, aid)
    if row is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    try:
        result = await send_announcement(db, announcement=row, admin_id=admin.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return SendAnnouncementResponse(**result)


@router.post(
    "/announcements/{announcement_id}/recall",
    response_model=RecallAnnouncementResponse,
)
async def recall_announcement_endpoint(
    announcement_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecallAnnouncementResponse:
    """Hard-delete all user_notifications fanned out from this
    announcement. Sets the row to status="archived"."""
    _require_admin(user)
    import uuid as _uuid

    try:
        aid = _uuid.UUID(announcement_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="公告不存在")
    row = await db.get(SystemAnnouncement, aid)
    if row is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    if row.status != "sent":
        raise HTTPException(
            status_code=400, detail="只有已发送的公告可以撤回"
        )
    n = await recall_announcement(db, announcement=row)
    await db.commit()
    return RecallAnnouncementResponse(
        announcement_id=str(row.id),
        deleted_count=n,
    )


@router.post(
    "/announcements/{announcement_id}/clone",
    response_model=AnnouncementView,
)
async def clone_announcement_endpoint(
    announcement_id: str,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnnouncementView:
    """Create a draft copy. Used for "edit and resend" workflows."""
    admin = _require_admin(user)
    import uuid as _uuid

    try:
        aid = _uuid.UUID(announcement_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="公告不存在")
    row = await db.get(SystemAnnouncement, aid)
    if row is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    clone = await clone_for_resend(db, source=row, admin_id=admin.id)
    await db.commit()
    return _serialize_announcement(clone)
