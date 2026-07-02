"use client"

/**
 * Topup credit-pack purchase card (CM-01).
 *
 * Renders on /settings/billing between the credits summary and the plan
 * checkout card. Default-inert: the whole card hides when the gateway
 * reports `enabled: false`, the package list is empty, or the fetch fails —
 * production stays unchanged until the owner flips TopupConfig.enabled.
 *
 * Provider choice is gateway-owned end-to-end: each package carries the
 * rails that can actually charge it (PayPal is pre-filtered server-side
 * when the SKU has no USD price). Display names come from checkout-config.
 * Payment hand-off mirrors CheckoutCard: WeChat Native renders the in-page
 * QR dialog; everything else navigates to checkout_url.
 */

import { useEffect, useMemo, useRef, useState } from "react"
import { useTranslations } from "next-intl"
import { toast } from "sonner"
import { Loader2, ShoppingBag } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { cn } from "@/lib/utils"
import type { CheckoutConfigResponse } from "@/lib/billing/get-checkout-config"
import { getOrder } from "@/lib/billing/get-order"
import {
  createTopupOrder,
  getTopupPackages,
  type TopupPackage,
} from "@/lib/billing/topup"
import { WechatQrDialog } from "@/components/billing/wechat-qr-dialog"

// Backstop poll after the QR dialog is closed while the order is still
// pending: 5s cadence, ~5 min cap (orders expire server-side at 30 min; the
// localStorage stash remains the next-page-load backstop after we stop).
const BACKSTOP_POLL_MS = 5000
const BACKSTOP_POLL_MAX_TICKS = 60

const ACTIVE_CHOICE_CLASS =
  "border-primary bg-background text-primary shadow-[inset_0_0_0_1px_var(--primary)]"
const INACTIVE_CHOICE_CLASS =
  "border-border bg-background text-foreground hover:border-primary/40"

function formatYuan(fen: number): string {
  const yuan = fen / 100
  return yuan % 1 === 0 ? `¥${yuan.toFixed(0)}` : `¥${yuan.toFixed(2)}`
}

type QrCheckoutState = {
  orderId: string
  qrCodeUrl: string
  amountFen: number
}

type TopupPurchaseCardProps = {
  checkoutConfig: CheckoutConfigResponse | null
  onOrderSettled?: () => void
}

