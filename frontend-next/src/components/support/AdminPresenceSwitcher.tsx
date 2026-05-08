"use client"

import { useState } from "react"

import { useAdminHeartbeat } from "./useAdminHeartbeat"
import type { PresenceStatus } from "@/lib/api/support"

/**
 * Topbar status pill — only renders for admin users.
 *
 * Click to open a small dropdown:
 *   ● 在线服务中  (online)
 *   ○ 暂停服务    (paused — heartbeat continues, but new tickets route
 *                   to WeChat QR fallback instead of in-product)
 *   ⚫ 下线        (offline — heartbeat stops)
 *
 * The hook drives heartbeat ticks; this component just exposes the
 * UX. Pass ``intervalMs`` (admin-configurable via /admin/support) so
 * the cadence can be adjusted without redeploying.
 */
export function AdminPresenceSwitcher({
  isAdmin,
  intervalMs = 30_000,
}: {
  isAdmin: boolean
  intervalMs?: number
}) {
  const [open, setOpen] = useState(false)
  const { status, setStatus, loaded } = useAdminHeartbeat({
    enabled: isAdmin,
    intervalMs,
  })

  if (!isAdmin || !loaded) return null

  const labelByStatus: Record<PresenceStatus, string> = {
    online: "在线服务中",
    paused: "暂停服务",
    offline: "下线",
  }
  const dotByStatus: Record<PresenceStatus, string> = {
    online: "bg-emerald-500",
    paused: "bg-amber-500",
    offline: "bg-muted-foreground",
  }

  const choose = async (next: PresenceStatus) => {
    setOpen(false)
    if (next !== status) {
      await setStatus(next)
    }
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={
          "flex items-center gap-2 rounded-full border border-border bg-card " +
          "px-3 py-1 text-xs hover:bg-muted transition"
        }
        title="客服在线状态"
      >
        <span aria-hidden className={"h-2 w-2 rounded-full " + dotByStatus[status]} />
        <span className="text-foreground">{labelByStatus[status]}</span>
        <span aria-hidden className="text-muted-foreground">▾</span>
      </button>

      {open ? (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div
            className={
              "absolute right-0 top-full z-50 mt-1 w-44 rounded-md border border-border " +
              "bg-card shadow-lg"
            }
          >
            {(Object.keys(labelByStatus) as PresenceStatus[]).map((opt) => (
              <button
                key={opt}
                type="button"
                onClick={() => void choose(opt)}
                className={
                  "flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-muted " +
                  (opt === status ? "font-medium" : "")
                }
              >
                <span aria-hidden className={"h-2 w-2 rounded-full " + dotByStatus[opt]} />
                <span className="flex-1 text-foreground">{labelByStatus[opt]}</span>
                {opt === status ? <span aria-hidden>✓</span> : null}
              </button>
            ))}
          </div>
        </>
      ) : null}
    </div>
  )
}
