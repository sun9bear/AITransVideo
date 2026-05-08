"""Top-level orchestrator for the customer support flow.

Stitches together the rate limit, policy, knowledge, template, AI provider,
budget, and handoff modules. Called by ``support_api.py``.

Design rules:

- The service is the only module that decides "is this conversation
  routed to AI/template/handoff *for this specific message*". Plan §3.2.
- All paths write a ``support_messages`` row (user + assistant), and a
  ``support_ai_usage`` row even when no LLM call happened.
- The service is stateless beyond the DB: no in-memory caches, no
  side-effect channels.
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Mirror admin_settings.py — gateway needs src/ on sys.path before any
# llm_registry import succeeds. Module-level so it runs once at import.
for _candidate in [
    Path(__file__).resolve().parent.parent / "src",
    Path("/opt/aivideotrans/app/src"),
]:
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    Job,
    SupportConversation,
    SupportMessage,
    User,
)
from support_admin_settings import load_support_settings
from support_ai import resolve_provider, is_real_provider_ready
from support_budget import (
    current_budget_month,
    get_budget_status,
    record_usage,
)
from support_handoff import create_handoff, user_reply_for_reason
from support_knowledge import (
    get_plan_facts,
    redact_pii,
    sanitize_job_context_for_ai,
    search_faq,
)
from support_models import HandoffSummary, SupportSource
from support_policy import RoutingDecision, decide_route
from support_templates import (
    ENGLISH_FALLBACK_REPLY,
    route_message,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_job_for_user(
    db: AsyncSession,
    *,
    user: User | None,
    job_id: str | None,
) -> Job | None:
    """Fetch a Job iff it exists AND belongs to the current user.

    Anonymous visitors and cross-user requests both get None — the AI
    must never see another user's job context.
    """
    if not job_id or user is None:
        return None
    stmt = select(Job).where(Job.job_id == job_id, Job.user_id == user.id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _conversation_message_count_user(
    db: AsyncSession,
    *,
    conversation_id: uuid.UUID,
) -> int:
    stmt = select(SupportMessage).where(
        SupportMessage.conversation_id == conversation_id,
        SupportMessage.sender == "user",
    )
    result = await db.execute(stmt)
    return len(result.scalars().all())


def _ai_enabled(settings_dict: dict[str, Any]) -> bool:
    return bool(settings_dict.get("support_ai_enabled", False))


def _resolve_provider_for_model(
    model: str | None,
    *,
    env_override: str | None = None,
) -> str:
    """Map an admin-selected logical model name to a provider name.

    Resolution order (Codex P1-3, 2026-05-08):

    1. If ``env_override`` (the legacy ``AVT_SUPPORT_AI_PROVIDER`` setting)
       is set to a non-empty, non-"fake" value, honor it. This stays as
       an emergency escape hatch.
    2. Otherwise look up ``model`` in ``services.llm_registry.MODEL_REGISTRY``
       and return the registry's ``provider`` field.
    3. If neither path produces a usable provider name, return "fake".
    """
    override = (env_override or "").strip().lower()
    if override and override != "fake":
        return override
    if not model:
        return "fake"
    try:
        from services.llm_registry import MODEL_REGISTRY  # type: ignore
    except Exception:
        return "fake"
    info = MODEL_REGISTRY.get(model.strip()) if isinstance(model, str) else None
    if not info:
        return "fake"
    provider = (info.get("provider") or "").strip().lower()
    return provider or "fake"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_conversation(
    db: AsyncSession,
    *,
    user: User | None,
    anonymous_id: str | None,
    channel: str,
    entrypoint: str,
    page_url: str | None,
    job_id: str | None,
    notification_id: str | None = None,
) -> SupportConversation:
    """Insert a new SupportConversation row.

    ``anonymous_id`` is required when ``user`` is None (caller passes a
    cookie / device id). The ID is opaque to the support system; we just
    persist it for analytics.
    """
    if user is None and not anonymous_id:
        # Caller fell through without a session AND without a device id.
        # Generate a synthetic one so the row has a stable identity.
        anonymous_id = str(uuid.uuid4())

    notif_uuid: uuid.UUID | None = None
    if notification_id:
        try:
            notif_uuid = uuid.UUID(notification_id)
        except ValueError:
            notif_uuid = None

    convo = SupportConversation(
        user_id=user.id if user is not None else None,
        anonymous_id=anonymous_id if user is None else None,
        channel=channel,
        entrypoint=entrypoint,
        page_url=page_url,
        job_id=job_id,
        notification_id=notif_uuid,
    )
    db.add(convo)
    await db.flush()
    await db.refresh(convo)
    return convo


async def send_message(
    db: AsyncSession,
    *,
    conversation: SupportConversation,
    user: User | None,
    message: str,
    page_url: str | None,
    job_id: str | None,
    rate_limited: bool,
) -> dict[str, Any]:
    """Process a user message and return the structured reply payload.

    Returns the raw dict that ``support_api`` wraps in
    ``SendMessageResponse``.
    """

    settings_dict = load_support_settings()
    sensitive = settings_dict.get("support_sensitive_keywords") or []
    monthly_cap = float(settings_dict.get("support_ai_monthly_budget_usd", 50.0))
    budget = await get_budget_status(db, cap_usd=monthly_cap)
    ai_enabled = _ai_enabled(settings_dict)

    # 1. Persist the user message first — even if we throw later, the
    #    conversation history stays consistent.
    user_msg = SupportMessage(
        conversation_id=conversation.id,
        sender="user",
        body=message,
        redacted_body=redact_pii(message),
    )
    db.add(user_msg)
    conversation.message_count = (conversation.message_count or 0) + 1
    if page_url:
        conversation.page_url = page_url
    if job_id:
        conversation.job_id = job_id
    conversation.updated_at = datetime.now(timezone.utc)
    await db.flush()

    # 2. Resolve job context (if the user owns the job).
    job = await _load_job_for_user(db, user=user, job_id=conversation.job_id)
    job_ctx = sanitize_job_context_for_ai(job)

    # 3. Decide the high-level routing.
    decision = decide_route(
        message=message,
        sensitive_keywords=sensitive,
        budget_exhausted=(budget.state == "budget_exhausted"),
        rate_limited=rate_limited,
        ai_enabled=ai_enabled,
    )

    # ---- Path A: rate limit ----------------------------------------------
    if decision.decision is RoutingDecision.rate_limited:
        reply_text = "请求过于频繁，请稍后再试，或转人工客服处理。"
        await _persist_assistant(
            db,
            conversation=conversation,
            reply_text=reply_text,
            metadata={"route": "blocked", "reason": "rate_limited"},
        )
        await record_usage(
            db,
            conversation_id=conversation.id,
            user_id=user.id if user else None,
            anonymous_id=conversation.anonymous_id,
            provider="none",
            model="none",
            route="template",
        )
        return {
            "reply": reply_text,
            "confidence": 1.0,
            "category": "throttled",
            "sources": [],
            "handoff": HandoffSummary(recommended=True, reason="abuse_review").model_dump(),
            "route": "blocked",
            "budget_state": budget.state,
        }

    # ---- Path B: explicit handoff path -----------------------------------
    if decision.decision is RoutingDecision.handoff_now:
        handoff_result = await create_handoff(
            db,
            conversation=conversation,
            reason=decision.handoff_reason or "user_requested_human",
            provider="email",
            ops_email=settings_dict.get("support_ops_email", "sxz999@proton.me"),
        )
        actual_provider = handoff_result.get("provider", "email")
        ho_payload = handoff_result.get("payload") or {}
        # User-facing reply text depends on routing decision (in_product
        # vs wechat_qr vs legacy).
        if actual_provider == "in_product":
            reply_text = "已为你转接人工客服。运营在线，会直接在这里回复你。"
        elif actual_provider == "wechat_qr":
            reply_text = ho_payload.get(
                "offline_message",
                "运营暂未在线，可扫码添加客服微信，我们尽快回复。",
            )
        else:
            reply_text = user_reply_for_reason(
                decision.handoff_reason or "user_requested_human"
            )
        await _persist_assistant(
            db,
            conversation=conversation,
            reply_text=reply_text,
            metadata={
                "route": "handoff",
                "reason": decision.handoff_reason,
                "provider": actual_provider,
            },
        )
        await record_usage(
            db,
            conversation_id=conversation.id,
            user_id=user.id if user else None,
            anonymous_id=conversation.anonymous_id,
            provider="none",
            model="none",
            route="handoff",
        )
        return {
            "reply": reply_text,
            "confidence": 1.0,
            "category": f"handoff:{decision.handoff_reason}",
            "sources": [],
            "handoff": HandoffSummary(
                recommended=True,
                required=decision.handoff_required,
                reason=decision.handoff_reason,
            ).model_dump(),
            "route": "handoff",
            "budget_state": budget.state,
            "wechat_qr_url": ho_payload.get("wechat_qr_url"),
            "offline_message": ho_payload.get("offline_message"),
            "handoff_provider": actual_provider,
        }

    # ---- Path C: English fallback ----------------------------------------
    if decision.decision is RoutingDecision.english_fallback:
        ops_email = settings_dict.get("support_ops_email", "sxz999@proton.me")
        reply_text = ENGLISH_FALLBACK_REPLY.format(ops_email=ops_email)
        await _persist_assistant(
            db,
            conversation=conversation,
            reply_text=reply_text,
            metadata={"route": "template", "reason": "english_only"},
        )
        await record_usage(
            db,
            conversation_id=conversation.id,
            user_id=user.id if user else None,
            anonymous_id=conversation.anonymous_id,
            provider="none",
            model="none",
            route="template",
        )
        return {
            "reply": reply_text,
            "confidence": 1.0,
            "category": "english_fallback",
            "sources": [],
            "handoff": HandoffSummary().model_dump(),
            "route": "template",
            "budget_state": budget.state,
        }

    # ---- Path D: budget exhausted ----------------------------------------
    if decision.decision is RoutingDecision.budget_blocked:
        reply_text = settings_dict.get(
            "support_budget_exhausted_message",
            "AI 客服当前繁忙，你可以先查看常见问题，或转人工客服处理。",
        )
        await _persist_assistant(
            db,
            conversation=conversation,
            reply_text=reply_text,
            metadata={"route": "template", "reason": "budget_exhausted"},
        )
        await record_usage(
            db,
            conversation_id=conversation.id,
            user_id=user.id if user else None,
            anonymous_id=conversation.anonymous_id,
            provider="none",
            model="none",
            route="template",
        )
        return {
            "reply": reply_text,
            "confidence": 1.0,
            "category": "budget_exhausted",
            "sources": [],
            "handoff": HandoffSummary(recommended=True, reason="low_confidence").model_dump(),
            "route": "template",
            "budget_state": budget.state,
        }

    # ---- Path E / F: deterministic template, then LLM --------------------
    template_match = route_message(
        message,
        error_category=(job_ctx.error_category if job_ctx else None),
        sensitive_keywords=sensitive,
    )
    if template_match is not None:
        sources = [SupportSource(type="template", id=template_match.template_id)]
        reply_text = template_match.reply
        handoff_info = HandoffSummary(
            recommended=template_match.handoff_recommended,
            required=template_match.handoff_required,
            reason=template_match.handoff_reason,
        )
        await _persist_assistant(
            db,
            conversation=conversation,
            reply_text=reply_text,
            metadata={
                "route": "template",
                "template_id": template_match.template_id,
            },
        )
        ho_payload: dict[str, Any] = {}
        actual_provider: str | None = None
        if template_match.handoff_recommended:
            handoff_result = await create_handoff(
                db,
                conversation=conversation,
                reason=template_match.handoff_reason or "sensitive_category",
                provider="email",
                ops_email=settings_dict.get("support_ops_email", "sxz999@proton.me"),
            )
            actual_provider = handoff_result.get("provider")
            ho_payload = handoff_result.get("payload") or {}
        await record_usage(
            db,
            conversation_id=conversation.id,
            user_id=user.id if user else None,
            anonymous_id=conversation.anonymous_id,
            provider="none",
            model="none",
            route="template",
        )
        return {
            "reply": reply_text,
            "confidence": template_match.confidence,
            "category": template_match.category,
            "sources": [s.model_dump() for s in sources],
            "handoff": handoff_info.model_dump(),
            "route": "template",
            "budget_state": budget.state,
            "wechat_qr_url": ho_payload.get("wechat_qr_url"),
            "offline_message": ho_payload.get("offline_message"),
            "handoff_provider": actual_provider,
        }

    # FAQ search before LLM — short answers are deterministic and free.
    faq_hits = search_faq(message, top_k=1)
    if faq_hits:
        first = faq_hits[0]
        sources = [SupportSource(type="faq", id=first["id"])]
        reply_text = first["a"]
        await _persist_assistant(
            db,
            conversation=conversation,
            reply_text=reply_text,
            metadata={"route": "faq", "faq_id": first["id"]},
        )
        await record_usage(
            db,
            conversation_id=conversation.id,
            user_id=user.id if user else None,
            anonymous_id=conversation.anonymous_id,
            provider="none",
            model="none",
            route="faq",
        )
        return {
            "reply": reply_text,
            "confidence": 0.7,
            "category": first.get("id", ""),
            "sources": [s.model_dump() for s in sources],
            "handoff": HandoffSummary().model_dump(),
            "route": "faq",
            "budget_state": budget.state,
        }

    # LLM fallback — but only if AI is enabled AND a real provider is
    # ready. Otherwise we route the conversation back to a polite "I'm
    # not sure" + handoff suggestion (still no LLM call).
    #
    # Codex review P1-3 (2026-05-08): admin's ``support_ai_model`` MUST
    # drive the provider, not just env. We look up the model in the
    # registry to find its provider; an explicit ``AVT_SUPPORT_AI_PROVIDER``
    # env override still wins (escape hatch for emergency rollbacks).
    desired_model = settings_dict.get("support_ai_model", "deepseek")
    desired_provider = _resolve_provider_for_model(
        desired_model, env_override=settings_dict.get("support_ai_provider")
    )
    real_ready = ai_enabled and is_real_provider_ready(desired_provider)
    provider_name = desired_provider if real_ready else "fake"
    provider = resolve_provider(provider_name)

    knowledge = {
        "plans": get_plan_facts(),
        "job": job_ctx.to_prompt_dict() if job_ctx else None,
    }
    history = await _recent_history(db, conversation.id, limit=6)
    # Codex P1 round 2 (2026-05-08): defense-in-depth fallback. Even if
    # a future change accidentally promotes a stub provider into the
    # "real" path, NotImplementedError must NOT surface as a 500 to the
    # user. Catch it, log, and serve a fake reply on the same prompt.
    try:
        ai_reply = await provider.reply(
            message=message,
            history=history,
            knowledge=knowledge,
            max_output_tokens=int(settings_dict.get("support_ai_max_output_tokens", 400)),
            max_input_chars=int(settings_dict.get("support_ai_max_input_chars", 2000)),
            timeout_seconds=float(settings_dict.get("support_ai_timeout_seconds", 15.0)),
        )
    except NotImplementedError:
        logger.warning(
            "Provider %r raised NotImplementedError; falling back to fake. "
            "Add the provider to _IMPLEMENTED_REAL_PROVIDERS only after wiring is reviewed.",
            provider_name,
        )
        provider = resolve_provider("fake")
        provider_name = "fake"
        ai_reply = await provider.reply(
            message=message,
            history=history,
            knowledge=knowledge,
            max_output_tokens=int(settings_dict.get("support_ai_max_output_tokens", 400)),
            max_input_chars=int(settings_dict.get("support_ai_max_input_chars", 2000)),
            timeout_seconds=float(settings_dict.get("support_ai_timeout_seconds", 15.0)),
        )

    handoff_info = HandoffSummary(
        recommended=ai_reply.handoff_recommended,
        reason=ai_reply.handoff_reason,
    )
    sources: list[SupportSource] = []
    if knowledge.get("plans") and knowledge["plans"].get("plans"):
        sources.append(SupportSource(type="plan_catalog", id="snapshot"))
    if knowledge.get("job"):
        sources.append(SupportSource(type="job_status", id=conversation.job_id or ""))

    await _persist_assistant(
        db,
        conversation=conversation,
        reply_text=ai_reply.reply,
        metadata={
            "route": "llm",
            "provider": provider_name,
            "confidence": ai_reply.confidence,
        },
    )
    ho_payload: dict[str, Any] = {}
    actual_handoff_provider: str | None = None
    if ai_reply.handoff_recommended:
        handoff_result = await create_handoff(
            db,
            conversation=conversation,
            reason=ai_reply.handoff_reason or "low_confidence",
            provider="email",
            ops_email=settings_dict.get("support_ops_email", "sxz999@proton.me"),
        )
        actual_handoff_provider = handoff_result.get("provider")
        ho_payload = handoff_result.get("payload") or {}

    await record_usage(
        db,
        conversation_id=conversation.id,
        user_id=user.id if user else None,
        anonymous_id=conversation.anonymous_id,
        provider=provider_name,
        model=settings_dict.get("support_ai_model", "deepseek"),
        route="llm",
        input_tokens=ai_reply.input_tokens,
        output_tokens=ai_reply.output_tokens,
        input_usd_per_1m=float(
            settings_dict.get("support_ai_input_usd_per_1m_tokens", 0.14)
        ),
        output_usd_per_1m=float(
            settings_dict.get("support_ai_output_usd_per_1m_tokens", 0.28)
        ),
    )

    return {
        "reply": ai_reply.reply,
        "confidence": ai_reply.confidence,
        "category": ai_reply.category,
        "sources": [s.model_dump() for s in sources],
        "handoff": handoff_info.model_dump(),
        "route": "llm",
        "budget_state": budget.state,
        "wechat_qr_url": ho_payload.get("wechat_qr_url"),
        "offline_message": ho_payload.get("offline_message"),
        "handoff_provider": actual_handoff_provider,
    }


async def _persist_assistant(
    db: AsyncSession,
    *,
    conversation: SupportConversation,
    reply_text: str,
    metadata: dict[str, Any],
) -> None:
    msg = SupportMessage(
        conversation_id=conversation.id,
        sender="assistant",
        body=reply_text,
        redacted_body=redact_pii(reply_text),
        metadata_json=metadata,
    )
    db.add(msg)
    conversation.message_count = (conversation.message_count or 0) + 1
    conversation.updated_at = datetime.now(timezone.utc)
    await db.flush()


async def _recent_history(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    limit: int = 6,
) -> list[dict[str, str]]:
    stmt = (
        select(SupportMessage)
        .where(SupportMessage.conversation_id == conversation_id)
        .order_by(SupportMessage.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = list(result.scalars())
    rows.reverse()
    return [
        {
            "sender": r.sender,
            "body": redact_pii(r.body or "")[:500],
        }
        for r in rows
    ]
