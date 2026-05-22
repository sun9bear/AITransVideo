"use client"

import Link from "next/link"
import { Bell } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { getUnreadCount } from "@/lib/api/notifications"

/**
 * Header bell with live unread count.
 *
 * Polls /api/notifications/unread-count every 30s when the tab is
 * visible. Backs off when tab is hidden to save requests. Clicking
 * navigates to /notifications.
 *
 * Side effect: when ``count`` increases (new notification arrived
 * while user is on another tab) the document.title flashes
 * `(N) <original>` so the user notices.
 *
 * Plan 2026-05-08 §16.7 P1 polish.
 */
export function NotificationBell({ isAuthenticated }: { isAuthenticated: boolean }) {
  const [count, setCount] = useState(0)
  const lastCountRef = useRef(0)
  const originalTitleRef = useRef<string | null>(null)
  const flashIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Capture original title on mount.
  useEffect(() => {
    if (typeof document === "undefined") return
    if (originalTitleRef.current === null) {
      originalTitleRef.current = document.title
    }
    return () => {
      // Restore original title on unmount.
      if (originalTitleRef.current !== null) {
        document.title = originalTitleRef.current
      }
      if (flashIntervalRef.current) {
        clearInterval(flashIntervalRef.current)
        flashIntervalRef.current = null
      }
    }
  }, [])

  // Poll unread count.
  useEffect(() => {
    if (!isAuthenticated) {
      return
    }
    let cancelled = false
    const tick = async () => {
      if (typeof document !== "undefined" && document.hidden) {
        // Tab hidden — skip the request to save bandwidth. We'll catch
        // up on the next visibility change.
        return
      }
      try {
        const r = await getUnreadCount()
        if (!cancelled) setCount(r.unread_count || 0)
      } catch {
        // 401 for anonymous (shouldn't happen since we gate on isAuthenticated)
      }
    }
    void tick()
    const t = setInterval(tick, 30_000)
    // Refresh on visibility regain — user comes back to tab, show fresh count.
    const onVis = () => {
      if (!document.hidden) void tick()
    }
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVis)
    }
    return () => {
      cancelled = true
      clearInterval(t)
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVis)
      }
    }
  }, [isAuthenticated])

  // Flash document.title when count crosses 0 → N or N → N+1 while
  // tab is hidden. Stop flashing on visibility regain.
  useEffect(() => {
    if (typeof document === "undefined") return
    const original = originalTitleRef.current ?? document.title
    const isIncrement = count > lastCountRef.current
    lastCountRef.current = count

    // Update static title to reflect count.
    document.title = count > 0 ? `(${count}) ${original}` : original

    // If count increased AND tab is hidden, alternate "(N) … ↔ ✦ 新通知"
    // until visibility comes back.
    if (
      isIncrement &&
      count > 0 &&
      typeof document !== "undefined" &&
      document.hidden
    ) {
      if (flashIntervalRef.current) clearInterval(flashIntervalRef.current)
      let toggle = false
      flashIntervalRef.current = setInterval(() => {
        if (!document.hidden) {
          // User came back; stop flashing.
          if (flashIntervalRef.current) {
            clearInterval(flashIntervalRef.current)
            flashIntervalRef.current = null
          }
          document.title = count > 0 ? `(${count}) ${original}` : original
          return
        }
        document.title = toggle
          ? `(${count}) ${original}`
          : `✦ 新通知 · ${original}`
        toggle = !toggle
      }, 1500)
    } else if (flashIntervalRef.current && (count === 0 || !document.hidden)) {
      clearInterval(flashIntervalRef.current)
      flashIntervalRef.current = null
      document.title = count > 0 ? `(${count}) ${original}` : original
    }

    return () => {
      // Don't clear here — the next render will manage. The only true
      // cleanup is in the unmount effect above.
    }
  }, [count])

  if (!isAuthenticated) return null

  return (
    <Link
      href="/notifications"
      aria-label={count > 0 ? `通知（${count} 条未读）` : "通知"}
      className="relative inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
    >
      <Bell className="h-4 w-4" aria-hidden="true" />
      {count > 0 ? (
        <span
          aria-hidden
          className="absolute right-0 top-0 inline-flex min-w-[16px] items-center justify-center rounded-full bg-[color:var(--cinnabar,#C73E3A)] px-1 text-[10px] font-semibold leading-4 text-white"
          style={{ transform: "translate(40%, -30%)" }}
        >
          {count > 99 ? "99+" : count}
        </span>
      ) : null}
    </Link>
  )
}

/**
 * Just the unread count, no UI — for places that want to render a
 * custom badge (e.g. sidebar nav).
 */
export function useNotificationUnreadCount(isAuthenticated: boolean) {
  const [count, setCount] = useState(0)

  useEffect(() => {
    if (!isAuthenticated) {
      return
    }
    let cancelled = false
    const tick = async () => {
      if (typeof document !== "undefined" && document.hidden) return
      try {
        const r = await getUnreadCount()
        if (!cancelled) setCount(r.unread_count || 0)
      } catch {
        // silent
      }
    }
    void tick()
    const t = setInterval(tick, 30_000)
    const onVis = () => {
      if (typeof document !== "undefined" && !document.hidden) void tick()
    }
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVis)
    }
    return () => {
      cancelled = true
      clearInterval(t)
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVis)
      }
    }
  }, [isAuthenticated])

  return isAuthenticated ? count : 0
}
