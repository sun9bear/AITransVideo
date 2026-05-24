"use client"

import { useCallback, useEffect, useState } from "react"

import {
  adminCloseHandoff,
  adminGetConversation,
  adminListHandoffs,
  adminReplyToConversation,
  type AdminConversationDetail,
} from "@/lib/api/support"
import { usePollingTask } from "@/lib/react/usePollingTask"

/**
 * Admin "人工工单" management panel.
 *
 * Lists pending/created handoff tickets (auto-refreshes every 30s),
 * lets admin expand a ticket inline → see full conversation + user
 * identity → type a reply → POST /api/admin/support/conversations/{id}/reply.
 *
 * After reply, the user gets a "客服已回复" notification in their
 * notification center, and SupportWidget polls and renders the reply
 * as a "客服" bubble in the same conversation.
 *
 * Plan 2026-05-08 follow-up §"L1 in-product chat".
 */

interface HandoffRow {
  id: string
  conversation_id: string
  provider: string
  reason: string
  summary: string | null
  status: string
  created_at: string
  updated_at: string
}

const REASON_LABEL: Record<string, string> = {
  user_requested_human: "用户主动请求",
  low_confidence: "AI 置信度不足",
  sensitive_category: "敏感关键词",
  repeated_unresolved: "多次未解决",
  policy_required: "政策类（账单 / 退款 / 隐私）",
  abuse_review: "投诉 / 升级",
}

const PROVIDER_LABEL: Record<string, string> = {
  in_product: "产品内回复",
  wechat_qr: "微信 QR（运营离线）",
  email: "邮件工单",
  chatwoot: "Chatwoot",
  wechat_kf: "微信客服",
}

