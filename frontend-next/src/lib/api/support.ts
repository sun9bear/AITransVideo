/**
 * API client for the customer support endpoints (plan 2026-05-08).
 *
 * Hits the gateway's /api/support/* surface directly via fetch — no
 * shared ApiClient because the gateway-rooted /api endpoints are not
 * under the /job-api prefix that ApiClient is configured for.
 *
 * All calls use credentials: "include" so the session cookie + the
 * anonymous device cookie (avt_support_anon, set by the gateway) ride
 * along. The gateway decides per-endpoint whether anonymous access is
 * allowed (config.anonymous_enabled).
 */

export type HandoffProvider =
  | "in_product"
  | "wechat_qr"
  | "email"
  | "chatwoot"
  | "wechat_kf"

export type PresenceStatus = "online" | "paused" | "offline"

export interface OnlineStatus {
  online: boolean
  online_count: number
  has_wechat_qr: boolean
  offline_message: string
  handoff_offline_fallback_minutes: number
}

export interface PresenceView {
  user_id: string
  status: PresenceStatus
  last_heartbeat_at: string
  seconds_since_last_heartbeat: number
}

export interface MyOpenConversation {
  conversation_id: string
  status: "open" | "waiting_human" | "handled" | "closed"
  handoff_state:
    | "none"
    | "recommended"
    | "requested"
    | "created"
    | "failed"
    | "closed"
  last_message_preview: string
  updated_at: string
}

export interface SupportConfig {
  enabled: boolean
  anonymous_enabled: boolean
  ai_enabled: boolean
  handoff_enabled: boolean
  wechat_kf_enabled: boolean
  max_messages_before_captcha: number
  greeting: string
  quick_questions: string[]
  handoff_provider_default: HandoffProvider
}

export interface SupportSource {
  type:
    | "faq"
    | "plan_catalog"
    | "legal_page"
    | "job_status"
    | "template"
    | "notification"
  id: string
}

export interface HandoffSummary {
  recommended: boolean
  required: boolean
  reason: string | null
}

export interface SupportMessageView {
  id: string
  sender: "user" | "assistant" | "human" | "system"
  body: string
  created_at: string
}

export interface ConversationDetail {
  id: string
  status: "open" | "waiting_human" | "handled" | "closed"
  handoff_state:
    | "none"
    | "recommended"
    | "requested"
    | "created"
    | "failed"
    | "closed"
  messages: SupportMessageView[]
}

export interface CreateConversationRequest {
  channel?: "web" | "wechat" | "email"
  entrypoint:
    | "marketing_home"
    | "pricing"
    | "trial"
    | "contact"
    | "workspace"
    | "task_detail"
    | "billing"
    | "help"
    | "faq"
    | "notification"
    | "auth"
    | "unknown"
  page_url?: string | null
  job_id?: string | null
  notification_id?: string | null
}

export interface CreateConversationResponse {
  conversation_id: string
  status: ConversationDetail["status"]
  handoff_state: ConversationDetail["handoff_state"]
}

export interface SendMessageRequest {
  message: string
  client_context?: {
    page_url?: string | null
    job_id?: string | null
    notification_id?: string | null
  }
}

export interface SendMessageResponse {
  reply: string
  confidence: number
  category: string | null
  sources: SupportSource[]
  handoff: HandoffSummary
  route: "template" | "faq" | "llm" | "handoff" | "blocked"
  budget_state: "normal" | "budget_exhausted"
  wechat_qr_url: string | null
  offline_message: string | null
  handoff_provider: HandoffProvider | null
  online_count: number | null
}

export interface HandoffRequest {
  reason:
    | "user_requested_human"
    | "low_confidence"
    | "sensitive_category"
    | "repeated_unresolved"
    | "policy_required"
    | "abuse_review"
  preferred_channel?: HandoffProvider
}

export interface HandoffResponse {
  handoff_state: "created" | "failed" | "requested"
  provider: HandoffProvider
  provider_conversation_id: string | null
  message: string
  wechat_qr_url: string | null
  offline_message: string | null
  online_count: number | null
}

async function fetchJson<T>(
  url: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set("Accept", "application/json")
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json; charset=utf-8")
  }
  const res = await fetch(url, {
    ...init,
    credentials: "include",
    headers,
  })
  const raw = await res.text()
  let parsed: unknown = null
  if (raw) {
    try {
      parsed = JSON.parse(raw)
    } catch {
      parsed = raw
    }
  }
  if (!res.ok) {
    const detail =
      parsed && typeof parsed === "object" && "detail" in parsed
        ? String((parsed as { detail: unknown }).detail)
        : `support API request failed: ${res.status}`
    throw new Error(detail)
  }
  return parsed as T
}

export async function getSupportConfig(): Promise<SupportConfig> {
  return fetchJson<SupportConfig>("/api/support/config")
}

