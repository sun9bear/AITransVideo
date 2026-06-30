"use client"

/**
 * Notification center page (plan 2026-05-08 §16.4).
 *
 * Authenticated only. Lists user / job notifications, supports
 * mark-as-read / archive, and renders click-through links via
 * `action_url`.
 *
 * P1 scope (per §16.7):
 * - Single page (no system-announcement carousel).
 * - Read / unread filter via local state, not URL.
 * - Filter by topic (chips) — used by the small badge counts.
 *
 * The notification stream is the user-visible projection of pipeline
 * events; the authoritative status of any task remains the polling-based
 * job detail view (§16.4 boundary note).
 */

import { useEffect, useMemo, useState } from "react"
import { useTranslations } from "next-intl"

import {
  archiveNotifications,
  listNotifications,
  markNotificationsRead,
  type NotificationView,
} from "@/lib/api/notifications"
import { useIntlLocale } from "@/lib/intl-locale"

const TOPIC_KEYS = ["billing", "account", "artifact", "support", "maintenance"] as const

const SEVERITY_COLORS: Record<string, string> = {
  info: "border-border bg-card",
  success: "border-green-300/40 bg-green-50/50",
  warning: "border-yellow-300/60 bg-yellow-50/40",
  error: "border-red-300/60 bg-red-50/40",
}

