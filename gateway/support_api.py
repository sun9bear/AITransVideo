"""FastAPI router for the customer support endpoints.

Routes:

- ``GET  /api/support/config`` — public config, anonymous-safe.
- ``POST /api/support/conversations`` — create a new conversation.
- ``POST /api/support/conversations/{id}/messages`` — send a user message,
  receive the AI/template/handoff reply.
- ``GET  /api/support/conversations/{id}`` — retrieve transcript.
- ``POST /api/support/conversations/{id}/handoff`` — explicit handoff.

Authorization rules (plan §10.1):

- Anonymous users: allowed iff ``support_anonymous_enabled``. Anonymous
  conversation rows carry ``anonymous_id``; we never read their own
  account data because there isn't any.
- Logged-in users: must own the conversation (``conversation.user_id``)
  to read or post.
- Cross-user reads: 403.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timezone

from fastapi.responses import FileResponse, Response

from auth import get_current_user
from csrf import require_same_origin_state_change
from database import get_db
from models import (
    SupportConversation,
    SupportMessage,
    User,
)
from support_admin_settings import load_support_settings
from support_handoff import create_handoff, user_reply_for_reason
from support_models import (
    ConversationDetail,
    CreateConversationRequest,
    CreateConversationResponse,
    HandoffRequest,
    HandoffResponse,
    MessageView,
    MyOpenConversationsResponse,
    MyOpenConversationView,
    OnlineStatusResponse,
    SendMessageRequest,
    SendMessageResponse,
    SupportConfigResponse,
)
from support_presence import count_online
from support_service import create_conversation, send_message
from support_wechat_qr import existing_qr_path, get_qr_metadata, public_url as qr_public_url

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/support",
    tags=["support"],
    dependencies=[Depends(require_same_origin_state_change)],
)

# Anonymous device cookie. Distinct from the auth session cookie so that
# logged-out visitors get a stable identity for rate limiting / threading
# without inheriting any auth state.
ANON_COOKIE_NAME = "avt_support_anon"
ANON_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


def _ensure_anon_cookie(request: Request, response: Response) -> str:
    """Return a stable anonymous device id, setting the cookie if missing."""
    existing = request.cookies.get(ANON_COOKIE_NAME)
    if existing and len(existing) >= 16:
        return existing
    new_id = uuid.uuid4().hex
    response.set_cookie(
        key=ANON_COOKIE_NAME,
        value=new_id,
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=ANON_COOKIE_MAX_AGE,
        path="/",
    )
    return new_id


# Simple in-memory rate limit. Plan §10.2 — P1 may use simple
# in-memory/DB limiter; this matches the existing risk_control pattern
# for low-volume endpoints. If rate limit hits exceed budget alarms,
# upgrade to the shared limiter.
_RATE_BUCKET: dict[str, list[float]] = {}


def _rate_limit_check(*, key: str, max_per_hour: int) -> bool:
    """Return True if rate limited, False otherwise.

    Sliding 1h window. Cheap O(N) cleanup per call; N is bounded by the
    per-IP/per-user budget so this stays trivial.
    """
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - 3600
    bucket = _RATE_BUCKET.get(key, [])
    bucket = [t for t in bucket if t >= cutoff]
    if len(bucket) >= max_per_hour:
        _RATE_BUCKET[key] = bucket
        return True
    bucket.append(now)
    _RATE_BUCKET[key] = bucket
    return False


# ---------------------------------------------------------------------------
# /api/support/config
# ---------------------------------------------------------------------------


@router.get("/online-status", response_model=OnlineStatusResponse)
async def get_online_status(
    db: AsyncSession = Depends(get_db),
) -> OnlineStatusResponse:
    """Public endpoint — widget polls this to choose handoff routing UI.

    Returns the boolean ``online`` flag plus enough config so the widget
    can render the offline branch (WeChat QR + offline message) without
    a second round-trip.
    """
    settings = load_support_settings()
    threshold = int(settings.get("support_admin_online_threshold_seconds", 60))
    online_count = await count_online(db, threshold_seconds=threshold)
    qr_meta = get_qr_metadata()
    return OnlineStatusResponse(
        online=online_count > 0,
        online_count=online_count,
        has_wechat_qr=qr_meta is not None,
        offline_message=str(
            settings.get(
                "support_offline_message",
                "运营暂未在线，可扫码添加客服微信，我们尽快回复。",
            )
        ),
        handoff_offline_fallback_minutes=int(
            settings.get("support_handoff_offline_fallback_minutes", 5)
        ),
    )


@router.get("/wechat-qr")
async def get_wechat_qr() -> Response:
    """Serve the uploaded WeChat QR image. Public (anyone visiting the
    site can see it — same trust level as a QR printed on the contact
    page footer)."""
    p = existing_qr_path()
    if p is None:
        return Response(status_code=404, content="no QR uploaded")
    media_type = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    # Mild caching: the URL has ``?v={mtime}`` suffix from the frontend
    # so a re-upload busts caches automatically.
    headers = {
        "Cache-Control": "public, max-age=300",
    }
    return FileResponse(
        path=str(p),
        media_type=media_type,
        headers=headers,
    )


@router.get("/conversations/my/open", response_model=MyOpenConversationsResponse)
async def list_my_open_conversations(
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MyOpenConversationsResponse:
    """Return the user's still-open support conversations.

    Used by SupportWidget on mount: if the user has a conversation that
    is waiting for human reply (handoff_state in {requested, created}),
    we restore it instead of starting a fresh one. 401 for anonymous —
    only logged-in users get conversation continuity across sessions.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    stmt = (
        select(SupportConversation)
        .where(
            SupportConversation.user_id == user.id,
            SupportConversation.status.in_(("open", "waiting_human")),
        )
        .order_by(SupportConversation.updated_at.desc())
        .limit(20)
    )
    rows = list((await db.execute(stmt)).scalars())
    items: list[MyOpenConversationView] = []
    for c in rows:
        # Pull last message body for preview.
        last_msg_stmt = (
            select(SupportMessage)
            .where(SupportMessage.conversation_id == c.id)
            .order_by(SupportMessage.created_at.desc())
            .limit(1)
        )
        last = (await db.execute(last_msg_stmt)).scalar_one_or_none()
        preview = (last.body or "")[:120] if last else ""
        items.append(
            MyOpenConversationView(
                conversation_id=str(c.id),
                status=c.status,
                handoff_state=c.handoff_state,
                last_message_preview=preview,
                updated_at=c.updated_at,
            )
        )
    return MyOpenConversationsResponse(conversations=items)


