"use client"

import { useTranslations } from "next-intl"

/**
 * Persistent banner shown after a handoff has been requested or created.
 *
 * The banner takes over the conversation foot — once a handoff is in
 * flight we deliberately stop letting the AI auto-reply. Plan §8.3:
 * "转人工后展示当前人工渠道状态，不继续让 AI 抢答".
 */
export function SupportHandoffBanner({
  state,
}: {
  state: "none" | "recommended" | "requested" | "created" | "failed" | "closed"
}) {
  const t = useTranslations("appSupport")
  if (state === "none") return null
  if (state === "recommended") {
    return (
      <div className="border-t border-border bg-yellow-50/40 px-4 py-2 text-xs text-foreground">
        {t("handoffBanner.recommended")}
      </div>
    )
  }
  if (state === "requested") {
    return (
      <div className="border-t border-border bg-yellow-50/60 px-4 py-2 text-xs text-foreground">
        {t("labels.handoffWaitingNote")}
      </div>
    )
  }
  if (state === "created") {
    return (
      <div className="border-t border-border bg-green-50/60 px-4 py-2 text-xs text-foreground">
        {t("labels.handoffCreatedNote")}
        {t("handoffBanner.createdPrefix")}
        {t("labels.handoffWaitingNote")}
      </div>
    )
  }
  if (state === "failed") {
    return (
      <div className="border-t border-border bg-red-50/40 px-4 py-2 text-xs text-foreground">
        {t("labels.handoffFailedNote")}
      </div>
    )
  }
  return (
    <div className="border-t border-border bg-card px-4 py-2 text-xs text-muted-foreground">
      {t("handoffBanner.closed")}
    </div>
  )
}
