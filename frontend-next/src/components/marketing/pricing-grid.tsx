"use client"

import Link from "next/link"
import { Check, Minus } from "lucide-react"
import { buttonVariants } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import { useSession } from "@/components/providers/session-provider"
import { cn } from "@/lib/utils"
import type { Plan, PlanPriceMap } from "@/lib/billing/types"
import { usePlans } from "./use-plans"

/**
 * Three-tier pricing grid consumed from the gateway truth source (GET /api/plans).
 *
 * Strict rules (DESIGN.md §3.5 + T2 preflight):
 * - Exactly three tiers: Free / Plus / Pro. Plus is visually highlighted.
 * - Trial is NEVER a fourth card — the trial entry is rendered elsewhere
 *   (TrialBanner) as a banner / conversion entry.
 * - Prices, minutes, concurrency caps, and quotas come from the API. No local
 *   fallback numbers. If the API is unavailable, show an empty / error state
 *   rather than inventing defaults.
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

/**
 * Plan CTA href is session-aware so logged-in visitors don't get dropped back on
 * the registration page (which isn't designed as an "upgrade" landing).
 *
 * Guest path (default, Task 3): every tier routes to `/auth` (phone-first main
 * path), which is the only supported public registration entry.
 *
 * Logged-in path: every tier routes to `/translations/new`. Upgrade handling
 * for already-authenticated users is a later-milestone concern (Task 4
 * subscription / checkout flow). Until that ships, logged-in visitors are
 * sent to the workspace so the conversion path never regresses to an auth screen.
 */
function planCtaHref(
  plan: Plan,
  isAuthenticated: boolean,
): { href: string; label: string } {
  const authedHref = "/translations/new"
  const guestHref = "/auth"
  if (plan.code === "free") {
    return {
      href: isAuthenticated ? authedHref : guestHref,
      label: isAuthenticated ? "进入工作台" : "免费开始",
    }
  }
  if (plan.self_serve) {
    return {
      href: isAuthenticated ? authedHref : guestHref,
      label: isAuthenticated ? "进入工作台" : `选择 ${plan.display_name}`,
    }
  }
  return {
    href: isAuthenticated ? authedHref : guestHref,
    label: "联系我们",
  }
}

function PlanCard({
  plan,
  highlight,
  isAuthenticated,
}: {
  plan: Plan
  highlight: boolean
  isAuthenticated: boolean
}) {
  const price = monthlyPriceLabel(plan.price_cny_fen)
  const cta = planCtaHref(plan, isAuthenticated)
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
        <h3 className="text-lg font-semibold text-foreground">{plan.display_name}</h3>
        <p className="text-sm text-muted-foreground min-h-[1.25rem]">
          {plan.code === "free" && "适合个人创作者试水与小型项目"}
          {plan.code === "plus" && "适合稳定输出的独立创作者"}
          {plan.code === "pro" && "适合高频输出的团队与工作室"}
        </p>
      </div>

      <div className="mt-5 flex items-baseline gap-1">
        {price ? (
          <>
            <span className="text-3xl font-semibold text-foreground tabular-nums">
              {price.amount}
            </span>
            <span className="text-sm text-muted-foreground">{price.unit}</span>
          </>
        ) : (
          <span className="text-3xl font-semibold text-foreground">免费</span>
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
        <Link
          href={cta.href}
          className={cn(
            buttonVariants({ variant: highlight ? "default" : "outline", size: "lg" }),
            "h-11 w-full",
          )}
        >
          {cta.label}
        </Link>
      </div>
    </div>
  )
}

function PlanCardSkeleton() {
  return (
    <div className="flex flex-col rounded-xl border border-border bg-card p-6">
      <Skeleton className="h-5 w-24" />
      <Skeleton className="mt-2 h-4 w-40" />
      <Skeleton className="mt-5 h-9 w-28" />
      <div className="mt-6 space-y-3">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-11/12" />
        <Skeleton className="h-4 w-10/12" />
        <Skeleton className="h-4 w-9/12" />
      </div>
      <Skeleton className="mt-8 h-10 w-full" />
    </div>
  )
}

export function PricingGrid() {
  const state = usePlans()
  const { user } = useSession()
  const isAuthenticated = Boolean(user)

  if (state.status === "loading") {
    return (
      <div className="grid gap-6 md:grid-cols-3">
        <PlanCardSkeleton />
        <PlanCardSkeleton />
        <PlanCardSkeleton />
      </div>
    )
  }

  if (state.status === "error") {
    return (
      <div className="rounded-xl border border-border bg-card p-8 text-center">
        <p className="text-sm text-muted-foreground">
          套餐信息暂时无法加载，请稍后重试。
        </p>
      </div>
    )
  }

  const byCode = new Map(state.data.plans.map((p) => [p.code, p]))
  const ordered = PLAN_ORDER.map((code) => byCode.get(code)).filter(
    (p): p is Plan => Boolean(p),
  )

  if (ordered.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-card p-8 text-center">
        <p className="text-sm text-muted-foreground">暂无可展示的套餐。</p>
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
          isAuthenticated={isAuthenticated}
        />
      ))}
    </div>
  )
}
