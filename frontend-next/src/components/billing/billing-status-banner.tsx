"use client"

import { useEffect, useRef, useState } from "react"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react"
import { getOrder, type PaymentOrderStatus } from "@/lib/billing/get-order"
import { cn } from "@/lib/utils"

type BannerTone = "success" | "info" | "error"

type BannerContent = {
  tone: BannerTone
  title: string
  body: string
}

type BillingStatusBannerProps = {
  onOrderSettled?: () => void
}

const ERROR_REASON_COPY: Record<string, string> = {
  order_not_found: "未找到对应的订单，可能已经过期或被取消。",
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
      body: "订单已处理，你的订阅信息已更新。",
    }
  }
  if (status === "already_settled") {
    return {
      tone: "info",
      title: "订单已处理",
      body: "这个订单此前已经支付成功，当前没有新的扣款。",
    }
  }
  if (status === "error") {
    return {
      tone: "error",
      title: "支付未完成",
      body:
        (reason && ERROR_REASON_COPY[reason]) ||
        "支付流程未能完成，请稍后重试或重新创建订单。",
    }
  }
  return null
}

function readBannerFromOrderStatus(status: PaymentOrderStatus): BannerContent {
  if (status === "paid") {
    return {
      tone: "success",
      title: "支付成功",
      body: "系统已经确认到账，订阅与权益会自动刷新。",
    }
  }
  if (status === "failed") {
    return {
      tone: "error",
      title: "支付失败",
      body: "订单未能完成支付，请重新发起支付或稍后再试。",
    }
  }
  if (status === "cancelled" || status === "expired") {
    return {
      tone: "info",
      title: "订单未完成",
      body: "订单当前未支付完成，如需升级可重新创建订单。",
    }
  }
  if (status === "refunded") {
    return {
      tone: "info",
      title: "订单已退款",
      body: "退款状态已同步到账单记录中。",
    }
  }
  return {
    tone: "info",
    title: "正在确认支付结果",
    body: "支付完成后，系统会自动确认订单状态，请稍候。",
  }
}

function toneStyles(tone: BannerTone) {
  if (tone === "success") {
    return {
      container: "border-primary/30 bg-primary/5 text-foreground",
      iconClass: "text-primary",
      Icon: CheckCircle2,
    }
  }
  if (tone === "error") {
    return {
      container: "border-destructive/40 bg-destructive/5 text-foreground",
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

async function sleep(ms: number) {
  await new Promise((resolve) => setTimeout(resolve, ms))
}

export function BillingStatusBanner({
  onOrderSettled,
}: BillingStatusBannerProps) {
  const searchParams = useSearchParams()
  const pathname = usePathname()
  const router = useRouter()

  const [initialStatus] = useState(() => searchParams.get("status"))
  const [initialReason] = useState(() => searchParams.get("reason"))
  const [initialOrderId] = useState(() => searchParams.get("order_id"))
  const [initialProvider] = useState(() => searchParams.get("provider"))
  const [dismissed, setDismissed] = useState(false)
  const [content, setContent] = useState<BannerContent | null>(() => {
    if (initialOrderId && initialProvider === "alipay") {
      return readBannerFromOrderStatus("pending")
    }
    return readBannerFromStatus(initialStatus, initialReason)
  })
  const onOrderSettledRef = useRef(onOrderSettled)
  const notifiedRef = useRef(false)

  useEffect(() => {
    onOrderSettledRef.current = onOrderSettled
  }, [onOrderSettled])

  useEffect(() => {
    const shouldClean =
      Boolean(initialStatus) ||
      Boolean(initialReason) ||
      Boolean(initialOrderId) ||
      Boolean(initialProvider)
    if (!shouldClean) return
    router.replace(pathname, { scroll: false })
  }, [initialOrderId, initialProvider, initialReason, initialStatus, pathname, router])

  useEffect(() => {
    if (!initialOrderId || initialProvider !== "alipay") return
    let cancelled = false
    const orderId = initialOrderId

    async function pollOrderStatus() {
      for (let attempt = 0; attempt < 8; attempt += 1) {
        try {
          const order = await getOrder(orderId, { refresh: true })
          if (cancelled) return
          const nextContent = readBannerFromOrderStatus(order.status)
          setContent(nextContent)
          if (order.status !== "created" && order.status !== "pending") {
            if (!notifiedRef.current) {
              notifiedRef.current = true
              onOrderSettledRef.current?.()
            }
            return
          }
        } catch (error) {
          if (cancelled) return
          const message =
            error instanceof Error ? error.message : "支付结果确认失败，请稍后刷新重试。"
          setContent({
            tone: "error",
            title: "支付结果确认失败",
            body: message,
          })
          return
        }
        await sleep(2000)
      }

      if (!cancelled) {
        setContent({
          tone: "info",
          title: "仍在确认支付结果",
          body: "订单状态还在同步中，你可以稍后刷新此页面再次查看。",
        })
      }
    }

    void pollOrderStatus()
    return () => {
      cancelled = true
    }
  }, [initialOrderId, initialProvider])

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
