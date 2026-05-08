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

export type HandoffProvider = "email" | "chatwoot" | "wechat_kf"

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
