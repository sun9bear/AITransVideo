import { Check, Minus } from "lucide-react"
import { getTranslations } from "next-intl/server"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { getPlansSafeServer } from "@/lib/billing/get-plans"
import type { CreditsPerMinute, Plan, PlanPriceMap } from "@/lib/billing/types"
import { PlanCardCta } from "./plan-card-cta"

type GridTranslator = Awaited<ReturnType<typeof getTranslations<"marketing.pricingGrid">>>

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

function monthlyPriceLabel(
  price: PlanPriceMap | null,
  t: GridTranslator,
): { amount: string; unit: string } | null {
  if (!price) return null
  if (price.monthly != null) return { amount: formatYuan(price.monthly), unit: t("unitMonthly") }
  if (price.quarterly != null) return { amount: formatYuan(price.quarterly), unit: t("unitQuarterly") }
  if (price.annual != null) return { amount: formatYuan(price.annual), unit: t("unitAnnual") }
  return null
}

/**
 * Format a credit count + per-minute rate into a readable minute estimate.
 * "Math.floor" is intentional — it's better to under-promise (e.g. 3500/15=233)
 * than to round up (234) and have a buyer argue they got a fractional minute
 * less than advertised.
 */
function creditsToMinutes(grant: number, ratePerMin?: number): number | null {
  if (!ratePerMin || ratePerMin <= 0) return null
  return Math.floor(grant / ratePerMin)
}

function planBenefits(
  plan: Plan,
  creditsPerMinute: CreditsPerMinute | undefined,
  t: GridTranslator,
): Array<{ label: string; included: boolean }> {
  const benefits: Array<{ label: string; included: boolean }> = []
  benefits.push({
    label: t("benefitMaxDuration", { minutes: plan.max_duration_minutes }),
    included: true,
  })
  benefits.push({
    label: t("benefitConcurrency", { jobs: plan.max_concurrent_jobs }),
    included: true,
  })

  // Monthly processing quota line. With 2026-05-02+ gateway, /api/plans
  // returns `monthly_grant_credits` per plan + top-level `credits_per_minute`
  // map. We compute Express + Studio-standard minute estimates from those
  // and show one combined line so paid-tier buyers know exactly what they get.
  // Older gateway / runtime config without these fields falls back to the
  // qualitative wording so the card never breaks.
  if (plan.free_quota_total === undefined && plan.monthly_grant_credits) {
    const expRate = creditsPerMinute?.express_standard
    const studioRate = creditsPerMinute?.studio_standard
    const expMin = creditsToMinutes(plan.monthly_grant_credits, expRate)
    const studioMin = creditsToMinutes(plan.monthly_grant_credits, studioRate)
    if (expMin !== null && studioMin !== null) {
      benefits.push({
        label: t("benefitGrantWithMinutes", {
          credits: plan.monthly_grant_credits,
          expMin,
          studioMin,
        }),
        included: true,
      })
    } else {
      // Have grant but missing rate map — show grant only.
      benefits.push({
        label: t("benefitGrantOnly", { credits: plan.monthly_grant_credits }),
        included: true,
      })
    }
  } else if (plan.free_quota_total === undefined) {
    // No grant data at all — qualitative fallback.
    benefits.push({
      label: t("benefitGrantQualitative"),
      included: true,
    })
  }

  const hasStudio = plan.allowed_service_modes.includes("studio")
  benefits.push({
    label: t("benefitExpress"),
    included: plan.allowed_service_modes.includes("express"),
  })
  benefits.push({
    label: t("benefitStudio"),
    included: hasStudio,
  })
  if (plan.free_quota_total !== undefined) {
    benefits.push({
      label: t("benefitFreeQuota", { quota: plan.free_quota_total }),
      included: true,
    })
  }
  return benefits
}

function PlanCard({
  plan,
  highlight,
  creditsPerMinute,
  t,
}: {
  plan: Plan
  highlight: boolean
  creditsPerMinute: CreditsPerMinute | undefined
  t: GridTranslator
}) {
  const price = monthlyPriceLabel(plan.price_cny_fen, t)
  const benefits = planBenefits(plan, creditsPerMinute, t)

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
          {t("popular")}
        </Badge>
      )}
      <div className="space-y-1">
        <h3 className="ink-heading text-lg font-semibold text-foreground">
          {plan.display_name}
        </h3>
        <p className="text-sm text-muted-foreground min-h-[1.25rem]">
          {plan.code === "free" && t("subtitleFree")}
          {plan.code === "plus" && t("subtitlePlus")}
          {plan.code === "pro" && t("subtitlePro")}
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
          <span className="ink-display text-4xl text-foreground">{t("free")}</span>
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
  const t = await getTranslations("marketing.pricingGrid")
  const data = await getPlansSafeServer()
  const byCode = new Map(data.plans.map((p) => [p.code, p]))
  const ordered = PLAN_ORDER.map((code) => byCode.get(code)).filter(
    (p): p is Plan => Boolean(p),
  )

  if (ordered.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-card p-8 text-center">
        <p className="text-sm text-muted-foreground">
          {t("emptyState")}
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
          creditsPerMinute={data.credits_per_minute}
          t={t}
        />
      ))}
    </div>
  )
}
