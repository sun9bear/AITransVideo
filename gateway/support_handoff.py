"""Unified handoff orchestrator.

Plan §13 P2 — email is the default human channel; Chatwoot / WeChat are
stubs. The orchestrator:

1. Builds an AI-generated summary of the conversation from the last few
   messages (sanitized via ``redact_pii``).
2. Inserts a ``support_handoff_requests`` row.
3. Calls the configured provider adapter.
4. Updates ``support_conversations.handoff_state`` and
   ``support_handoff_requests.status`` according to the result.
5. Falls back to the email adapter if a non-email provider raises.

All paths return a structured response the API layer renders to the
frontend.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    SupportConversation,
    SupportHandoffRequest,
    SupportMessage,
    User,
)
from support_adapters.email import send_handoff_email
from support_knowledge import redact_pii

logger = logging.getLogger(__name__)


HANDOFF_USER_REPLY_BY_REASON = {
    "user_requested_human": (
        "好的，已为你创建人工客服工单。运营会通过邮件回复你；"
        "如果你登录了账号，也可以在「通知中心」看到回复进度。"
    ),
    "abuse_review": (
        "我已记录你的反馈并转人工跟进，运营会通过邮件联系你。"
    ),
    "policy_required": (
        "这类问题（账单 / 退款 / 隐私 / 版权）由人工客服直接处理。"
        "我已经创建工单，运营会通过邮件回复你。"
    ),
    "low_confidence": (
        "我没法确定准确答案，已为你转接人工客服。"
        "运营会通过邮件回复你。"
    ),
    "repeated_unresolved": (
        "看起来这个问题 AI 没能帮上忙，已为你转接人工客服。"
        "运营会通过邮件回复你。"
    ),
    "sensitive_category": (
        "我已为你转人工客服，运营会通过邮件回复你。"
    ),
    "abuse_review_blocked": (
        "我已记录你的反馈，运营将通过邮件联系你。"
    ),
}


def user_reply_for_reason(reason: str) -> str:
    return HANDOFF_USER_REPLY_BY_REASON.get(
        reason,
        "已为你创建人工客服工单，运营会通过邮件回复你。",
    )


async def _summarize_conversation(
    db: AsyncSession,
    conversation: SupportConversation,
    *,
    last_n: int = 5,
) -> tuple[str, list[dict[str, Any]]]:
    stmt = (
        select(SupportMessage)
        .where(SupportMessage.conversation_id == conversation.id)
        .order_by(SupportMessage.created_at.desc())
        .limit(last_n)
    )
    result = await db.execute(stmt)
    messages = list(result.scalars())
    messages.reverse()

    last_msgs = []
    for m in messages:
        last_msgs.append(
            {
                "sender": m.sender,
                "body": redact_pii(m.body or "")[:500],
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
        )

    user_msgs = [m for m in messages if m.sender == "user"]
    if user_msgs:
        first_user_q = redact_pii(user_msgs[0].body or "")[:200]
        summary = f"用户最初的问题：{first_user_q}"
    else:
        summary = "用户尚未发送问题文本。"
    return summary, last_msgs


async def create_handoff(
    db: AsyncSession,
    *,
    conversation: SupportConversation,
    reason: str,
    provider: str = "email",
    ops_email: str,
) -> dict[str, Any]:
    """Create a handoff request and call the provider adapter.

    On success, commits the handoff row and updates the conversation.
    On adapter failure, marks the row as ``failed`` and (if the failed
    provider was not already ``email``) tries the email adapter as a
    last-mile fallback.
    """

    summary, last_msgs = await _summarize_conversation(db, conversation)

    # Pull user identity for the ticket payload (best-effort).
    user_email = None
    user_phone = None
    plan_code = None
    if conversation.user_id is not None:
        user = await db.get(User, conversation.user_id)
        if user is not None:
            user_email = user.email
            user_phone = user.phone_number
            plan_code = getattr(user, "plan_code", None)

    request = SupportHandoffRequest(
        conversation_id=conversation.id,
        provider=provider,
        reason=reason,
        summary=summary,
        status="pending",
    )
    db.add(request)
    await db.flush()

    # Mark the conversation as awaiting human; status stays "open" until
    # the operator marks it handled, but ``handoff_state`` flips so the
    # frontend stops the AI auto-reply.
    conversation.status = "waiting_human"
    conversation.handoff_state = "requested"
    conversation.handoff_provider = provider
    conversation.updated_at = datetime.now(timezone.utc)

    payload: dict[str, Any]
    try:
        if provider == "email":
            payload = await send_handoff_email(
                to_email=ops_email,
                conversation_id=str(conversation.id),
                user_id=str(conversation.user_id) if conversation.user_id else None,
                user_email=user_email,
                user_phone=user_phone,
                plan_code=plan_code,
                page_url=conversation.page_url,
                job_id=conversation.job_id,
                reason=reason,
                summary=summary,
                last_messages=last_msgs,
            )
        else:
            # chatwoot / wechat_kf — stubs raise NotImplementedError; we
            # fall through to email below.
            raise NotImplementedError(f"provider {provider} not wired in P1")
        request.status = "created"
        request.provider_payload = payload
        conversation.handoff_state = "created"
    except NotImplementedError as exc:
        logger.warning("Handoff provider %s not implemented; falling back to email: %s", provider, exc)
        # Fallback path
        try:
            payload = await send_handoff_email(
                to_email=ops_email,
                conversation_id=str(conversation.id),
                user_id=str(conversation.user_id) if conversation.user_id else None,
                user_email=user_email,
                user_phone=user_phone,
                plan_code=plan_code,
                page_url=conversation.page_url,
                job_id=conversation.job_id,
                reason=reason,
                summary=summary,
                last_messages=last_msgs,
            )
            request.provider = "email"
            request.status = "created"
            request.provider_payload = payload
            conversation.handoff_provider = "email"
            conversation.handoff_state = "created"
        except Exception as fallback_exc:
            logger.exception("Email fallback also failed: %s", fallback_exc)
            request.status = "failed"
            request.provider_payload = {"error": str(fallback_exc)}
            conversation.handoff_state = "failed"
    except Exception as exc:
        logger.exception("Handoff provider %s failed: %s", provider, exc)
        request.status = "failed"
        request.provider_payload = {"error": str(exc)}
        conversation.handoff_state = "failed"

    await db.flush()
    return {
        "handoff_state": conversation.handoff_state,
        "provider": conversation.handoff_provider or provider,
        "provider_conversation_id": conversation.handoff_provider_conversation_id,
        "request_id": str(request.id),
        "status": request.status,
    }
