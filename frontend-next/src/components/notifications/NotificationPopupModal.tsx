"use client"

import { Link } from "@/i18n/navigation"
import { useEffect, useRef, useState } from "react"

import {
  dismissPopup,
  listActivePopups,
  type PopupNotification,
} from "@/lib/api/notifications"

/**
 * Modal renderer for "popup=true" notifications.
 *
 * Plan 2026-05-08 §16.7 follow-up §"管理员发通知的时候，加一个选项：
 * 是否弹窗".
 *
 * Lifecycle:
 *   1. On mount, fetch /api/notifications/popups (limit 5).
 *   2. If any returned, render the FIRST as a modal overlay.
 *   3. User dismisses (close / 稍后 → POST /dismiss preserving unread,
 *      OR 查看详情 → POST /dismiss?mark_read=true and navigate).
 *   4. Pop the dismissed item from the local queue and show the next.
 *   5. When queue is empty, hide.
 *
 * Mounted in AppShell so every authenticated route picks it up. Not
 * shown for anonymous visitors (popup notifications are per-user).
 */
export function NotificationPopupModal({
  isAuthenticated,
}: {
  isAuthenticated: boolean
}) {
  const [queue, setQueue] = useState<PopupNotification[]>([])
  const [busy, setBusy] = useState(false)
  const fetchedRef = useRef(false)

  useEffect(() => {
    if (!isAuthenticated) {
      setQueue([])
      fetchedRef.current = false
      return
    }
    if (fetchedRef.current) return // only fetch once per mount
    fetchedRef.current = true
    let cancelled = false
    listActivePopups()
      .then((r) => {
        if (cancelled) return
        setQueue(r.items)
      })
      .catch(() => {
        // 401 if anonymous (we already gated above) — silent.
      })
    return () => {
      cancelled = true
    }
  }, [isAuthenticated])

  if (!isAuthenticated) return null
  if (queue.length === 0) return null

  const current = queue[0]

  const dismissCurrent = async (markRead: boolean) => {
    setBusy(true)
    try {
      await dismissPopup(current.id, { markRead })
    } catch {
      // If dismiss fails server-side, drop locally anyway so the
      // user isn't stuck looking at an immortal modal. Server will
      // try to surface again on next page load.
    } finally {
      setBusy(false)
      setQueue((prev) => prev.slice(1))
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="notification-popup-title"
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/45 backdrop-blur-sm p-4"
    >
      <div
        className={
          "w-full max-w-md rounded-xl border border-border bg-background shadow-2xl " +
          severityRingClass(current.severity)
        }
      >
        <header className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0 flex-1">
            <p className={"text-[10px] font-semibold uppercase tracking-wide " + severityLabelClass(current.severity)}>
              {severityLabel(current.severity)} · 系统通知
            </p>
            <h2
              id="notification-popup-title"
              className="mt-1 text-base font-semibold text-foreground"
            >
              {current.title}
            </h2>
          </div>
          <button
            type="button"
            onClick={() => void dismissCurrent(false)}
            aria-label="关闭"
            disabled={busy}
            className="text-xl leading-none text-muted-foreground hover:text-foreground disabled:opacity-50"
          >
            ×
          </button>
        </header>

        <div className="max-h-[50vh] overflow-y-auto whitespace-pre-wrap px-5 py-4 text-sm leading-relaxed text-foreground">
          {current.body}
        </div>

        <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-border bg-muted/20 px-5 py-3 text-xs">
          <span className="flex-1 text-[11px] text-muted-foreground">
            {queue.length > 1 ? `还有 ${queue.length - 1} 条公告` : ""}
          </span>
          <button
            type="button"
            disabled={busy}
            onClick={() => void dismissCurrent(false)}
            className="rounded border border-border bg-card px-3 py-1.5 text-foreground hover:bg-muted disabled:opacity-50"
          >
            稍后
          </button>
          {current.action_url ? (
            <Link
              href={current.action_url}
              onClick={() => void dismissCurrent(true)}
              className="rounded bg-[color:var(--cinnabar,#C73E3A)] px-3 py-1.5 font-medium text-white"
            >
              查看详情
            </Link>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => void dismissCurrent(true)}
              className="rounded bg-[color:var(--cinnabar,#C73E3A)] px-3 py-1.5 font-medium text-white disabled:opacity-50"
            >
              已知晓
            </button>
          )}
        </footer>
      </div>
    </div>
  )
}

function severityRingClass(severity: PopupNotification["severity"]): string {
  switch (severity) {
    case "success":
      return "ring-1 ring-emerald-300/60"
    case "warning":
      return "ring-1 ring-amber-300/60"
    case "error":
      return "ring-2 ring-destructive/60"
    default:
      return ""
  }
}

function severityLabelClass(severity: PopupNotification["severity"]): string {
  switch (severity) {
    case "success":
      return "text-emerald-700"
    case "warning":
      return "text-amber-700"
    case "error":
      return "text-destructive"
    default:
      return "text-muted-foreground"
  }
}

function severityLabel(severity: PopupNotification["severity"]): string {
  switch (severity) {
    case "success":
      return "✓ 完成"
    case "warning":
      return "⚠ 提示"
    case "error":
      return "✦ 重要"
    default:
      return "ℹ 通知"
  }
}