@router.get("/config", response_model=SupportConfigResponse)
async def get_support_config() -> SupportConfigResponse:
    settings = load_support_settings()
    return SupportConfigResponse(
        enabled=bool(settings.get("support_enabled", True)),
        anonymous_enabled=bool(settings.get("support_anonymous_enabled", True)),
        ai_enabled=bool(settings.get("support_ai_enabled", False)),
        handoff_enabled=True,
        wechat_kf_enabled=False,
        max_messages_before_captcha=5,
        greeting=(
            "你好，我可以帮你解答试用、套餐、导出剪映草稿和任务排障问题。"
        ),
        quick_questions=[
            "试用会自动扣费吗？",
            "怎么导出剪映草稿？",
            "任务失败怎么办？",
            "我要找人工客服",
        ],
        handoff_provider_default=settings.get("support_handoff_provider", "email"),
    )


# ---------------------------------------------------------------------------
# POST /api/support/conversations
# ---------------------------------------------------------------------------


@router.post("/conversations", response_model=CreateConversationResponse)
async def create_conversation_endpoint(
    body: CreateConversationRequest,
    request: Request,
    response: Response,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CreateConversationResponse:
    settings = load_support_settings()
    if not settings.get("support_enabled", True):
        raise HTTPException(status_code=503, detail="客服系统暂未开放")
    if user is None and not settings.get("support_anonymous_enabled", True):
        raise HTTPException(status_code=401, detail="请登录后再咨询")

    anon_id = None
    if user is None:
        anon_id = _ensure_anon_cookie(request, response)

    convo = await create_conversation(
        db,
        user=user,
        anonymous_id=anon_id,
        channel=body.channel,
        entrypoint=body.entrypoint,
        page_url=body.page_url,
        job_id=body.job_id,
        notification_id=body.notification_id,
    )
    await db.commit()
    return CreateConversationResponse(
        conversation_id=str(convo.id),
        status=convo.status,
        handoff_state=convo.handoff_state,
    )


# ---------------------------------------------------------------------------
# POST /api/support/conversations/{id}/messages
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


async def _resolve_conversation(
    db: AsyncSession,
    *,
    conversation_id: str,
    user: User | None,
    anonymous_id: str | None,
) -> SupportConversation:
    """Fetch + ownership-check a conversation.

    Anonymous-conversation rule (Codex review P1-1, 2026-05-08):
    knowing a ``conversation_id`` is NOT enough to read or post into an
    anonymous conversation — the request must also present the matching
    ``avt_support_anon`` cookie. A request without the cookie is
    rejected with 401, even if the conversation has no ``user_id``.
    """
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="会话不存在")
    convo = await db.get(SupportConversation, cid)
    if convo is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if user is not None:
        if convo.user_id is not None and convo.user_id != user.id:
            raise HTTPException(status_code=403, detail="无权访问该会话")
        # A logged-in user accessing an anonymous conversation is also
        # rejected — the conversation belongs to a visitor session and
        # we do not implicitly bind it to the now-authenticated user.
        if convo.user_id is None:
            raise HTTPException(status_code=403, detail="无权访问该会话")
    else:
        if convo.user_id is not None:
            raise HTTPException(status_code=401, detail="请登录后再访问")
        # convo.user_id is None → anonymous conversation. Require an
        # anonymous_id cookie present AND matching the row.
        if not convo.anonymous_id:
            # Conversation row has no anonymous_id at all (legacy / corrupt
            # state). Refuse rather than fall through.
            raise HTTPException(status_code=401, detail="无法识别访客会话")
        if not anonymous_id:
            raise HTTPException(status_code=401, detail="无法识别访客会话")
        if convo.anonymous_id != anonymous_id:
            raise HTTPException(status_code=403, detail="无权访问该会话")
    return convo


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=SendMessageResponse,
)
async def send_message_endpoint(
    conversation_id: str,
    body: SendMessageRequest,
    request: Request,
    response: Response,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    anonymous_cookie: str | None = Cookie(default=None, alias=ANON_COOKIE_NAME),
) -> SendMessageResponse:
    settings = load_support_settings()
    if not settings.get("support_enabled", True):
        raise HTTPException(status_code=503, detail="客服系统暂未开放")

    anon_id = anonymous_cookie if user is None else None
    convo = await _resolve_conversation(
        db,
        conversation_id=conversation_id,
        user=user,
        anonymous_id=anon_id,
    )

    if user is None:
        max_per_hour = 20
        rate_key = f"ip:{_client_ip(request)}"
    else:
        max_per_hour = 60
        rate_key = f"user:{user.id}"
    rate_limited = _rate_limit_check(key=rate_key, max_per_hour=max_per_hour)

    payload = await send_message(
        db,
        conversation=convo,
        user=user,
        message=body.message,
        page_url=body.client_context.page_url,
        job_id=body.client_context.job_id,
        rate_limited=rate_limited,
    )
    await db.commit()
    return SendMessageResponse(**payload)