export function HandoffTicketsPanel() {
  const [items, setItems] = useState<HandoffRow[]>([])
  const [activeStatus, setActiveStatus] = useState<"created" | "closed">("created")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const r = await adminListHandoffs(activeStatus)
      setItems(r.items)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载工单失败")
    } finally {
      setLoading(false)
    }
  }, [activeStatus])

  useEffect(() => {
    void refresh()
  }, [refresh])

  usePollingTask(refresh, {
    immediate: false,
    intervalMs: 30_000,
  })

  return (
    <section className="space-y-4 rounded-xl border border-border bg-card p-4">
      <header className="flex items-center justify-between">
        <h2 className="text-base font-semibold text-foreground">人工工单</h2>
        <div className="flex gap-2 text-xs">
          {(["created", "closed"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => {
                setActiveStatus(s)
                setExpandedId(null)
                setLoading(true)
              }}
              className={
                "rounded border px-2 py-1 " +
                (activeStatus === s
                  ? "border-foreground bg-foreground text-background"
                  : "border-border bg-background text-foreground hover:bg-muted")
              }
            >
              {s === "created" ? `待处理 (${items.length})` : "已处理"}
            </button>
          ))}
          <button
            type="button"
            onClick={() => void refresh()}
            className="rounded border border-border bg-background px-2 py-1 text-foreground hover:bg-muted"
          >
            刷新
          </button>
        </div>
      </header>

      {error ? (
        <p className="rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      ) : null}

      {loading ? (
        <p className="text-sm text-muted-foreground">加载中…</p>
      ) : items.length === 0 ? (
        <p className="rounded-xl border border-dashed border-border bg-background p-6 text-center text-sm text-muted-foreground">
          {activeStatus === "created" ? "暂无待处理工单" : "暂无已处理工单"}
        </p>
      ) : (
        <ul className="space-y-2">
          {items.map((row) => (
            <li
              key={row.id}
              className="rounded-lg border border-border bg-background"
            >
              <button
                type="button"
                className="flex w-full items-start justify-between gap-3 px-3 py-2 text-left hover:bg-muted/50"
                onClick={() =>
                  setExpandedId((cur) => (cur === row.id ? null : row.id))
                }
              >
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-foreground">
                    {REASON_LABEL[row.reason] ?? row.reason}
                  </p>
                  <p className="mt-1 truncate text-xs text-muted-foreground">
                    {row.summary || `convo: ${row.conversation_id.slice(0, 8)}`}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground/80">
                    {new Date(row.created_at).toLocaleString("zh-CN")} ·{" "}
                    {PROVIDER_LABEL[row.provider] ?? row.provider}
                  </p>
                </div>
                <span aria-hidden className="text-xs text-muted-foreground">
                  {expandedId === row.id ? "▴" : "▾"}
                </span>
              </button>

              {expandedId === row.id ? (
                <div className="border-t border-border bg-muted/20 p-3">
                  <HandoffDetail
                    handoffId={row.id}
                    conversationId={row.conversation_id}
                    onAction={() => void refresh()}
                  />
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function HandoffDetail({
  handoffId,
  conversationId,
  onAction,
}: {
  handoffId: string
  conversationId: string
  onAction: () => void
}) {
  const [detail, setDetail] = useState<AdminConversationDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState("")
  const [posting, setPosting] = useState(false)

  const reload = useCallback(async (silent = false) => {
    try {
      if (!silent) setLoading(true)
      setDetail(await adminGetConversation(conversationId))
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载会话失败")
    } finally {
      setLoading(false)
    }
  }, [conversationId])

  useEffect(() => {
    void reload()
  }, [reload])

  usePollingTask(() => reload(true), {
    immediate: false,
    intervalMs: 30_000,
  })

  const submitReply = async (markHandled: boolean) => {
    if (!draft.trim()) return
    setPosting(true)
    setError(null)
    try {
      await adminReplyToConversation(conversationId, draft.trim(), markHandled)
      setDraft("")
      await reload()
      onAction()
    } catch (err) {
      setError(err instanceof Error ? err.message : "发送失败")
    } finally {
      setPosting(false)
    }
  }

  const close = async () => {
    if (!confirm("确认关闭这个工单？关闭后用户的浮窗会话也会标记为 closed。")) return
    setPosting(true)
    try {
      await adminCloseHandoff(handoffId)
      onAction()
    } catch (err) {
      setError(err instanceof Error ? err.message : "关闭失败")
    } finally {
      setPosting(false)
    }
  }

  if (loading && !detail) {
    return <p className="text-xs text-muted-foreground">加载中…</p>
  }
  if (!detail) {
    return (
      <p className="text-xs text-destructive">{error ?? "加载失败"}</p>
    )
  }

  return (
    <div className="space-y-3 text-sm">
      {/* User identity */}
      <div className="rounded border border-border bg-background p-2 text-xs">
        <p>
          <span className="text-muted-foreground">用户：</span>
          {detail.user
            ? `${detail.user.display_name} · ${detail.user.plan_code ?? "—"} · ${detail.user.email || detail.user.phone_number || "—"}`
            : `匿名访客 (anon: ${detail.conversation.anonymous_id ?? "—"})`}
        </p>
        <p className="mt-1">
          <span className="text-muted-foreground">来源：</span>
          {detail.conversation.entrypoint || "—"} · {detail.conversation.page_url || "—"}
          {detail.conversation.job_id ? ` · job: ${detail.conversation.job_id}` : ""}
        </p>
      </div>

      {/* Conversation log */}
      <div className="max-h-72 space-y-2 overflow-y-auto rounded border border-border bg-background p-2 text-xs">
        {detail.messages.length === 0 ? (
          <p className="text-muted-foreground">还没有消息</p>
        ) : (
          detail.messages.map((m) => (
            <div
              key={m.id}
              className={
                "rounded p-2 " +
                (m.sender === "user"
                  ? "ml-8 bg-[color:var(--cinnabar,#C73E3A)]/10"
                  : m.sender === "human"
                    ? "mr-8 bg-emerald-50/40 border border-emerald-300/50"
                    : m.sender === "system"
                      ? "border border-dashed border-border italic text-muted-foreground"
                      : "mr-8 bg-card border border-border")
              }
            >
              <p className="text-[10px] font-semibold uppercase text-muted-foreground">
                {m.sender}
                {" · "}
                {new Date(m.created_at).toLocaleString("zh-CN")}
              </p>
              <p className="mt-1 whitespace-pre-wrap">{m.body}</p>
            </div>
          ))
        )}
      </div>

      {/* Reply box */}
      <div className="space-y-2">
        <textarea
          rows={3}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="在这里输入回复，发送后会进入用户浮窗 + 触发通知中心红点…"
          className="w-full resize-y rounded border border-border bg-background px-2 py-1 text-sm"
        />
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={posting || !draft.trim()}
            onClick={() => void submitReply(false)}
            className="rounded bg-[color:var(--cinnabar,#C73E3A)] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
          >
            发送回复
          </button>
          <button
            type="button"
            disabled={posting || !draft.trim()}
            onClick={() => void submitReply(true)}
            className="rounded border border-border bg-background px-3 py-1.5 text-xs hover:bg-muted disabled:opacity-50"
            title="发送回复并把工单标为已处理"
          >
            回复并标为已处理
          </button>
          <span className="flex-1" />
          <button
            type="button"
            disabled={posting}
            onClick={() => void close()}
            className="rounded border border-border bg-background px-3 py-1.5 text-xs text-muted-foreground hover:bg-muted disabled:opacity-50"
          >
            关闭工单
          </button>
        </div>
        {error ? (
          <p className="text-xs text-destructive">{error}</p>
        ) : null}
      </div>
    </div>
  )
}
