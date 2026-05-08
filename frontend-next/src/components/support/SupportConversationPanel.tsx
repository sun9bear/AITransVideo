"use client"

import { useEffect, useRef, useState } from "react"

import {
  createSupportConversation,
  type ConversationDetail,
  type CreateConversationRequest,
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
}) {
  const [state, setState] = useState<ConversationState>(INITIAL_STATE)
  const [draft, setDraft] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [latestRoute, setLatestRoute] = useState<SendMessageResponse["route"] | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)

  // Reset on close — keep transcript on subsequent re-opens but discard
  // pending text. Plan §8.3 — open / close should not feel destructive,
  // but a stale draft from yesterday is more confusing than helpful.
  useEffect(() => {
    if (!visible) {
      setDraft("")
      setError(null)
    } else {
      // Tiny defer so the textarea has time to mount.
      window.setTimeout(() => {
        inputRef.current?.focus()
      }, 30)
    }
  }, [visible])

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

  const append = (msg: SupportMessageView) =>
    setState((prev) => ({ ...prev, messages: [...prev.messages, msg] }))

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
      append({
        id: `srv-handoff-${Date.now()}`,
        sender: "system",
        body: result.message,
        created_at: new Date().toISOString(),
      })
      setState((prev) => ({
        ...prev,
        handoff_state: result.handoff_state,
        status: "waiting_human",
      }))
    } catch (err) {
      setError(err instanceof Error ? err.message : "转人工失败")
    }
  }

  if (!visible) return null

  const showQuickQuestions =
    state.messages.length === 0 && (quickQuestions ?? FALLBACK_QUICK_QUESTIONS).length > 0

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
          <p className="text-xs text-muted-foreground">
            {SUPPORT_LABELS.panelSubtitle}
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

      {state.messages.length === 0 ? (
        <div className="px-4 py-3 text-xs leading-relaxed text-muted-foreground">
          {greeting || FALLBACK_GREETING}
        </div>
      ) : null}

      <SupportMessageList messages={state.messages} loading={loading} />

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

      <SupportHandoffBanner state={state.handoff_state} />

      {error ? (
        <p className="px-4 py-2 text-xs text-destructive">{error}</p>
      ) : null}

      {latestRoute && !handoffActive && state.messages.length > 0 ? (
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
          if (handoffActive) return
          void sendText(draft)
        }}
      >
        <textarea
          ref={inputRef}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={
            handoffActive
              ? SUPPORT_LABELS.handoffWaitingNote
              : SUPPORT_LABELS.inputPlaceholder
          }
          disabled={handoffActive}
          rows={2}
          className="min-h-[2.5rem] flex-1 resize-none rounded border border-border bg-card px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-[color:var(--cinnabar,#C73E3A)]/40 disabled:opacity-60"
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !handoffActive
            ) {
              event.preventDefault()
              void sendText(draft)
            }
          }}
        />
        <button
          type="submit"
          disabled={loading || handoffActive || draft.trim().length === 0}
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
