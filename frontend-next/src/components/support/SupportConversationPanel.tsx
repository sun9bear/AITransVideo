"use client"

import { useEffect, useRef, useState } from "react"

import {
  createSupportConversation,
  getOnlineStatus,
  getSupportConversation,
  listMyOpenConversations,
  type ConversationDetail,
  type CreateConversationRequest,
  type OnlineStatus,
  type SendMessageResponse,
  type SupportMessageView,
  requestSupportHandoff,
  sendSupportMessage,
} from "@/lib/api/support"

import { SupportHandoffBanner } from "./SupportHandoffBanner"
import { SupportMessageList } from "./SupportMessageList"
import {
  FALLBACK_GREETING,
  FALLBACK_QUICK_QUESTIONS,
  SUPPORT_LABELS,
} from "./support-copy"

type HandoffState = ConversationDetail["handoff_state"]

interface ConversationState {
  id: string | null
  status: ConversationDetail["status"]
  handoff_state: HandoffState
  messages: SupportMessageView[]
}

const INITIAL_STATE: ConversationState = {
  id: null,
  status: "open",
  handoff_state: "none",
  messages: [],
}

interface OfflineState {
  qr_url: string
  message: string
}

export function SupportConversationPanel({
  visible,
  onRequestClose,
  greeting,
  quickQuestions,
  entrypoint,
  pageUrl,
  jobId,
  notificationId,
  budgetState,
  onlineStatus,
  isLoggedIn,
}: {
  visible: boolean
  onRequestClose: () => void
  greeting: string | null
  quickQuestions: string[] | null
  entrypoint: CreateConversationRequest["entrypoint"]
  pageUrl: string
  jobId: string | null
  notificationId: string | null
  budgetState: "normal" | "budget_exhausted" | null
  onlineStatus: OnlineStatus | null
  isLoggedIn: boolean
}) {
  const [state, setState] = useState<ConversationState>(INITIAL_STATE)
  const [draft, setDraft] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [latestRoute, setLatestRoute] = useState<SendMessageResponse["route"] | null>(null)
  const [offline, setOffline] = useState<OfflineState | null>(null)
  const [restoreNotice, setRestoreNotice] = useState<string | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const handoffPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const handoffTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastMessageIdRef = useRef<string | null>(null)

  // Reset draft on close (keep transcript on subsequent re-opens).
  useEffect(() => {
    if (!visible) {
      setDraft("")
      setError(null)
    } else {
      window.setTimeout(() => inputRef.current?.focus(), 30)
    }
  }, [visible])

  // Conversation restore on mount: if user is logged in and has an
  // open conversation in waiting_human state, restore it so they can
  // see the agent's reply seamlessly. Plan §"用户离线/回流" UX.
  useEffect(() => {
    if (!visible) return
    if (state.id) return // already have a conversation
    if (!isLoggedIn) return
    let cancelled = false
    listMyOpenConversations()
      .then(async ({ conversations }) => {
        if (cancelled || conversations.length === 0) return
        const target = conversations[0]
        const detail = await getSupportConversation(target.conversation_id)
        if (cancelled) return
        setState({
          id: detail.id,
          status: detail.status,
          handoff_state: detail.handoff_state,
          messages: detail.messages,
        })
        if (detail.messages.length > 0) {
          lastMessageIdRef.current =
            detail.messages[detail.messages.length - 1].id
        }
        if (
          detail.handoff_state === "created" ||
          detail.handoff_state === "requested"
        ) {
          setRestoreNotice("已恢复你之前的客服对话")
        }
      })
      .catch(() => {
        // 401 is fine (anonymous), other errors silent.
      })
    return () => {
      cancelled = true
    }
  }, [visible, state.id, isLoggedIn])

  const handoffActive =
    state.handoff_state === "requested" || state.handoff_state === "created"

  const ensureConversation = async (): Promise<string | null> => {
    if (state.id) return state.id
    try {
      const created = await createSupportConversation({
        channel: "web",
        entrypoint,
        page_url: pageUrl,
        job_id: jobId,
        notification_id: notificationId,
      })
      setState((prev) => ({
        ...prev,
        id: created.conversation_id,
        status: created.status,
        handoff_state: created.handoff_state,
      }))
      return created.conversation_id
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建会话失败")
      return null
    }
  }

  const append = (msg: SupportMessageView) => {
    setState((prev) => ({ ...prev, messages: [...prev.messages, msg] }))
    lastMessageIdRef.current = msg.id
  }

  const sendText = async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed) return
    setError(null)
    setLoading(true)
    const cid = await ensureConversation()
    if (!cid) {
      setLoading(false)
      return
    }
    append({
      id: `local-${Date.now()}`,
      sender: "user",
      body: trimmed,
      created_at: new Date().toISOString(),
    })
    try {
      const reply = await sendSupportMessage(cid, {
        message: trimmed,
        client_context: {
          page_url: pageUrl,
          job_id: jobId,
          notification_id: notificationId,
        },
      })
      append({
        id: `srv-${Date.now()}`,
        sender: reply.route === "handoff" ? "system" : "assistant",
        body: reply.reply,
        created_at: new Date().toISOString(),
      })
      setLatestRoute(reply.route)
      if (reply.route === "handoff") {
        setState((prev) => ({
          ...prev,
          handoff_state: "created",
          status: "waiting_human",
        }))
        if (
          reply.handoff_provider === "wechat_qr" &&
          reply.wechat_qr_url
        ) {
          setOffline({
            qr_url: reply.wechat_qr_url,
            message: reply.offline_message ?? "",
          })
        }
      } else if (reply.handoff?.recommended) {
        setState((prev) => ({ ...prev, handoff_state: "recommended" }))
      }
      setDraft("")
    } catch (err) {
      setError(err instanceof Error ? err.message : "发送失败")
    } finally {
      setLoading(false)
    }
  }

  const requestHandoff = async () => {
    setError(null)
    if (!state.id) {
      const cid = await ensureConversation()
      if (!cid) return
    }
    try {
      const result = await requestSupportHandoff(state.id ?? "", {
        reason: "user_requested_human",
      })
      if (result.provider === "wechat_qr" && result.wechat_qr_url) {
        setOffline({
          qr_url: result.wechat_qr_url,
          message: result.offline_message ?? "",
        })
      } else {
        // in_product / email — the assistant note appended server-side
        // will surface on the next polling tick. Add a local optimistic
        // copy so the user sees something immediately.
        append({
          id: `srv-handoff-${Date.now()}`,
          sender: "system",
          body: result.message,
          created_at: new Date().toISOString(),
        })
      }
      setState((prev) => ({
        ...prev,
        handoff_state: result.handoff_state,
        status: "waiting_human",
      }))
    } catch (err) {
      setError(err instanceof Error ? err.message : "转人工失败")
    }
  }

  // Polling for new human messages once handoff is created (in_product
  // path). Stops when conversation closes or widget is hidden.
  useEffect(() => {
    if (!visible) return
    if (!state.id) return
    if (state.handoff_state !== "created") {
      // Clear any prior timer / poll if state moved to closed/handled.
      if (handoffPollRef.current) {
        clearInterval(handoffPollRef.current)
        handoffPollRef.current = null
      }
      if (handoffTimeoutRef.current) {
        clearTimeout(handoffTimeoutRef.current)
        handoffTimeoutRef.current = null
      }
      return
    }
    if (state.status === "closed") return

    let inFlight = false
    const tick = async () => {
      if (typeof document !== "undefined" && document.hidden) return
      if (inFlight) return
      inFlight = true
      try {
        const detail = await getSupportConversation(state.id!)
        // Append any new messages we don't have yet.
        const known = new Set(state.messages.map((m) => m.id))
        const fresh = detail.messages.filter((m) => !known.has(m.id))
        if (fresh.length > 0) {
          setState((prev) => ({
            ...prev,
            status: detail.status,
            handoff_state: detail.handoff_state,
            messages: [...prev.messages, ...fresh],
          }))
          // If a human reply came in, clear the offline fallback timer.
          if (fresh.some((m) => m.sender === "human")) {
            if (handoffTimeoutRef.current) {
              clearTimeout(handoffTimeoutRef.current)
              handoffTimeoutRef.current = null
            }
          }
        } else if (
          detail.status !== state.status ||
          detail.handoff_state !== state.handoff_state
        ) {
          setState((prev) => ({
            ...prev,
            status: detail.status,
            handoff_state: detail.handoff_state,
          }))
        }
      } catch {
        // silent
      } finally {
        inFlight = false
      }
    }
    void tick()
    handoffPollRef.current = setInterval(tick, 5_000)
    const onVisibilityChange = () => {
      if (typeof document !== "undefined" && !document.hidden) void tick()
    }
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibilityChange)
    }

    // Schedule the offline-fallback timer. If no human message arrives
    // within fallback_minutes, fetch online-status to get the QR URL
    // and surface it.
    const fallbackMin = onlineStatus?.handoff_offline_fallback_minutes ?? 0
    if (fallbackMin > 0 && !offline) {
      handoffTimeoutRef.current = setTimeout(async () => {
        // Re-check whether a human has replied since polling kicked in.
        if (
          state.messages.some((m) => m.sender === "human") &&
          handoffTimeoutRef.current === null
        ) {
          return
        }
        try {
          const online = await getOnlineStatus()
          if (!online.online && online.has_wechat_qr) {
            setOffline({
              qr_url: "/api/support/wechat-qr",
              message: online.offline_message,
            })
          }
        } catch {
          // silent
        }
      }, fallbackMin * 60_000)
    }

    return () => {
      if (handoffPollRef.current) {
        clearInterval(handoffPollRef.current)
        handoffPollRef.current = null
      }
      if (handoffTimeoutRef.current) {
        clearTimeout(handoffTimeoutRef.current)
        handoffTimeoutRef.current = null
      }
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibilityChange)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, state.id, state.handoff_state])

  if (!visible) return null

  const showQuickQuestions =
    !offline &&
    state.messages.length === 0 &&
    (quickQuestions ?? FALLBACK_QUICK_QUESTIONS).length > 0

  // Online indicator: green if anyone online, gray otherwise.
  const onlineDotClass = onlineStatus?.online
    ? "bg-emerald-500"
    : "bg-muted-foreground"
  const onlineLabel = onlineStatus?.online ? "客服在线" : "运营离线"

  return (
    <section
      role="dialog"
      aria-label="客服对话"
      className={
        "fixed z-50 bottom-20 right-4 w-[min(380px,calc(100vw-2rem))] " +
        "sm:bottom-24 sm:right-6 sm:w-[380px] " +
        "max-h-[min(640px,calc(100vh-6rem))] flex flex-col " +
        "rounded-xl border border-border bg-background shadow-xl"
      }
    >
      <header className="flex items-start justify-between border-b border-border px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold text-foreground">
            {SUPPORT_LABELS.panelTitle}
          </h2>
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span aria-hidden className={"h-1.5 w-1.5 rounded-full " + onlineDotClass} />
            <span>{onlineLabel}</span>
            <span aria-hidden>·</span>
            <span>{SUPPORT_LABELS.panelSubtitle}</span>
          </p>
        </div>
        <button
          type="button"
          onClick={onRequestClose}
          aria-label={SUPPORT_LABELS.closeButton}
          className="text-xl leading-none text-muted-foreground hover:text-foreground"
        >
          ×
        </button>
      </header>

      {budgetState === "budget_exhausted" ? (
        <div className="border-b border-border bg-yellow-50/50 px-4 py-2 text-xs text-foreground">
          {SUPPORT_LABELS.budgetExhaustedNote}
        </div>
      ) : null}

      {restoreNotice ? (
        <div className="border-b border-border bg-blue-50/50 px-4 py-2 text-xs text-foreground">
          {restoreNotice}
        </div>
      ) : null}

      {state.messages.length === 0 && !offline ? (
        <div className="px-4 py-3 text-xs leading-relaxed text-muted-foreground">
          {greeting || FALLBACK_GREETING}
        </div>
      ) : null}

      <SupportMessageList messages={state.messages} loading={loading} />

      {/* Offline branch: WeChat QR card */}
      {offline ? (
        <div className="border-t border-border bg-card/40 px-4 py-3">
          <p className="mb-2 text-xs leading-relaxed text-foreground">
            {offline.message ||
              "运营暂未在线，可扫码添加客服微信，我们尽快回复。"}
          </p>
          <div className="flex justify-center">
            <img
              src={offline.qr_url}
              alt="客服微信二维码"
              className="h-44 w-44 rounded border border-border bg-background object-contain p-1"
            />
          </div>
        </div>
      ) : null}

      {showQuickQuestions ? (
        <div className="flex flex-wrap gap-2 border-t border-border px-4 py-2">
          {(quickQuestions ?? FALLBACK_QUICK_QUESTIONS).map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => {
                setDraft(q)
                void sendText(q)
              }}
              className="rounded-full border border-border bg-card px-3 py-1 text-xs text-foreground hover:bg-muted"
            >
              {q}
            </button>
          ))}
        </div>
      ) : null}

      {!offline ? <SupportHandoffBanner state={state.handoff_state} /> : null}

      {error ? (
        <p className="px-4 py-2 text-xs text-destructive">{error}</p>
      ) : null}

      {latestRoute && !handoffActive && !offline && state.messages.length > 0 ? (
        <div className="flex gap-2 border-t border-border px-4 py-2 text-xs">
          <button
            type="button"
            onClick={onRequestClose}
            className="flex-1 rounded border border-border bg-card px-3 py-1.5 text-foreground hover:bg-muted"
          >
            {SUPPORT_LABELS.resolvedButton}
          </button>
          <button
            type="button"
            onClick={() => void requestHandoff()}
            className="flex-1 rounded border border-[color:var(--cinnabar,#C73E3A)] bg-[color:var(--cinnabar,#C73E3A)]/10 px-3 py-1.5 text-foreground hover:bg-[color:var(--cinnabar,#C73E3A)]/20"
          >
            {SUPPORT_LABELS.notResolvedButton}
          </button>
        </div>
      ) : null}

      <form
        className="flex gap-2 border-t border-border px-3 py-3"
        onSubmit={(event) => {
          event.preventDefault()
          if (handoffActive || offline) return
          void sendText(draft)
        }}
      >
        <textarea
          ref={inputRef}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={
            offline
              ? "请在微信继续沟通"
              : handoffActive
                ? SUPPORT_LABELS.handoffWaitingNote
                : SUPPORT_LABELS.inputPlaceholder
          }
          disabled={handoffActive || !!offline}
          rows={2}
          className="min-h-[2.5rem] flex-1 resize-none rounded border border-border bg-card px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-[color:var(--cinnabar,#C73E3A)]/40 disabled:opacity-60"
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !handoffActive &&
              !offline
            ) {
              event.preventDefault()
              void sendText(draft)
            }
          }}
        />
        <button
          type="submit"
          disabled={
            loading ||
            handoffActive ||
            !!offline ||
            draft.trim().length === 0
          }
          className="self-end rounded bg-[color:var(--cinnabar,#C73E3A)] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
        >
          {SUPPORT_LABELS.sendButton}
        </button>
      </form>
    </section>
  )
}

SupportConversationPanel.displayName = "SupportConversationPanel"
export type { HandoffState }