export async function createSupportConversation(
  body: CreateConversationRequest,
): Promise<CreateConversationResponse> {
  return fetchJson<CreateConversationResponse>("/api/support/conversations", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export async function sendSupportMessage(
  conversationId: string,
  body: SendMessageRequest,
): Promise<SendMessageResponse> {
  return fetchJson<SendMessageResponse>(
    `/api/support/conversations/${encodeURIComponent(conversationId)}/messages`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  )
}

export async function getSupportConversation(
  conversationId: string,
): Promise<ConversationDetail> {
  return fetchJson<ConversationDetail>(
    `/api/support/conversations/${encodeURIComponent(conversationId)}`,
  )
}

export async function requestSupportHandoff(
  conversationId: string,
  body: HandoffRequest,
): Promise<HandoffResponse> {
  return fetchJson<HandoffResponse>(
    `/api/support/conversations/${encodeURIComponent(conversationId)}/handoff`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  )
}

export async function getOnlineStatus(): Promise<OnlineStatus> {
  return fetchJson<OnlineStatus>("/api/support/online-status")
}

export async function listMyOpenConversations(): Promise<{
  conversations: MyOpenConversation[]
}> {
  return fetchJson<{ conversations: MyOpenConversation[] }>(
    "/api/support/conversations/my/open",
  )
}

// --- Admin-side ---

export async function adminHeartbeat(status?: PresenceStatus): Promise<PresenceView> {
  return fetchJson<PresenceView>("/api/admin/support/heartbeat", {
    method: "POST",
    body: JSON.stringify(status ? { status } : {}),
  })
}

export async function adminGetMyPresence(): Promise<PresenceView | null> {
  return fetchJson<PresenceView | null>("/api/admin/support/presence/me")
}

export async function adminSetPresenceStatus(
  status: PresenceStatus,
): Promise<PresenceView> {
  return fetchJson<PresenceView>("/api/admin/support/presence/status", {
    method: "POST",
    body: JSON.stringify({ status }),
  })
}

export interface WeChatQrInfo {
  has_qr: boolean
  url: string | null
  uploaded_at: string | null
  size_bytes: number | null
}

export async function adminGetWechatQrInfo(): Promise<WeChatQrInfo> {
  return fetchJson<WeChatQrInfo>("/api/admin/support/wechat-qr")
}

export async function adminUploadWechatQr(file: File): Promise<WeChatQrInfo> {
  const form = new FormData()
  form.append("file", file)
  const res = await fetch("/api/admin/support/wechat-qr", {
    method: "POST",
    credentials: "include",
    body: form,
  })
  const raw = await res.text()
  let parsed: unknown = null
  if (raw) {
    try {
      parsed = JSON.parse(raw)
    } catch {
      parsed = raw
    }
  }
  if (!res.ok) {
    const detail =
      parsed && typeof parsed === "object" && "detail" in parsed
        ? String((parsed as { detail: unknown }).detail)
        : `wechat-qr upload failed: ${res.status}`
    throw new Error(detail)
  }
  return parsed as WeChatQrInfo
}

export async function adminDeleteWechatQr(): Promise<{ removed: boolean }> {
  return fetchJson<{ removed: boolean }>("/api/admin/support/wechat-qr", {
    method: "DELETE",
  })
}

export interface AdminConversationDetail {
  conversation: {
    id: string
    user_id: string | null
    anonymous_id: string | null
    channel: string
    entrypoint: string | null
    page_url: string | null
    job_id: string | null
    status: string
    handoff_state: string
    handoff_provider: string | null
    created_at: string
    updated_at: string
  }
  user: {
    id: string
    display_name: string
    email: string
    phone_number: string | null
    plan_code: string | null
    role: string | null
  } | null
  messages: Array<{
    id: string
    sender: "user" | "assistant" | "human" | "system"
    body: string
    created_at: string
    metadata: Record<string, unknown> | null
  }>
}

export async function adminGetConversation(
  conversationId: string,
): Promise<AdminConversationDetail> {
  return fetchJson<AdminConversationDetail>(
    `/api/admin/support/conversations/${encodeURIComponent(conversationId)}`,
  )
}

export async function adminReplyToConversation(
  conversationId: string,
  body: string,
  markHandled = false,
): Promise<{
  message_id: string
  notification_dispatched: boolean
  handoff_status: string | null
}> {
  return fetchJson(
    `/api/admin/support/conversations/${encodeURIComponent(conversationId)}/reply`,
    {
      method: "POST",
      body: JSON.stringify({ body, mark_handled: markHandled }),
    },
  )
}

export async function adminListHandoffs(
  status: "pending" | "created" | "failed" | "closed" | null = null,
): Promise<{ items: Array<{
  id: string
  conversation_id: string
  provider: string
  reason: string
  summary: string | null
  status: string
  created_at: string
  updated_at: string
}> }> {
  const qs = status ? `?status=${status}` : ""
  return fetchJson(`/api/admin/support/handoffs${qs}`)
}

export async function adminCloseHandoff(handoffId: string): Promise<{ status: string }> {
  return fetchJson(
    `/api/admin/support/handoffs/${encodeURIComponent(handoffId)}/close`,
    { method: "POST" },
  )
}
