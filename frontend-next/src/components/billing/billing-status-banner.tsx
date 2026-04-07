"use client"

/**
 * Billing status banner (Task 6).
 *
 * Consumes the redirect query params that the T5 fake-pay browser handler
 * emits, and renders a small, calm result surface at the top of
 * `/settings/billing`. The component does NOT raise or disappear on unknown
 * status values — it only renders when the current `?status=` is one of the
 * values we know how to explain, and stays silent otherwise.
 *
 * Known status values (from T5 `fake_pay_browser`):
 * - `paid`              → success surface, optional subscription context
 * - `already_settled`   → neutral surface, nothing new happened
 * - `error` + reason    → error surface with translated reason
 *
 * After mounting we call `router.replace` once to strip the query param from
 * the URL, so a hard reload or a browser back-navigation later doesn't show
 * a stale "支付成功" banner. The cleared URL keeps the same pathname, so the
 * component's own message stays visible until unmount.
 *
 * Design: matches DESIGN.md §4.3 billing guardrails — restrained, neutral
 * surface, clear state labels, no drama. No marketing typography.
 */

import { useEffect, useState } from "react"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { CheckCircle2, AlertCircle, Info, X } from "lucide-react"
import { cn } from "@/lib/utils"

type BannerTone = "success" | "info" | "error"

type BannerContent = {
  tone: BannerTone
  title: string
  body: string
}

const ERROR_REASON_COPY: Record<string, string> = {
  order_not_found: "未找到对应的订单,可能已经过期或已取消。",
}

function readBannerFromStatus(
  status: string | null,
  reason: string | null,
): BannerContent | null {
  if (!status) return null
  if (status === "paid") {
    return {
      tone: "success",
      title: "支付成功",
      body: "订单已处理,你的订阅信息已更新。",
    }
  }
  if (status === "already_settled") {
    return {
      tone: "info",
      title: "订单已处理",
      body: "这个订单此前已经支付成功,当前没有新的扣款。",
    }
  }
  if (status === "error") {
    const detail =
      (reason && ERROR_REASON_COPY[reason]) ||
      "支付流程未能完成,请稍后重试或返回下方选择套餐。"
    return {
      tone: "error",
      title: "支付未完成",
      body: detail,
    }
  }
  return null
}

function toneStyles(tone: BannerTone) {
  if (tone === "success") {
    return {
      container:
        "border-primary/30 bg-primary/5 text-foreground",
      iconClass: "text-primary",
      Icon: CheckCircle2,
    }
  }
  if (tone === "error") {
    return {
      container:
        "border-destructive/40 bg-destructive/5 text-foreground",
      iconClass: "text-destructive",
      Icon: AlertCircle,
    }
  }
  return {
    container: "border-border bg-muted/30 text-foreground",
    iconClass: "text-muted-foreground",
    Icon: Info,
  }
}

export function BillingStatusBanner() {
  const searchParams = useSearchParams()
  const pathname = usePathname()
  const router = useRouter()

  // Snapshot the status once on mount so a subsequent router.replace that
  // strips the query param does NOT cause the banner to disappear between
  // renders. The banner stays visible until unmount / dismiss.
  const [initialStatus] = useState(() => searchParams.get("status"))
  const [initialReason] = useState(() => searchParams.get("reason"))
  const [dismissed, setDismissed] = useState(false)

  useEffect(() => {
    // Clean the URL exactly once so refresh / back-nav does not re-trigger
    // the banner. We only touch the URL when there's something to clean.
    if (!initialStatus) return
    const current = searchParams.get("status")
    if (!current) return
    router.replace(pathname, { scroll: false })
    // We intentionally depend on an empty dep list style: mount-only cleanup.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const content = readBannerFromStatus(initialStatus, initialReason)
  if (!content || dismissed) return null

  const { container, iconClass, Icon } = toneStyles(content.tone)

  return (
    <div
      role={content.tone === "error" ? "alert" : "status"}
      className={cn(
        "flex items-start gap-3 rounded-lg border px-4 py-3 text-sm",
        container,
      )}
    >
      <Icon
        className={cn("mt-0.5 h-4 w-4 shrink-0", iconClass)}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <p className="font-medium">{content.title}</p>
        <p className="mt-0.5 text-[0.875rem] leading-relaxed text-muted-foreground">
          {content.body}
        </p>
      </div>
      <button
        type="button"
        onClick={() => setDismissed(true)}
        className="shrink-0 rounded p-1 text-muted-foreground transition-colors hover:text-foreground"
        aria-label="关闭通知"
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
    </div>
  )
}
