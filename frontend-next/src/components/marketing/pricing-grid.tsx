import { Check, Minus } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { getPlansSafeServer } from "@/lib/billing/get-plans"
import type { Plan, PlanPriceMap } from "@/lib/billing/types"
import { PlanCardCta } from "./plan-card-cta"

/**
 * Three-tier pricing grid — Server Component variant.
 *
 * Why Server Component:
 *   The previous version was `"use client"` and fetched plans via a useEffect
 *   in `usePlans()`. As a result, the SSR HTML contained only skeleton cards
 *   and the live site showed a blank pricing area until JS hydrated. That
 *   broke first paint, SEO, and slow-network UX.
 *
 *   This rewrite fetches plans server-side via `getPlansSafeServer()`, ships
 *   real prices in the initial HTML, and only the CTA button (which depends
 *   on session) is a client component (`PlanCardCta`).
 *
 * Strict rules (DESIGN.md §3.5 + plan §7):
 *   - Exactly three tiers: Free / Plus / Pro. Plus is highlighted.
 *   - Trial is rendered elsewhere (TrialBanner), never as a fourth card.
 *   - Prices come from `/api/plans`. On gateway failure, fallback to empty
 *     state with a friendly message — never invent default numbers.
 *   - Session-aware CTA logic stays a client island.
 *
 * See: docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md §7 PricingPreview
 */
const PLAN_ORDER = ["free", "plus", "pro"] as const

const HIGHLIGHT_CODE = "plus"

function formatYuan(fen: number): string {
  const yuan = fen / 100
  return yuan % 1 === 0 ? `¥${yuan.toFixed(0)}` : `¥${yuan.toFixed(2)}`
}

function monthlyPriceLabel(price: PlanPriceMap | null): { amount: string; unit: string } | null {
  if (!price) return null
  if (price.monthly != null) return { amount: formatYuan(price.monthly), unit: "/ 月" }
  if (price.quarterly != null) return { amount: formatYuan(price.quarterly), unit: "/ 季" }
  if (price.annual != null) return { amount: formatYuan(price.annual), unit: "/ 年" }
  return null
}

function planBenefits(plan: Plan): Array<{ label: string; included: boolean }> {
  const benefits: Array<{ label: string; included: boolean }> = []
  benefits.push({
    label: `单次视频最长 ${plan.max_duration_minutes} 分钟`,
    included: true,
  })
  benefits.push({
    label: `最多 ${plan.max_concurrent_jobs} 个任务并行处理`,
    included: true,
  })
  // Monthly processing quota — Plus/Pro subscriptions grant a monthly credit
  // pool that converts to source-minutes via the per-mode debit rate (Express
  // standard 10 cr/min, Studio standard 15, Studio high 30, Studio flagship
  // 50 — see gateway/credits_service.DEBIT_RATES). The exact credit grant
  // (Plus 3500 / Pro 12000) is not exposed via /api/plans yet, so we use
  // ChatGPT's safe fallback wording per 2026-05-01 marketing consult: tell
  // the visitor they get a monthly pool, point them at the dashboard for
  // the precise figure. When `_plan_to_public_dict` is extended to include
  // monthly_grant_credits, swap this for "约 N 分钟 Express / N 分钟 Studio"
  // computed from the rate table.
  if (plan.free_quota_total === undefined) {
    benefits.push({
      label: "包含月度处理额度，按视频实际时长计费",
      included: true,
    })
  }
  const hasStudio = plan.allowed_service_modes.includes("studio")
  benefits.push({
    label: "Express 快速模式",
    included: plan.allowed_service_modes.includes("express"),
  })
  benefits.push({
    label: "Studio 精校模式（支持人工复核）",
    included: hasStudio,
  })
  if (plan.free_quota_total !== undefined) {
    benefits.push({
      label: `${plan.free_quota_total} 条免费任务额度`,
      included: true,
    })
  }
  return benefits
}

function PlanCard({
  plan,
  highlight,
}: {
  plan: Plan
  highlight: boolean
}) {
  const price = monthlyPriceLabel(plan.price_cny_fen)
  const benefits = planBenefits(plan)

  return (
    <div
      className={cn(
        "relative flex flex-col rounded-xl border bg-card p-6 shadow-sm transition-shadow",
        highlight
          ? "border-primary/70 ring-1 ring-primary/30 shadow-md"
          : "border-border hover:shadow-md",
      )}
    >
      {highlight && (
        <Badge className="absolute -top-3 left-6" variant="default">
          最受欢迎
        </Badge>
      )}
      <div className="space-y-1">
        <h3 className="ink-heading text-lg font-semibold text-foreground">
          {plan.display_name}
        </h3>
        <p className="text-sm text-muted-foreground min-h-[1.25rem]">
          {plan.code === "free" && "适合个人创作者试水与小型项目"}
          {plan.code === "plus" && "适合稳定输出的独立创作者"}
          {plan.code === "pro" && "适合高频输出的团队与工作室"}
        </p>
      </div>

      <div className="mt-5 flex items-baseline gap-1">
        {price ? (
          <>
            <span className="ink-num text-4xl font-bold text-foreground">
              {price.amount}
            </span>
            <span className="text-sm text-muted-foreground">{price.unit}</span>
          </>
        ) : (
          <span className="ink-display text-4xl text-foreground">免费</span>
        )}
      </div>

      <ul className="mt-6 space-y-3 text-sm" role="list">
        {benefits.map((b) => (
          <li key={b.label} className="flex items-start gap-2">
            {b.included ? (
              <Check className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden="true" />
            ) : (
              <Minus className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground/60" aria-hidden="true" />
            )}
            <span
              className={cn(
                "leading-relaxed",
                b.included ? "text-foreground/90" : "text-muted-foreground line-through",
              )}
            >
              {b.label}
            </span>
          </li>
        ))}
      </ul>

      <div className="mt-auto pt-6">
        <PlanCardCta plan={plan} highlight={highlight} />
      </div>
    </div>
  )
}

export async function PricingGrid() {
  const data = await getPlansSafeServer()
  const byCode = new Map(data.plans.map((p) => [p.code, p]))
  const ordered = PLAN_ORDER.map((code) => byCode.get(code)).filter(
    (p): p is Plan => Boolean(p),
  )

  if (ordered.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-card p-8 text-center">
        <p className="text-sm text-muted-foreground">
          套餐信息暂时无法加载，请稍后重试。
        </p>
      </div>
    )
  }

  return (
    <div className="grid gap-6 md:grid-cols-3">
      {ordered.map((plan) => (
        <PlanCard
          key={plan.code}
          plan={plan}
          highlight={plan.code === HIGHLIGHT_CODE}
        />
      ))}
    </div>
  )
}
