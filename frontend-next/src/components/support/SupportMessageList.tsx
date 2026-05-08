"use client"

import type { SupportMessageView } from "@/lib/api/support"

/**
 * Renders the back-and-forth between the user, the assistant, and any
 * "system" notes (handoff created, etc.).
 *
 * Chat-bubble styling is intentionally muted — plan §8.3 prefers short,
 * specific replies, and a busy bubble theme would compete with the
 * marketing surface chrome.
 */
export function SupportMessageList({
  messages,
  loading,
}: {
  messages: SupportMessageView[]
  loading?: boolean
}) {
  return (
    <ol
      role="log"
      aria-live="polite"
      className="flex flex-1 flex-col gap-3 overflow-y-auto px-4 py-4 text-sm"
    >
      {messages.length === 0 && !loading ? (
        <li className="text-xs text-muted-foreground/70">
          {"还没有对话记录。问点什么吧。"}
        </li>
      ) : null}

      {messages.map((msg) => (
        <li
          key={msg.id}
          className={
            "flex " +
            (msg.sender === "user" ? "justify-end" : "justify-start")
          }
        >
          <div
            className={
              "max-w-[85%] whitespace-pre-wrap rounded-lg px-3 py-2 leading-relaxed " +
              (msg.sender === "user"
                ? "bg-[color:var(--cinnabar,#C73E3A)]/10 text-foreground"
                : msg.sender === "system"
                  ? "border border-dashed border-border bg-transparent text-xs text-muted-foreground"
                  : "border border-border bg-card text-foreground")
            }
          >
            {msg.body}
          </div>
        </li>
      ))}

      {loading ? (
        <li className="self-start text-xs italic text-muted-foreground/70">
          {"AI 正在思考…"}
        </li>
      ) : null}
    </ol>
  )
}
