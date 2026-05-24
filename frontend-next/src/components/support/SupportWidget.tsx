"use client"

import { usePathname } from "next/navigation"
import { useCallback, useEffect, useState } from "react"

import { useSession } from "@/components/providers/session-provider"
import {
  getOnlineStatus,
  getSupportConfig,
  type OnlineStatus,
  type SupportConfig,
} from "@/lib/api/support"
import { usePollingTask } from "@/lib/react/usePollingTask"

import { SupportConversationPanel } from "./SupportConversationPanel"
import { SupportLauncher } from "./SupportLauncher"
import { ENTRYPOINT_FROM_PATH, FALLBACK_GREETING } from "./support-copy"

/**
 * Top-level support widget — laucher + panel.
 *
 * Renders client-side only. Lives in the marketing and (app) layouts;
 * the (auth) layout deliberately does NOT mount this so the login form
 * stays uncluttered (plan §8.2).
 *
 * The widget self-disables if /api/support/config returns enabled=false
 * (admin kill switch).
 *
 * Codex round-3 polish (2026-05-08): if the admin has support_enabled=true
 * but support_anonymous_enabled=false, an anonymous visitor would still
 * see the launcher and only hit 401 after typing a message. We hide the
 * launcher in that case so the UX matches what the backend will actually
 * accept. Logged-in users always see the widget regardless of the
 * anonymous toggle.
 */
export function SupportWidget() {
  const pathname = usePathname()
  const { user, loading: sessionLoading } = useSession()
  const [config, setConfig] = useState<SupportConfig | null>(null)
  const [onlineStatus, setOnlineStatus] = useState<OnlineStatus | null>(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let cancelled = false
    getSupportConfig()
      .then((cfg) => {
        if (!cancelled) setConfig(cfg)
      })
      .catch(() => {
        // Silent failure — if /api/support/config is down, we just don't
        // render. Better than confusing the user with an error toast.
      })
    return () => {
      cancelled = true
    }
  }, [])

  const refreshOnlineStatus = useCallback(async () => {
    try {
      const s = await getOnlineStatus()
      setOnlineStatus(s)
    } catch {
      // silent - keep last known status
    }
  }, [])

  // Poll online status when widget is open. Drives the green/gray dot
  // in the header and the offline branch routing decision.
  usePollingTask(refreshOnlineStatus, {
    enabled: open,
    intervalMs: 30_000,
  })

  if (!config || !config.enabled) return null
  // Wait for the session check before deciding visibility — flashing the
  // launcher only to remove it on auth resolve is uglier than a 200ms
  // delay.
  if (sessionLoading) return null
  // Anonymous gating (Codex round-3): if anonymous chat is OFF and the
  // visitor is not logged in, don't render. The marketing pages still
  // have email + the contact page as backup channels.
  if (!user && !config.anonymous_enabled) return null

  // The (auth) layout never mounts this component, so reaching here for
  // /login or /register would be a misuse, but we still suppress the UI
  // as a defensive measure if a future layout change wires it in.
  if (pathname?.startsWith("/login") || pathname?.startsWith("/register")) {
    return null
  }

  // We currently don't compute unread support replies — always false.
  // The notifications system handles "你有新消息" reminders separately.
  const unreadHint = false

  const jobId = (() => {
    if (!pathname) return null
    const match = pathname.match(/^\/workspace\/([a-zA-Z0-9_\-]{1,64})/)
    return match ? match[1] : null
  })()

  return (
    <>
      <SupportLauncher
        open={open}
        onToggle={() => setOpen((v) => !v)}
        unreadHint={unreadHint}
      />
      <SupportConversationPanel
        visible={open}
        onRequestClose={() => setOpen(false)}
        greeting={config.greeting || FALLBACK_GREETING}
        quickQuestions={config.quick_questions}
        entrypoint={ENTRYPOINT_FROM_PATH(pathname)}
        pageUrl={pathname ?? ""}
        jobId={jobId}
        notificationId={null}
        budgetState={null}
        onlineStatus={onlineStatus}
        isLoggedIn={user !== null}
      />
    </>
  )
}
