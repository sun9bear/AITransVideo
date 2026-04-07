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

import { useMemo, useState } from "react"
import { toast } from "sonner"
import { Loader2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { cn } from "@/lib/utils"
import type { Plan, PlanPriceMap, BillingPeriod } from "@/lib/billing/types"
import type { MeSubscriptionResponse } from "@/lib/billing/get-subscription"
import type { CheckoutConfigResponse } from "@/lib/billing/get-checkout-config"
import { createOrder } from "@/lib/billing/create-order"

const PLAN_RANK: Record<string, number> = {
  free: 0,
  plus: 1,
  pro: 2,
}

const PERIOD_LABELS: Record<BillingPeriod, string> = {
  monthly: "月付",
  quarterly: "季付",
  annual: "年付",
}

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

type CheckoutCardProps = {
  plans: Plan[]
  subscription: MeSubscriptionResponse | null
  checkoutConfig: CheckoutConfigResponse | null
}

export function CheckoutCard({
  plans,
  subscription,
  checkoutConfig,
}: CheckoutCardProps) {
  const paidPlans = useMemo(
    () => plans.filter((p) => p.price_cny_fen !== null),
    [plans],
  )

  const [selectedPlanCode, setSelectedPlanCode] = useState<string>(
    paidPlans[0]?.code ?? "",
  )
  const [selectedPeriod, setSelectedPeriod] = useState<BillingPeriod>("monthly")
  const [submitting, setSubmitting] = useState(false)

  const selectedPlan = paidPlans.find((p) => p.code === selectedPlanCode) ?? null
  const periods = periodsForPlan(selectedPlan?.price_cny_fen ?? null)
  const effectivePeriod = periods.includes(selectedPeriod)
    ? selectedPeriod
    : periods[0] ?? "monthly"

  const priceFen =
    selectedPlan?.price_cny_fen?.[effectivePeriod] ?? null

  const currentRank = currentPlanRank(subscription)
  const selectedRank = PLAN_RANK[selectedPlanCode] ?? 0
  const isDowngradeOrSame = selectedRank <= currentRank && currentRank > 0

  // Provider selection is gateway-owned. Use the default the server told us.
  const defaultProvider = checkoutConfig?.default_provider ?? "fake"
  const providerEntry = checkoutConfig?.providers.find(
    (p) => p.code === defaultProvider,
  )
  const providerDisplay = providerEntry?.display_name ?? "测试支付"
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
        provider: defaultProvider,
      })
      if (!result.checkout_url) {
        throw new Error("未收到支付链接,请稍后重试")
      }
      // Hand off to the provider checkout URL. For fake provider this is
      // `/api/billing/fake-pay/{order_id}`; for Alipay this is the signed
      // payment URL from the gateway.
      toast.success("正在跳转到支付页…")
      await new Promise((r) => setTimeout(r, 300))
      window.location.href = result.checkout_url
    } catch (err) {
      const message = err instanceof Error ? err.message : "创建订单失败"
      toast.error(message)
    } finally {
      setSubmitting(false)
    }
  }

  if (paidPlans.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-card p-6 text-sm text-muted-foreground">
        当前没有可购买的套餐。
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-border bg-card p-6 space-y-6">
      <div>
        <h3 className="text-base font-semibold text-foreground">选择套餐</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          支付完成后将立即升级你的账户权益。
        </p>
      </div>

      {/* Plan picker */}
      <div>
        <Label className="text-xs font-medium text-muted-foreground">套餐</Label>
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
                  active
                    ? "border-primary/60 bg-primary/5 text-foreground"
                    : "border-border bg-background text-foreground hover:border-primary/40",
                  disabled && "cursor-not-allowed opacity-50",
                )}
              >
                <div className="text-sm font-semibold">{plan.display_name}</div>
                <div className="mt-1 text-xs text-muted-foreground">
                  单次视频 {plan.max_duration_minutes} 分钟 · {plan.max_concurrent_jobs} 个并行任务
                </div>
                {disabled && (
                  <div className="mt-1 text-[11px] text-muted-foreground">
                    当前套餐已覆盖此级别
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
            计费周期
          </Label>
          <div className="mt-2 flex flex-wrap gap-2">
            {periods.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setSelectedPeriod(p)}
                className={cn(
                  "rounded-md border px-3 py-1.5 text-sm transition-colors",
                  effectivePeriod === p
                    ? "border-primary/60 bg-primary/5 text-foreground"
                    : "border-border bg-background text-muted-foreground hover:text-foreground",
                )}
              >
                {PERIOD_LABELS[p]}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Summary */}
      <div className="rounded-md border border-border bg-background px-4 py-3 text-sm">
        <div className="flex items-center justify-between">
          <span className="text-muted-foreground">应付金额</span>
          <span className="text-lg font-semibold tabular-nums text-foreground">
            {priceFen != null ? formatYuan(priceFen) : "--"}
          </span>
        </div>
        <div className="mt-2 flex items-center justify-between text-xs">
          <span className="text-muted-foreground">支付方式</span>
          <span
            className={cn(
              "text-foreground",
              !providerOperational && "text-muted-foreground",
            )}
          >
            {providerDisplay}
            {!providerOperational && <span className="ml-1">(暂未开放)</span>}
          </span>
        </div>
      </div>

      {/* CTA */}
      <Button
        type="button"
        className="h-11 w-full"
        disabled={!canCheckout || submitting}
        onClick={handleCheckout}
      >
        {submitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
        {submitting ? "正在创建订单…" : "立即支付"}
      </Button>

      <p className="text-xs leading-relaxed text-muted-foreground">
        支付渠道和价格均由服务器侧配置,试用与退款规则参见后续通知。本次支付仅创建当前选中套餐的订单,不会自动续费。
      </p>
    </div>
  )
}
