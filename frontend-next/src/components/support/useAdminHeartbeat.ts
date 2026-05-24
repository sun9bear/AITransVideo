"use client"

import { useCallback, useEffect, useRef, useState } from "react"

import {
  adminGetMyPresence,
  adminHeartbeat,
  adminSetPresenceStatus,
  type PresenceStatus,
} from "@/lib/api/support"

/**
 * Admin-only heartbeat manager.
 *
 * Fires POST /api/admin/support/heartbeat at the configured cadence
 * (default 30s; admin can adjust in /admin/support). Stops when:
 *   - status is "offline" (admin explicitly went off duty)
 *   - tab is hidden for > 60s (admin walked away — no need to keep
 *     pinging; we'll resume on visibility change)
 *   - component unmounts
 *
 * Returns ``{status, setStatus}`` so a topbar component can render
 * the status pill and let the admin toggle.
 *
 * Plan: 2026-05-08 follow-up §"管理员/运营/客服只要登录，就显示在线".
 */
export function useAdminHeartbeat({
  enabled,
  intervalMs = 30_000,
}: {
  enabled: boolean
  intervalMs?: number
}) {
  const [status, setStatusState] = useState<PresenceStatus>("online")
  const [loaded, setLoaded] = useState(false)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const lastVisibleRef = useRef<number | null>(null)
  const heartbeatInFlightRef = useRef(false)

  // Initial: fetch existing status (so a page reload doesn't reset
  // a paused admin back to online).
  useEffect(() => {
    if (!enabled) {
      return
    }
    let cancelled = false
    adminGetMyPresence()
      .then((p) => {
        if (cancelled) return
        if (p) setStatusState(p.status)
        setLoaded(true)
      })
      .catch(() => {
        // First-call may 404 if admin never heartbeated — fine.
        if (!cancelled) setLoaded(true)
      })
    return () => {
      cancelled = true
    }
  }, [enabled])

  // Schedule heartbeat ticks.
  useEffect(() => {
    if (!enabled || !loaded) return
    if (status === "offline") {
      if (timerRef.current) {
        clearInterval(timerRef.current)
        timerRef.current = null
      }
      return
    }

    const tick = async () => {
      // Throttle: if tab has been hidden for > 60s, don't waste a request.
      const now = Date.now()
      const hiddenFor = now - (lastVisibleRef.current ?? now)
      if (typeof document !== "undefined" && document.hidden && hiddenFor > 60_000) {
        return
      }
      if (heartbeatInFlightRef.current) return
      heartbeatInFlightRef.current = true
      try {
        await adminHeartbeat()
      } catch {
        // Silent — heartbeat failure is non-critical (next tick retries).
      } finally {
        heartbeatInFlightRef.current = false
      }
    }

    // Fire immediately on mount/status change.
    void tick()
    timerRef.current = setInterval(tick, Math.max(5_000, intervalMs))
    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current)
        timerRef.current = null
      }
    }
  }, [enabled, loaded, status, intervalMs])

  // Track tab visibility so we don't ping while in background.
  useEffect(() => {
    if (typeof document === "undefined") return
    lastVisibleRef.current = Date.now()
    const handler = () => {
      if (!document.hidden) {
        lastVisibleRef.current = Date.now()
      }
    }
    document.addEventListener("visibilitychange", handler)
    return () => document.removeEventListener("visibilitychange", handler)
  }, [])

  const setStatus = useCallback(async (next: PresenceStatus) => {
    setStatusState(next) // optimistic
    try {
      await adminSetPresenceStatus(next)
    } catch {
      // revert optimistic on failure — fall back to a refetch.
      try {
        const p = await adminGetMyPresence()
        if (p) setStatusState(p.status)
      } catch {
        // give up; UI may be slightly stale until next page load.
      }
    }
  }, [])

  return { status, setStatus, loaded: !enabled || loaded }
}