# ---------------------------------------------------------------------------
# GET /api/support/conversations/{id}
# ---------------------------------------------------------------------------


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation_endpoint(
    conversation_id: str,
    request: Request,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    anonymous_cookie: str | None = Cookie(default=None, alias=ANON_COOKIE_NAME),
) -> ConversationDetail:
    anon_id = anonymous_cookie if user is None else None
    convo = await _resolve_conversation(
        db,
        conversation_id=conversation_id,
        user=user,
        anonymous_id=anon_id,
    )
    stmt = (
        select(SupportMessage)
        .where(SupportMessage.conversation_id == convo.id)
        .order_by(SupportMessage.created_at.asc())
    )
    result = await db.execute(stmt)
    messages = [
        MessageView(
            id=str(m.id),
            sender=m.sender,
            body=m.body,
            created_at=m.created_at,
        )
        for m in result.scalars()
    ]
    return ConversationDetail(
        id=str(convo.id),
        status=convo.status,
        handoff_state=convo.handoff_state,
        messages=messages,
    )


# ---------------------------------------------------------------------------
# POST /api/support/conversations/{id}/handoff
# ---------------------------------------------------------------------------


@router.post(
    "/conversations/{conversation_id}/handoff",
    response_model=HandoffResponse,
)
async def handoff_endpoint(
    conversation_id: str,
    body: HandoffRequest,
    request: Request,
    user: User | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    anonymous_cookie: str | None = Cookie(default=None, alias=ANON_COOKIE_NAME),
) -> HandoffResponse:
    settings = load_support_settings()
    anon_id = anonymous_cookie if user is None else None
    convo = await _resolve_conversation(
        db,
        conversation_id=conversation_id,
        user=user,
        anonymous_id=anon_id,
    )
    provider = body.preferred_channel or settings.get("support_handoff_provider", "email")
    result = await create_handoff(
        db,
        conversation=convo,
        reason=body.reason,
        provider=provider,
        ops_email=settings.get("support_ops_email", "sxz999@proton.me"),
    )
    actual_provider = result["provider"]
    payload = result.get("payload") or {}

    # User-facing UI message depends on the routing decision:
    # - in_product: "运营会在浮窗里直接回复你"
    # - wechat_qr: offline message + QR (the widget renders the image)
    # - email / chatwoot / wechat_kf: legacy reasons → use user_reply_for_reason
    if actual_provider == "in_product":
        ui_message = "已为你转接人工客服。运营在线，会直接在这里回复你。"
    elif actual_provider == "wechat_qr":
        ui_message = payload.get(
            "offline_message",
            "运营暂未在线，可扫码添加客服微信，我们尽快回复。",
        )
    else:
        ui_message = user_reply_for_reason(body.reason)

    # Append a small "已转人工" assistant note for UI continuity. For
    # wechat_qr we DON'T append a chat-bubble note — the widget shows
    # the QR card directly.
    if actual_provider != "wechat_qr":
        note = SupportMessage(
            conversation_id=convo.id,
            sender="system",
            body=ui_message,
            metadata_json={"event": "handoff_created", "provider": actual_provider},
        )
        db.add(note)

    threshold = int(settings.get("support_admin_online_threshold_seconds", 60))
    online_now = await count_online(db, threshold_seconds=threshold)
    await db.commit()
    return HandoffResponse(
        handoff_state=result["handoff_state"] if result["handoff_state"] in ("created", "failed", "requested") else "requested",
        provider=actual_provider,
        provider_conversation_id=result.get("provider_conversation_id"),
        message=ui_message,
        wechat_qr_url=payload.get("wechat_qr_url"),
        offline_message=payload.get("offline_message"),
        online_count=online_now,
    )