export function TopupPurchaseCard({
  checkoutConfig,
  onOrderSettled,
}: TopupPurchaseCardProps) {
  const tb = useTranslations("appBilling")
  const [packages, setPackages] = useState<TopupPackage[] | null>(null)
  const [selectedCode, setSelectedCode] = useState<string>("")
  const [pickedProvider, setPickedProvider] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [redirecting, setRedirecting] = useState(false)
  const [qrCheckout, setQrCheckout] = useState<QrCheckoutState | null>(null)
  // Order still pending when the QR dialog was closed — the dialog's own
  // poller unmounts with it and BillingStatusBanner only reads the stash on
  // its initial mount, so without this backstop nothing on the page would
  // notice the webhook settling (CodeX review 2026-07-02 P2).
  const [backstopOrderId, setBackstopOrderId] = useState<string | null>(null)
  const backstopHandled = useRef(false)

  useEffect(() => {
    if (!backstopOrderId) return
    backstopHandled.current = false
    let ticks = 0
    const timer = window.setInterval(async () => {
      ticks += 1
      if (ticks > BACKSTOP_POLL_MAX_TICKS) {
        window.clearInterval(timer)
        return
      }
      try {
        const order = await getOrder(backstopOrderId, { refresh: true })
        if (backstopHandled.current) return
        if (order.status === "paid") {
          backstopHandled.current = true
          window.clearInterval(timer)
          setBackstopOrderId(null)
          try {
            window.localStorage.removeItem("avt_pending_order")
          } catch {
            // non-fatal
          }
          toast.success(tb("topup.paidToast"))
          onOrderSettled?.()
        } else if (!["created", "pending"].includes(order.status)) {
          // Terminal non-paid (expired / cancelled / failed) — stop quietly.
          backstopHandled.current = true
          window.clearInterval(timer)
          setBackstopOrderId(null)
        }
      } catch {
        // Transient poll error — keep going; the stash is the reload backstop.
      }
    }, BACKSTOP_POLL_MS)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backstopOrderId])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await getTopupPackages()
        if (cancelled) return
        if (res.enabled && res.packages.length > 0) {
          setPackages(res.packages)
          setSelectedCode(res.packages[0].code)
        }
      } catch {
        // Hide silently — topup is an optional lane; the rest of the billing
        // page must not degrade because this endpoint is unavailable.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    // bfcache restore after provider redirect — same reset as CheckoutCard.
    const handlePageShow = (event: PageTransitionEvent) => {
      if (event.persisted) {
        setSubmitting(false)
        setRedirecting(false)
      }
    }
    window.addEventListener("pageshow", handlePageShow)
    return () => window.removeEventListener("pageshow", handlePageShow)
  }, [])

  const selectedPackage = useMemo(
    () => packages?.find((p) => p.code === selectedCode) ?? null,
    [packages, selectedCode],
  )

  // Rails usable for the selected SKU = the SKU's chargeable rails (gateway
  // capability list) ∩ checkout-config's geo-visible operational providers —
  // checkout-config hides PayPal in mainland China / WeChat overseas, and the
  // topup card must never offer a rail the plan checkout deliberately
  // suppresses (CodeX review 2026-07-02 P2).
  const visibleOperationalCodes = useMemo(
    () =>
      new Set(
        (checkoutConfig?.providers ?? [])
          .filter((p) => p.operational)
          .map((p) => p.code),
      ),
    [checkoutConfig],
  )
  const availableProviders = (selectedPackage?.providers ?? []).filter((code) =>
    checkoutConfig ? visibleOperationalCodes.has(code) : true,
  )
  const gatewayPick =
    checkoutConfig?.recommended_provider ?? checkoutConfig?.default_provider ?? ""
  const selectedProvider =
    pickedProvider !== null && availableProviders.includes(pickedProvider)
      ? pickedProvider
      : availableProviders.includes(gatewayPick)
        ? gatewayPick
        : (availableProviders[0] ?? "")
  const providerDisplayName = (code: string): string =>
    checkoutConfig?.providers.find((p) => p.code === code)?.display_name ?? code

  if (!packages || packages.length === 0) {
    return null
  }

  const canPay = Boolean(selectedPackage) && Boolean(selectedProvider)

  const handlePay = async () => {
    if (!selectedPackage || !canPay) return
    setBackstopOrderId(null) // a new checkout supersedes any prior backstop
    setSubmitting(true)
    try {
      const result = await createTopupOrder({
        topup_code: selectedPackage.code,
        provider: selectedProvider,
      })
      // Same pending-order stash as CheckoutCard so the billing status banner
      // keeps confirming the order even without the success redirect.
      try {
        window.localStorage.setItem(
          "avt_pending_order",
          JSON.stringify({
            order_id: result.order_id,
            provider: selectedProvider,
            ts: Date.now(),
          }),
        )
      } catch {
        // localStorage unavailable — non-fatal; webhook settles server-side
      }
      if (result.display_mode === "qrcode" && result.qr_code_url) {
        setQrCheckout({
          orderId: result.order_id,
          qrCodeUrl: result.qr_code_url,
          amountFen: result.amount_cny,
        })
        setSubmitting(false)
        return
      }
      if (!result.checkout_url) {
        throw new Error(tb("topup.errorNoCheckoutUrl"))
      }
      setRedirecting(true)
      window.location.assign(result.checkout_url)
    } catch (err) {
      const message = err instanceof Error ? err.message : tb("topup.errorCreateOrder")
      toast.error(message)
      setRedirecting(false)
      setSubmitting(false)
    }
  }

  return (
    <div className="rounded-lg border border-border bg-card p-6 space-y-6">
      <div>
        <div className="flex items-center gap-2">
          <ShoppingBag className="h-4 w-4 text-primary" />
          <h3 className="text-base font-semibold text-foreground">
            {tb("topup.title")}
          </h3>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">{tb("topup.subtitle")}</p>
      </div>

      {/* Package picker */}
      <div>
        <Label className="text-xs font-medium text-muted-foreground">
          {tb("topup.packageLabel")}
        </Label>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {packages.map((pkg) => {
            const active = pkg.code === selectedCode
            return (
              <button
                key={pkg.code}
                type="button"
                onClick={() => setSelectedCode(pkg.code)}
                className={cn(
                  "rounded-md border px-4 py-3 text-left transition-colors",
                  active ? ACTIVE_CHOICE_CLASS : INACTIVE_CHOICE_CLASS,
                )}
              >
                <div className="text-sm font-semibold">
                  {tb("topup.packageCredits", { credits: pkg.credits })}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {formatYuan(pkg.price_cny_fen)}
                </div>
              </button>
            )
          })}
        </div>
      </div>

      {/* Provider picker — only when there is a real choice for this SKU. */}
      {availableProviders.length > 1 && (
        <div>
          <Label className="text-xs font-medium text-muted-foreground">
            {tb("topup.providerLabel")}
          </Label>
          <div className="mt-2 flex flex-wrap gap-2">
            {availableProviders.map((code) => (
              <button
                key={code}
                type="button"
                onClick={() => setPickedProvider(code)}
                className={cn(
                  "rounded-md border px-3 py-1.5 text-sm font-medium transition-colors",
                  code === selectedProvider
                    ? ACTIVE_CHOICE_CLASS
                    : "border-border bg-background text-muted-foreground hover:text-foreground",
                )}
              >
                {providerDisplayName(code)}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Summary */}
      <div className="rounded-md border border-border bg-background px-4 py-3 text-sm">
        <div className="flex items-center justify-between">
          <span className="text-muted-foreground">{tb("topup.amountDue")}</span>
          <span className="text-lg font-semibold tabular-nums text-foreground">
            {selectedPackage ? formatYuan(selectedPackage.price_cny_fen) : "--"}
          </span>
        </div>
        <div className="mt-2 flex items-center justify-between text-xs">
          <span className="text-muted-foreground">{tb("topup.providerLabel")}</span>
          <span className="text-foreground">
            {selectedProvider ? providerDisplayName(selectedProvider) : "--"}
          </span>
        </div>
        {selectedProvider === "paypal" &&
          selectedPackage?.price_usd_cents != null && (
            <div className="mt-1 text-right text-[11px] text-muted-foreground">
              {tb("topup.usdNotice", {
                amount: `$${(selectedPackage.price_usd_cents / 100).toFixed(2)}`,
              })}
            </div>
          )}
      </div>

      {/* CTA */}
      <Button
        type="button"
        className="h-11 w-full"
        disabled={!canPay || submitting}
        onClick={handlePay}
      >
        {submitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
        {redirecting
          ? tb("topup.redirecting")
          : submitting
            ? tb("topup.submitting")
            : tb("topup.pay")}
      </Button>

      <p className="text-xs leading-relaxed text-muted-foreground">
        {tb("topup.footnote")}
      </p>

      {qrCheckout && (
        <WechatQrDialog
          orderId={qrCheckout.orderId}
          qrCodeUrl={qrCheckout.qrCodeUrl}
          amountFen={qrCheckout.amountFen}
          onClose={() => {
            // Closed while still pending — keep a card-level poller alive so
            // a scan-then-close payment still lands without a page reload.
            setBackstopOrderId(qrCheckout.orderId)
            setQrCheckout(null)
          }}
          onPaid={() => {
            setQrCheckout(null)
            try {
              window.localStorage.removeItem("avt_pending_order")
            } catch {
              // non-fatal
            }
            toast.success(tb("topup.paidToast"))
            onOrderSettled?.()
          }}
        />
      )}
    </div>
  )
}
