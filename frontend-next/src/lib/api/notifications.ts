/**
 * API client for the notification center (plan 2026-05-08 §16).
 *
 * Authenticated only. Calls return 401 for anonymous users.
 */

export type NotificationScope = "system" | "user" | "job"
export type NotificationTopic =
  | "billing"
  | "account"
  | "artifact"
  | "support"
  | "maintenance"
export type NotificationSeverity = "info" | "success" | "warning" | "error"

export interface NotificationView {
  id: string
  scope: NotificationScope
  topic: NotificationTopic
  title: string
  body: string
  severity: NotificationSeverity
  job_id: string | null
  related_type: string | null
  related_id: string | null
  artifact_key: string | null
  action_url: string | null
  read: boolean
  archived: boolean
  expires_at: string | null
  created_at: string
}

export interface NotificationListResponse {
  items: NotificationView[]
  unread_count: number
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
        : `notifications API request failed: ${res.status}`
    throw new Error(detail)
  }
  return parsed as T
}

export async function listNotifications(
  options: { include_archived?: boolean; limit?: number } = {},
): Promise<NotificationListResponse> {
  const params = new URLSearchParams()
  if (options.include_archived) params.set("include_archived", "true")
  if (options.limit) params.set("limit", String(options.limit))
  const qs = params.toString() ? `?${params.toString()}` : ""
  return fetchJson<NotificationListResponse>(`/api/notifications${qs}`)
}

export async function getUnreadCount(): Promise<{ unread_count: number }> {
  return fetchJson<{ unread_count: number }>("/api/notifications/unread-count")
}

export async function markNotificationsRead(
  body: { ids?: string[]; mark_all?: boolean },
): Promise<{ updated: number }> {
  return fetchJson<{ updated: number }>("/api/notifications/read", {
    method: "POST",
    body: JSON.stringify({
      ids: body.ids ?? [],
      mark_all: body.mark_all ?? false,
    }),
  })
}

export async function archiveNotifications(
  ids: string[],
): Promise<{ archived: number }> {
  return fetchJson<{ archived: number }>("/api/notifications/archive", {
    method: "POST",
    body: JSON.stringify({ ids }),
  })
}

// --- Popup notifications (modal on next page load) ----------------------

export interface PopupNotification {
  id: string
  title: string
  body: string
  severity: NotificationSeverity
  topic: NotificationTopic
  action_url: string | null
  created_at: string
}

export async function listActivePopups(): Promise<{ items: PopupNotification[] }> {
  return fetchJson<{ items: PopupNotification[] }>("/api/notifications/popups")
}

export async function dismissPopup(
  id: string,
  options: { markRead?: boolean } = {},
): Promise<{
  notification_id: string
  dismissed_at: string
  also_marked_read: boolean
}> {
  const qs = options.markRead ? "?mark_read=true" : ""
  return fetchJson(
    `/api/notifications/popups/${encodeURIComponent(id)}/dismiss${qs}`,
    { method: "POST" },
  )
}
