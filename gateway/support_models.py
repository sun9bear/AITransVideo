"""Pydantic request/response schemas for the customer support API.

Public surface for ``gateway/support_api.py``. ORM models live in
``gateway/models.py`` (SupportConversation / SupportMessage / etc).

Design notes:
- Source types are validated at the API boundary; downstream services
  trust the parsed values.
- Reply ``sources`` is a typed list rather than free-form so the frontend
  can render different "evidence" badges per source kind without growing
  a string-matching switch.
- ``handoff`` is always present in the message reply, never NULL — easier
  for the UI to treat consistently.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# --- request/response shared bits -----------------------------------------


SupportChannel = Literal["web", "wechat", "email"]
SupportEntrypoint = Literal[
    "marketing_home",
    "pricing",
    "trial",
    "contact",
    "workspace",
    "task_detail",
    "billing",
    "help",
    "faq",
    "notification",
    "auth",
    "unknown",
]
HandoffProvider = Literal[
    "in_product",  # admin online → reply in /admin/support panel
    "wechat_qr",   # admin offline + QR uploaded → user scans QR
    "email",       # admin offline + no QR → log-only ticket
    "chatwoot",    # P3 (stub)
    "wechat_kf",   # P4/P5 (stub)
]
HandoffReason = Literal[
    "user_requested_human",
    "low_confidence",
    "sensitive_category",
    "repeated_unresolved",
    "policy_required",
    "abuse_review",
]


class ClientContext(BaseModel):
    """Optional per-message context the frontend forwards.

    ``page_url`` and ``job_id`` may differ from the conversation-level
    context if the user navigates while chatting; we record both.
    """

    page_url: str | None = Field(default=None, max_length=512)
    job_id: str | None = Field(default=None, max_length=64)
    notification_id: str | None = Field(default=None, max_length=64)


class SupportSource(BaseModel):
    """One piece of evidence backing the AI / template reply."""

    type: Literal[
        "faq",
        "plan_catalog",
        "legal_page",
        "job_status",
        "template",
        "notification",
    ]
    id: str = Field(max_length=128)


class HandoffSummary(BaseModel):
    """Always-present handoff hint on every message reply."""

    recommended: bool = False
    required: bool = False
    reason: HandoffReason | None = None


# --- /api/support/config --------------------------------------------------


class SupportConfigResponse(BaseModel):
    enabled: bool
    anonymous_enabled: bool
    ai_enabled: bool
    handoff_enabled: bool
    wechat_kf_enabled: bool
    max_messages_before_captcha: int
    greeting: str
    quick_questions: list[str]
    handoff_provider_default: HandoffProvider


# --- POST /api/support/conversations --------------------------------------


class CreateConversationRequest(BaseModel):
    channel: SupportChannel = "web"
    entrypoint: SupportEntrypoint = "unknown"
    page_url: str | None = Field(default=None, max_length=512)
    job_id: str | None = Field(default=None, max_length=64)
    notification_id: str | None = Field(default=None, max_length=64)


class CreateConversationResponse(BaseModel):
    conversation_id: str
    status: Literal["open", "waiting_human", "handled", "closed"]
    handoff_state: Literal[
        "none", "recommended", "requested", "created", "failed", "closed"
    ]


# --- POST /api/support/conversations/{id}/messages ------------------------


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    client_context: ClientContext = Field(default_factory=ClientContext)


class SendMessageResponse(BaseModel):
    reply: str
    confidence: float = Field(ge=0.0, le=1.0)
    category: str | None = None
    sources: list[SupportSource] = Field(default_factory=list)
    handoff: HandoffSummary = Field(default_factory=HandoffSummary)
    route: Literal["template", "faq", "llm", "handoff", "blocked"]
    budget_state: Literal["normal", "budget_exhausted"] = "normal"
    # Populated when an internal handoff (sensitive keyword / template
    # forced handoff / AI-recommended handoff) lands on the wechat_qr
    # provider — the widget should render the QR card.
    wechat_qr_url: str | None = None
    offline_message: str | None = None
    handoff_provider: HandoffProvider | None = None
    online_count: int | None = None


# --- POST /api/support/conversations/{id}/handoff -------------------------


class HandoffRequest(BaseModel):
    reason: HandoffReason = "user_requested_human"
    preferred_channel: HandoffProvider | None = None


class HandoffResponse(BaseModel):
    handoff_state: Literal["created", "failed", "requested"]
    provider: HandoffProvider
    provider_conversation_id: str | None = None
    message: str
    # When ``provider="wechat_qr"`` these fields tell the widget what
    # to render in the offline branch. Both null for other providers.
    wechat_qr_url: str | None = None
    offline_message: str | None = None
    # Number of admins currently online — useful for the widget to
    # show a "客服在线" badge even on the in-product path.
    online_count: int | None = None


# --- GET /api/support/conversations/{id}/messages -------------------------


class MessageView(BaseModel):
    id: str
    sender: Literal["user", "assistant", "human", "system"]
    body: str
    created_at: datetime


class ConversationDetail(BaseModel):
    id: str
    status: Literal["open", "waiting_human", "handled", "closed"]
    handoff_state: Literal[
        "none", "recommended", "requested", "created", "failed", "closed"
    ]
    messages: list[MessageView]


# --- Admin: GET / POST /api/admin/support/settings ------------------------


class SupportAdminSettings(BaseModel):
    """The admin-editable subset of support config (P1 §7.2 of the plan).

    Anything not in here stays as a hardcoded default or env var. Plan §7.2
    explicitly limits P1 to ~10 fields so the admin UI does not drift into
    a 60-row config form.
    """

    # Codex P2-1 (2026-05-08): both default off; admin must opt in.
    support_enabled: bool = False
    # Codex P2 round 2 (2026-05-08): expose anonymous-visitor toggle in
    # admin so operators don't have to redeploy with a new env var to
    # let pre-sales visitors chat. Default still off — admin must
    # explicitly enable.
    support_anonymous_enabled: bool = False
    support_ai_enabled: bool = False
    support_ai_model: str = "deepseek"
    support_ai_max_output_tokens: int = Field(default=400, ge=1, le=4000)
    support_ai_monthly_budget_usd: float = Field(default=50.0, ge=0)
    support_ai_input_usd_per_1m_tokens: float = Field(default=0.14, ge=0)
    support_ai_output_usd_per_1m_tokens: float = Field(default=0.28, ge=0)
    support_budget_exhausted_message: str = (
        "AI 客服当前繁忙，你可以先查看常见问题，或转人工客服处理。"
    )
    support_sensitive_keywords: list[str] = Field(
        default_factory=lambda: [
            "人工",
            "真人",
            "转客服",
            "找人",
            "退款",
            "投诉",
            "差评",
            "工信部",
            "315",
            "赔偿",
            "举报",
            "律师",
            "消协",
        ]
    )
    support_ops_email: str = "sxz999@proton.me"

    # ---------- Human handoff routing (L1, plan 2026-05-08 follow-up) -------
    # Frontend admin tabs ping POST /api/admin/support/heartbeat at this
    # cadence. Lower = better real-time signal but more requests.
    support_admin_heartbeat_interval_seconds: int = Field(default=30, ge=5, le=600)
    # Server treats an admin as "online" iff last_heartbeat is within
    # this window. Should be ≥ 2× heartbeat_interval to absorb network
    # jitter without false-offline flapping.
    support_admin_online_threshold_seconds: int = Field(default=60, ge=10, le=1200)
    # If user clicks 转人工 and gets routed to in-product chat, but no
    # human reply lands within this window, the widget falls back to
    # showing the WeChat QR. Set to 0 to disable the fallback timer.
    support_handoff_offline_fallback_minutes: int = Field(default=5, ge=0, le=120)
    # Shown to user when no admin is online (above the WeChat QR).
    support_offline_message: str = (
        "运营暂未在线，可扫码添加客服微信，我们尽快回复。"
    )


class AdminSupportSettingsResponse(BaseModel):
    settings: SupportAdminSettings
    budget_state: Literal["normal", "budget_exhausted"]
    spent_this_month_usd: float
    available_models: list[dict]


# --- Admin overview --------------------------------------------------------


class AdminSupportOverview(BaseModel):
    conversations_total: int
    conversations_today: int
    handoff_requests_total: int
    handoff_requests_pending: int
    ai_messages_this_month: int
    template_hits_this_month: int
    spent_this_month_usd: float
    budget_state: Literal["normal", "budget_exhausted"]


# --- Notifications ---------------------------------------------------------


NotificationScope = Literal["system", "user", "job"]
NotificationTopic = Literal[
    "billing", "account", "artifact", "support", "maintenance"
]
NotificationSeverity = Literal["info", "success", "warning", "error"]


class NotificationView(BaseModel):
    id: str
    scope: NotificationScope
    topic: NotificationTopic
    title: str
    body: str
    severity: NotificationSeverity
    job_id: str | None = None
    related_type: str | None = None
    related_id: str | None = None
    artifact_key: str | None = None
    action_url: str | None = None
    read: bool
    archived: bool
    expires_at: datetime | None = None
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationView]
    unread_count: int


class NotificationMarkReadRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    mark_all: bool = False


class NotificationArchiveRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


# ---------- Presence + offline routing (L1, plan 2026-05-08 follow-up) ------


PresenceStatusLiteral = Literal["online", "paused", "offline"]


class OnlineStatusResponse(BaseModel):
    """Public — used by SupportWidget to decide handoff routing UI."""

    online: bool
    online_count: int
    has_wechat_qr: bool
    offline_message: str
    handoff_offline_fallback_minutes: int


class HeartbeatRequest(BaseModel):
    # Optional explicit status; if omitted the heartbeat preserves
    # whatever status was already set (default new admin → online).
    status: PresenceStatusLiteral | None = None


class PresenceView(BaseModel):
    user_id: str
    status: PresenceStatusLiteral
    last_heartbeat_at: datetime
    seconds_since_last_heartbeat: int


class SetPresenceStatusRequest(BaseModel):
    status: PresenceStatusLiteral


class WeChatQrInfoResponse(BaseModel):
    has_qr: bool
    url: str | None = None
    uploaded_at: datetime | None = None
    size_bytes: int | None = None


class AdminReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    # When true, also marks the underlying handoff_request status="handled".
    mark_handled: bool = False


class AdminReplyResponse(BaseModel):
    message_id: str
    notification_dispatched: bool
    handoff_status: str | None = None


class MyOpenConversationView(BaseModel):
    conversation_id: str
    status: Literal["open", "waiting_human", "handled", "closed"]
    handoff_state: Literal[
        "none", "recommended", "requested", "created", "failed", "closed"
    ]
    last_message_preview: str
    updated_at: datetime


class MyOpenConversationsResponse(BaseModel):
    conversations: list[MyOpenConversationView]


# ---------- System announcements (plan 2026-05-08 §16.7 follow-up) -------


AnnouncementStatus = Literal["draft", "sent", "archived"]


class AnnouncementInput(BaseModel):
    """Composer payload — used for create draft AND update draft."""

    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=4000)
    topic: NotificationTopic = "maintenance"
    severity: NotificationSeverity = "info"
    action_url: str | None = Field(default=None, max_length=512)
    audience_kind: str = Field(min_length=1, max_length=32)
    audience_params: dict | None = None


class AnnouncementView(BaseModel):
    id: str
    title: str
    body: str
    topic: NotificationTopic
    severity: NotificationSeverity
    action_url: str | None = None
    audience_kind: str
    audience_params: dict | None = None
    status: AnnouncementStatus
    sent_at: datetime | None = None
    recipient_count: int | None = None
    parent_id: str | None = None
    created_by_admin_id: str | None = None
    created_at: datetime
    updated_at: datetime
    # Live read / archive stats from user_notifications (only meaningful
    # after status="sent"). Fields default to None for drafts.
    stats: dict | None = None


class AudienceParamSpec(BaseModel):
    key: str
    type: str
    default: int | str | float
    min: int | float | None = None
    max: int | float | None = None


class AudienceKindSpec(BaseModel):
    kind: str
    label: str
    group: str
    params: list[AudienceParamSpec] = Field(default_factory=list)


class AudienceCatalogResponse(BaseModel):
    kinds: list[AudienceKindSpec]


class AudiencePreviewResponse(BaseModel):
    audience_kind: str
    audience_params: dict | None = None
    count: int


class SendAnnouncementResponse(BaseModel):
    announcement_id: str
    audience_size: int
    newly_notified: int
    skipped_already_notified: int


class RecallAnnouncementResponse(BaseModel):
    announcement_id: str
    deleted_count: int


class AnnouncementListResponse(BaseModel):
    items: list[AnnouncementView]
