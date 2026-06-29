"use client"

import { useEffect, useRef, useState } from "react"
import { useTranslations } from "next-intl"
import { useSearchParams } from "next/navigation"
import { usePathname, useRouter } from "@/i18n/navigation"
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react"
import { getOrder, type PaymentOrderStatus } from "@/lib/billing/get-order"
import { cn } from "@/lib/utils"

/** Translator scoped to the `appBilling` namespace (relative keys). */
type BillingTranslator = ReturnType<typeof useTranslations<"appBilling">>
/** Literal message-key type for appBilling — lets label-key maps stay typed. */
type BillingKey = Parameters<BillingTranslator>[0]

type BannerTone = "success" | "info" | "error"

type BannerContent = {
  tone: BannerTone
  title: string
  body: string
}

type BillingStatusBannerProps = {
  onOrderSettled?: () => void
}

const ERROR_REASON_KEYS: Record<string, BillingKey> = {
  order_not_found: "banner.orderNotFound",
}

// Providers whose final status is confirmed by polling getOrder(refresh) after
// the user returns from an off-site / overlay checkout. Alipay (async notify can
// lag) and Paddle (webhook + ~10min WeChat capture) both rely on it. The refresh
// path is provider-generic on the gateway side (plan §7.5).
const POLLABLE_RETURN_PROVIDERS = new Set(["alipay", "paddle", "wechatpay"])

function isPollableReturnProvider(provider: string | null): boolean {
  return provider !== null && POLLABLE_RETURN_PROVIDERS.has(provider)
}

// A just-created pending order, stashed by the checkout card before it hands
// off to the (possibly async) provider checkout. This lets the billing page
// keep confirming even if the user closes the WeChat QR page without coming
// back through the success redirect. Cleared once the order reaches a terminal
// state. TTL matches the server-side ORDER_EXPIRY_MINUTES (30 min).
const PENDING_ORDER_KEY = "avt_pending_order"
const PENDING_ORDER_TTL_MS = 30 * 60 * 1000

// WeChat via Paddle (MoR) captures asynchronously — often several minutes.
// Poll long enough, at a relaxed cadence, to catch it via the gateway's
// query-refresh backstop (plan §7.5 / R2) without hammering.
const POLL_INTERVAL_MS = 15000
const MAX_POLL_ATTEMPTS = 52 // ~13 min

type StashedOrder = { order_id: string; provider: string }