export default function NotificationsPage() {
  const t = useTranslations("appNotifications")
  const topicLabel = (topic: string): string =>
    (TOPIC_KEYS as readonly string[]).includes(topic)
      ? t(`topic.${topic}` as Parameters<typeof t>[0])
      : topic
  const [items, setItems] = useState<NotificationView[]>([])
  const [unread, setUnread] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<"all" | "unread">("all")
  const [topicFilter, setTopicFilter] = useState<string | null>(null)

  const reload = async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await listNotifications({ limit: 100 })
      setItems(data.items)
      setUnread(data.unread_count)
    } catch (err) {
      setError(err instanceof Error ? err.message : t("error.loadFailed"))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  const formatLocale = useIntlLocale()

  const visible = useMemo(() => {
    return items.filter((n) => {
      if (filter === "unread" && n.read) return false
      if (topicFilter && n.topic !== topicFilter) return false
      return true
    })
  }, [items, filter, topicFilter])

  const topicCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const n of items) {
      counts[n.topic] = (counts[n.topic] ?? 0) + (n.read ? 0 : 1)
    }
    return counts
  }, [items])

  const handleMarkAllRead = async () => {
    try {
      await markNotificationsRead({ mark_all: true })
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : t("error.markFailed"))
    }
  }

  const handleMarkOne = async (id: string) => {
    try {
      await markNotificationsRead({ ids: [id] })
      setItems((prev) =>
        prev.map((n) =>
          n.id === id && !n.read ? { ...n, read: true } : n,
        ),
      )
      setUnread((prev) => Math.max(0, prev - 1))
    } catch (err) {
      setError(err instanceof Error ? err.message : t("error.markFailed"))
    }
  }

  const handleArchive = async (id: string) => {
    try {
      await archiveNotifications([id])
      setItems((prev) => prev.filter((n) => n.id !== id))
    } catch (err) {
      setError(err instanceof Error ? err.message : t("error.archiveFailed"))
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="ink-display text-2xl text-foreground sm:text-3xl">
            {t("page.title")}
          </h1>
          <p className="text-sm text-muted-foreground">
            {t("page.summary", { unread, total: items.length })}
          </p>
        </div>
        <div className="flex gap-2 text-xs">
          <button
            type="button"
            onClick={() => setFilter("all")}
            className={
              "rounded border px-2 py-1 " +
              (filter === "all"
                ? "border-foreground bg-foreground text-background"
                : "border-border bg-card text-foreground hover:bg-muted")
            }
          >
            {t("filter.all")}
          </button>
          <button
            type="button"
            onClick={() => setFilter("unread")}
            className={
              "rounded border px-2 py-1 " +
              (filter === "unread"
                ? "border-foreground bg-foreground text-background"
                : "border-border bg-card text-foreground hover:bg-muted")
            }
          >
            {t("filter.unread")}
          </button>
          <button
            type="button"
            onClick={() => void handleMarkAllRead()}
            className="rounded border border-border bg-card px-2 py-1 text-foreground hover:bg-muted"
            disabled={unread === 0}
          >
            {t("filter.markAllRead")}
          </button>
        </div>
      </header>

      <div className="flex flex-wrap gap-2 text-xs">
        <button
          type="button"
          onClick={() => setTopicFilter(null)}
          className={
            "rounded-full border px-3 py-1 " +
            (topicFilter === null
              ? "border-foreground bg-foreground text-background"
              : "border-border bg-card text-foreground hover:bg-muted")
          }
        >
          {t("topic.all")}
        </button>
        {TOPIC_KEYS.map((key) => (
          <button
            type="button"
            key={key}
            onClick={() =>
              setTopicFilter((prev) => (prev === key ? null : key))
            }
            className={
              "rounded-full border px-3 py-1 " +
              (topicFilter === key
                ? "border-foreground bg-foreground text-background"
                : "border-border bg-card text-foreground hover:bg-muted")
            }
          >
            {t(`topic.${key}`)}
            {topicCounts[key] ? (
              <span className="ml-1 rounded bg-[color:var(--cinnabar,#C73E3A)]/20 px-1 text-[10px] text-[color:var(--cinnabar,#C73E3A)]">
                {topicCounts[key]}
              </span>
            ) : null}
          </button>
        ))}
      </div>

      {error ? (
        <p className="rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      ) : null}

      {loading ? (
        <p className="text-sm text-muted-foreground">{t("state.loading")}</p>
      ) : visible.length === 0 ? (
        <p className="rounded-xl border border-dashed border-border bg-card p-8 text-center text-sm text-muted-foreground">
          {t("state.empty")}
        </p>
      ) : (
        <ul className="space-y-3">
          {visible.map((n) => (
            <li
              key={n.id}
              className={
                "rounded-xl border p-4 transition-shadow hover:shadow-md " +
                (SEVERITY_COLORS[n.severity] ?? SEVERITY_COLORS.info) +
                (n.read ? "" : " ring-1 ring-[color:var(--cinnabar,#C73E3A)]/30")
              }
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-2 text-sm font-semibold text-foreground">
                    {!n.read ? (
                      <span
                        aria-hidden
                        className="h-2 w-2 rounded-full bg-[color:var(--cinnabar,#C73E3A)]"
                      />
                    ) : null}
                    {n.title}
                  </p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {n.body}
                  </p>
                  <p className="mt-2 text-xs text-muted-foreground/80">
                    {topicLabel(n.topic)} ·{" "}
                    {new Date(n.created_at).toLocaleString(formatLocale)}
                  </p>
                </div>
                <div className="flex shrink-0 flex-col gap-1 text-xs">
                  {n.action_url ? (
                    <a
                      href={n.action_url}
                      onClick={() => void handleMarkOne(n.id)}
                      className="rounded border border-border bg-card px-2 py-1 text-foreground hover:bg-muted"
                    >
                      {t("action.view")}
                    </a>
                  ) : null}
                  {!n.read ? (
                    <button
                      type="button"
                      onClick={() => void handleMarkOne(n.id)}
                      className="rounded border border-border bg-card px-2 py-1 text-foreground hover:bg-muted"
                    >
                      {t("action.markRead")}
                    </button>
                  ) : null}
                  <button
                    type="button"
                    onClick={() => void handleArchive(n.id)}
                    className="rounded border border-border bg-card px-2 py-1 text-muted-foreground hover:bg-muted"
                  >
                    {t("action.archive")}
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
