"use client"

/**
 * Minimal checkout card for the in-app billing page (Task 5).
 *
 * Responsibilities:
 * - Let the user pick a paid plan (from `/api/plans`) and a billing period.
 * - Show the effective price and the current default provider (from
 *   `/api/billing/checkout-config`).
 * - Create an order via `POST /api/billing/orders` and hand off to
 *   `checkout_url` (either the real provider URL or the fake-pay path).
 *
 * Non-goals (stay out of T6 scope):
 * - Full billing history UI
 * - Invoice table
 * - Refund / cancellation UX
 * - Mandate management
 *
 * Guardrails:
 * - `free` tier is never shown here; it has no `price_cny_fen`.
 * - If the user already has an active subscription at the same or higher
 *   tier, the CTA for that tier is disabled. Server-side `create_order` also
 *   enforces this — the frontend gate is UX only.
 * - All numeric facts come from the gateway. Nothing is hardcoded.
 */

import { useEffect, useMemo, useState } from "react"
import { useTranslations } from "next-intl"
import { toast } from "sonner"
import { Loader2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { cn } from "@/lib/utils"
import type { Plan, PlanPriceMap, BillingPeriod } from "@/lib/billing/types"
import type { MeSubscriptionResponse } from "@/lib/billing/get-subscription"
import type { CheckoutConfigResponse } from "@/lib/billing/get-checkout-config"
import { createOrder } from "@/lib/billing/create-order"
import { WechatQrDialog } from "@/components/billing/wechat-qr-dialog"

const PLAN_RANK: Record<string, number> = {
  free: 0,
  plus: 1,
  pro: 2,
}

type BillingKey = Parameters<ReturnType<typeof useTranslations<"appBilling">>>[0]

const PERIOD_LABEL_KEYS: Record<BillingPeriod, BillingKey> = {
  monthly: "period.monthly",
  quarterly: "period.quarterly",
  annual: "period.annual",
}

const ACTIVE_CHOICE_CLASS =
  "border-primary bg-background text-primary shadow-[inset_0_0_0_1px_var(--primary)]"
const INACTIVE_CHOICE_CLASS =
  "border-border bg-background text-foreground hover:border-primary/40"

function formatYuan(fen: number): string {
  const yuan = fen / 100
  return yuan % 1 === 0 ? `¥${yuan.toFixed(0)}` : `¥${yuan.toFixed(2)}`
}

function periodsForPlan(price: PlanPriceMap | null): BillingPeriod[] {
  if (!price) return []
  const periods: BillingPeriod[] = []
  if (price.monthly != null) periods.push("monthly")
  if (price.quarterly != null) periods.push("quarterly")
  if (price.annual != null) periods.push("annual")
  return periods
}

function currentPlanRank(sub: MeSubscriptionResponse | null): number {
  if (!sub) return 0
  if (sub.subscription_status !== "active") return 0
  return PLAN_RANK[sub.plan_code] ?? 0
}

type QrCheckoutState = {
  orderId: string
  qrCodeUrl: string
  amountFen: number
}

type CheckoutCardProps = {
  plans: Plan[]
  subscription: MeSubscriptionResponse | null
  checkoutConfig: CheckoutConfigResponse | null
  onOrderSettled?: () => void
}

export function CheckoutCard({
  plans,
  subscription,
  checkoutConfig,
  onOrderSettled,
}: CheckoutCardProps) {
  const t = useTranslations("billing")
  const tb = useTranslations("appBilling")
  const paidPlans = useMemo(
    () => plans.filter((p) => p.price_cny_fen !== null),
    [plans],
  )

  const [selectedPlanCode, setSelectedPlanCode] = useState<string>(() => {
    // 定价页「升级到 X」CTA 带 ?plan= 直达；本组件仅在客户端数据就绪后挂载，
    // 不参与 SSR，读 window.location 不会产生 hydration 分歧。
    if (typeof window !== "undefined") {
      const fromQuery = new URLSearchParams(window.location.search).get("plan")
      if (fromQuery && paidPlans.some((p) => p.code === fromQuery)) {
        return fromQuery
      }
    }
    return paidPlans[0]?.code ?? ""
  })
  const [selectedPeriod, setSelectedPeriod] = useState<BillingPeriod>("monthly")
  const [submitting, setSubmitting] = useState(false)
  const [redirecting, setRedirecting] = useState(false)
  // User's explicit channel pick; null = follow the gateway recommendation.
  const [pickedProvider, setPickedProvider] = useState<string | null>(null)
  const [qrCheckout, setQrCheckout] = useState<QrCheckoutState | null>(null)

  useEffect(() => {
    // Browser Back from the provider checkout restores this page from the
    // bfcache with JS state intact — without this reset the pay button would
    // stay disabled on "正在跳转支付页…" forever.
    const handlePageShow = (event: PageTransitionEvent) => {
      if (event.persisted) {
        setSubmitting(false)
        setRedirecting(false)
      }
    }
    window.addEventListener("pageshow", handlePageShow)
    return () => window.removeEventListener("pageshow", handlePageShow)
  }, [])

  const selectedPlan = paidPlans.find((p) => p.code === selectedPlanCode) ?? null
  const periods = periodsForPlan(selectedPlan?.price_cny_fen ?? null)
  const effectivePeriod = periods.includes(selectedPeriod)
    ? selectedPeriod
    : periods[0] ?? "monthly"

  const priceFen =
    selectedPlan?.price_cny_fen?.[effectivePeriod] ?? null
  // PayPal lane USD price (USD cents) for the selected plan/period, surfaced as
  // an "≈ $X" notice when PayPal is the picked rail (USD comes from the backend
  // /api/plans, never computed/converted on the client).
  const paypalUsdCents =
    selectedPlan?.price_usd_cents?.[effectivePeriod] ?? null

  const currentRank = currentPlanRank(subscription)
  const selectedRank = PLAN_RANK[selectedPlanCode] ?? 0
  const isDowngradeOrSame = selectedRank <= currentRank && currentRank > 0

  // Provider AVAILABILITY is gateway-owned (checkout-config). When 2+ rails
  // are operational the user picks one (P3 three-rail selection); the initial
  // pick follows the gateway's recommendation, currently wechatpay first on
  // both desktop and mobile.
  const operationalProviders =
    checkoutConfig?.providers.filter((p) => p.operational) ?? []
  const gatewayPick =
    checkoutConfig?.recommended_provider ?? checkoutConfig?.default_provider ?? "fake"
  const selectedProvider =
    pickedProvider !== null &&
    operationalProviders.some((p) => p.code === pickedProvider)
      ? pickedProvider
      : gatewayPick
  const providerEntry = checkoutConfig?.providers.find(
    (p) => p.code === selectedProvider,
  )
  const providerDisplay = providerEntry?.display_name ?? tb("checkout.providerDefault")
  const providerOperational = providerEntry?.operational ?? true

  const canCheckout =
    Boolean(selectedPlan) &&
    priceFen != null &&
    !isDowngradeOrSame &&
    providerOperational

  const handleCheckout = async () => {
    if (!selectedPlan || !canCheckout) return
    setSubmitting(true)
    try {
      const result = await createOrder({
        target_plan_code: selectedPlan.code,
        billing_period: effectivePeriod,
        provider: selectedProvider,
      })
      // Stash the pending order so the billing page can keep confirming and
      // auto-update even if the user closes the (async) WeChat QR page without
      // returning through the success redirect. Banner reads "avt_pending_order".
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
        // localStorage unavailable — non-fatal; webhook still settles server-side
      }
      if (result.display_mode === "qrcode" && result.qr_code_url) {
        // WeChat Native: render the weixin:// string as an in-page QR dialog
        // instead of navigating (it is not a web URL).
        setQrCheckout({
          orderId: result.order_id,
          qrCodeUrl: result.qr_code_url,
          amountFen: result.amount_cny,
        })
        setSubmitting(false)
        return
      }
      if (!result.checkout_url) {
        throw new Error(tb("checkout.errorNoCheckoutUrl"))
      }
      // Hand off to the provider checkout URL immediately — every ms of delay
      // on the pay CTA is conversion loss, and a toast would be destroyed by
      // the navigation anyway. Keep the button disabled (redirecting) until
      // the page actually unloads so a double-click can't create a 2nd order.
      setRedirecting(true)
      window.location.assign(result.checkout_url)
    } catch (err) {
      const message = err instanceof Error ? err.message : tb("checkout.errorCreateOrder")
      toast.error(message)
      setRedirecting(false)
      setSubmitting(false)
    }
  }

  if (paidPlans.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-card p-6 text-sm text-muted-foreground">
        {tb("checkout.noPlans")}
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-border bg-card p-6 space-y-6">
      <div>
        <h3 className="text-base font-semibold text-foreground">{tb("checkout.title")}</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          {tb("checkout.subtitle")}
        </p>
      </div>

      {/* Plan picker */}
      <div>
        <Label className="text-xs font-medium text-muted-foreground">{tb("checkout.planLabel")}</Label>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {paidPlans.map((plan) => {
            const rank = PLAN_RANK[plan.code] ?? 0
            const disabled = rank <= currentRank && currentRank > 0
            const active = plan.code === selectedPlanCode
            return (
              <button
                key={plan.code}
                type="button"
                disabled={disabled}
                onClick={() => setSelectedPlanCode(plan.code)}
                className={cn(
                  "rounded-md border px-4 py-3 text-left transition-colors",
                  active ? ACTIVE_CHOICE_CLASS : INACTIVE_CHOICE_CLASS,
                  disabled && "cursor-not-allowed opacity-50",
                )}
              >
                <div className="text-sm font-semibold">{plan.display_name}</div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {tb("checkout.planMeta", {
                    minutes: plan.max_duration_minutes,
                    jobs: plan.max_concurrent_jobs,
                  })}
                </div>
                {disabled && (
                  <div className="mt-1 text-[11px] text-muted-foreground">
                    {tb("checkout.planCovered")}
                  </div>
                )}
              </button>
            )
          })}
        </div>
      </div>

      {/* Period picker */}
      {periods.length > 1 && (
        <div>
          <Label className="text-xs font-medium text-muted-foreground">
            {tb("checkout.periodLabel")}
          </Label>
          <div className="mt-2 flex flex-wrap gap-2">
            {periods.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setSelectedPeriod(p)}
                className={cn(
                  "rounded-md border px-3 py-1.5 text-sm font-medium transition-colors",
                  effectivePeriod === p
                    ? ACTIVE_CHOICE_CLASS
                    : "border-border bg-background text-muted-foreground hover:text-foreground",
                )}
              >
                {tb(PERIOD_LABEL_KEYS[p])}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Provider picker (P3 three-rail selection) — only when there is a
          real choice. Single-rail keeps the old static "支付方式" row. */}
      {operationalProviders.length > 1 && (
        <div>
          <Label className="text-xs font-medium text-muted-foreground">
            {tb("checkout.providerLabel")}
          </Label>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            {operationalProviders.map((p) => {
              const active = p.code === selectedProvider
              // Provider hint copy via next-intl message keys (no inline CJK —
              // uiloc:cjk-guard). Static keys (next-intl needs literal keys for
              // type-checking). Codes without a hint (e.g. fake) render none.
              const hint =
                p.code === "wechatpay"
                  ? t("checkout.providerHint.wechatpay")
                  : p.code === "paddle"
                    ? t("checkout.providerHint.paddle")
                    : p.code === "paypal"
                      ? t("checkout.providerHint.paypal")
                      : null
              return (
                <button
                  key={p.code}
                  type="button"
                  onClick={() => setPickedProvider(p.code)}
                  className={cn(
                    "rounded-md border px-4 py-3 text-left transition-colors",
                    active ? ACTIVE_CHOICE_CLASS : INACTIVE_CHOICE_CLASS,
                  )}
                >
                  <div className="text-sm font-medium">{p.display_name}</div>
                  {hint && (
                    <div className="mt-0.5 text-[11px] text-muted-foreground">
                      {hint}
                    </div>
                  )}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* Summary */}
      <div className="rounded-md border border-border bg-background px-4 py-3 text-sm">
        <div className="flex items-center justify-between">
          <span className="text-muted-foreground">{tb("checkout.amountDue")}</span>
          <span className="text-lg font-semibold tabular-nums text-foreground">
            {priceFen != null ? formatYuan(priceFen) : "--"}
          </span>
        </div>
        <div className="mt-2 flex items-center justify-between text-xs">
          <span className="text-muted-foreground">{tb("checkout.providerLabel")}</span>
          <span
            className={cn(
              "text-foreground",
              !providerOperational && "text-muted-foreground",
            )}
          >
            {providerDisplay}
            {!providerOperational && <span className="ml-1">{tb("checkout.providerUnavailable")}</span>}
          </span>
        </div>
        {selectedProvider === "paypal" && paypalUsdCents != null && (
          <div className="mt-1 text-right text-[11px] text-muted-foreground">
            {t("checkout.paypalUsdNotice", {
              amount: `$${(paypalUsdCents / 100).toFixed(2)}`,
            })}
          </div>
        )}
      </div>

      {/* CTA */}
      <Button
        type="button"
        className="h-11 w-full"
        disabled={!canCheckout || submitting}
        onClick={handleCheckout}
      >
        {submitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
        {redirecting ? tb("checkout.redirecting") : submitting ? tb("checkout.submitting") : tb("checkout.pay")}
      </Button>

      <p className="text-xs leading-relaxed text-muted-foreground">
        {tb("checkout.footnote")}
      </p>

      {qrCheckout && (
        <WechatQrDialog
          orderId={qrCheckout.orderId}
          qrCodeUrl={qrCheckout.qrCodeUrl}
          amountFen={qrCheckout.amountFen}
          onClose={() => setQrCheckout(null)}
          onPaid={() => {
            setQrCheckout(null)
            try {
              window.localStorage.removeItem("avt_pending_order")
            } catch {
              // non-fatal
            }
            toast.success(tb("checkout.paidToast"))
            onOrderSettled?.()
          }}
        />
      )}
    </div>
  )
}