function readStashedPendingOrder(): StashedOrder | null {
  if (typeof window === "undefined") return null
  try {
    const raw = window.localStorage.getItem(PENDING_ORDER_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as {
      order_id?: string
      provider?: string
      ts?: number
    }
    if (!parsed.order_id || !isPollableReturnProvider(parsed.provider ?? null)) {
      return null
    }
    if (typeof parsed.ts === "number" && Date.now() - parsed.ts > PENDING_ORDER_TTL_MS) {
      window.localStorage.removeItem(PENDING_ORDER_KEY)
      return null
    }
    return { order_id: parsed.order_id, provider: parsed.provider as string }
  } catch {
    return null
  }
}

function clearStashedPendingOrder(): void {
  if (typeof window === "undefined") return
  try {
    window.localStorage.removeItem(PENDING_ORDER_KEY)
  } catch {
    // localStorage unavailable — non-fatal
  }
}

function readBannerFromStatus(
  t: BillingTranslator,
  status: string | null,
  reason: string | null,
): BannerContent | null {
  if (!status) return null
  if (status === "paid") {
    return {
      tone: "success",
      title: t("banner.paidTitle"),
      body: t("banner.paidBody"),
    }
  }
  if (status === "already_settled") {
    return {
      tone: "info",
      title: t("banner.alreadySettledTitle"),
      body: t("banner.alreadySettledBody"),
    }
  }
  if (status === "error") {
    const reasonKey = reason ? ERROR_REASON_KEYS[reason] : null
    return {
      tone: "error",
      title: t("banner.errorTitle"),
      body: reasonKey ? t(reasonKey) : t("banner.errorBody"),
    }
  }
  // successUrl / checkout.closed returns may arrive without an order_id AND
  // without a localStorage stash (private mode) — show static copy instead of
  // nothing so a paid user isn't left staring at an unchanged page.
  if (status === "processing") return pendingBanner(t, false)
  if (status === "closed") return pendingBanner(t, true)
  return null
}

function pendingBanner(t: BillingTranslator, closedReturn: boolean): BannerContent {
  if (closedReturn) {
    // The buyer deliberately closed the checkout window. Don't pretend a
    // payment is confirming — but keep polling: a WeChat buyer often closes
    // the QR page while the async capture is still in flight.
    return {
      tone: "info",
      title: t("banner.closedTitle"),
      body: t("banner.closedBody"),
    }
  }
  return {
    tone: "info",
    title: t("banner.pendingTitle"),
    body: t("banner.pendingBody"),
  }
}

function readBannerFromOrderStatus(
  t: BillingTranslator,
  status: PaymentOrderStatus,
  closedReturn = false,
): BannerContent {
  if (status === "paid") {
    return {
      tone: "success",
      title: t("banner.paidConfirmedTitle"),
      body: t("banner.paidConfirmedBody"),
    }
  }
  if (status === "failed") {
    return {
      tone: "error",
      title: t("banner.failedTitle"),
      body: t("banner.failedBody"),
    }
  }
  if (status === "cancelled" || status === "expired") {
    return {
      tone: "info",
      title: t("banner.incompleteTitle"),
      body: t("banner.incompleteBody"),
    }
  }
  if (status === "refunded") {
    return {
      tone: "info",
      title: t("banner.refundedTitle"),
      body: t("banner.refundedBody"),
    }
  }
  return pendingBanner(t, closedReturn)
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
  const t = useTranslations("appBilling")
  const searchParams = useSearchParams()
  const pathname = usePathname()
  const router = useRouter()

  const [initialStatus] = useState(() => searchParams.get("status"))
  const [initialReason] = useState(() => searchParams.get("reason"))
  const [initialOrderId] = useState(() => searchParams.get("order_id"))
  const [initialProvider] = useState(() => searchParams.get("provider"))
  // Fall back to a stashed pending order (set by the checkout card) when the
  // user returns to billing without success-redirect params — e.g. they closed
  // the async WeChat QR page. URL params still take precedence.
  const [stashedOrder] = useState(() => readStashedPendingOrder())
  const effectiveOrderId = initialOrderId ?? stashedOrder?.order_id ?? null
  const effectiveProvider = initialProvider ?? stashedOrder?.provider ?? null
  // The /paddle-checkout page returns status=closed when the buyer dismissed
  // the overlay without completing — same polling, distinct copy.
  const closedReturn = initialStatus === "closed"
  const [dismissed, setDismissed] = useState(false)
  const [content, setContent] = useState<BannerContent | null>(() => {
    if (effectiveOrderId && isPollableReturnProvider(effectiveProvider)) {
      return pendingBanner(t, closedReturn)
    }
    return readBannerFromStatus(t, initialStatus, initialReason)
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
    if (!effectiveOrderId || !isPollableReturnProvider(effectiveProvider)) return
    let cancelled = false
    const orderId = effectiveOrderId

    async function pollOrderStatus() {
      for (let attempt = 0; attempt < MAX_POLL_ATTEMPTS; attempt += 1) {
        try {
          const order = await getOrder(orderId, { refresh: true })
          if (cancelled) return
          setContent(readBannerFromOrderStatus(t, order.status, closedReturn))
          if (order.status !== "created" && order.status !== "pending") {
            clearStashedPendingOrder()
            if (!notifiedRef.current) {
              notifiedRef.current = true
              onOrderSettledRef.current?.()
            }
            return
          }
        } catch {
          // Transient (network blip / brief gateway hiccup). Keep polling —
          // WeChat capture can take minutes, so one failed poll must not abort.
          if (cancelled) return
        }
        await sleep(POLL_INTERVAL_MS)
      }

      if (!cancelled) {
        setContent({
          tone: "info",
          title: t("banner.stillConfirmingTitle"),
          body: t("banner.stillConfirmingBody"),
        })
      }
    }

    void pollOrderStatus()
    return () => {
      cancelled = true
    }
  }, [closedReturn, effectiveOrderId, effectiveProvider])

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
        aria-label={t("banner.dismiss")}
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
    </div>
  )
}
