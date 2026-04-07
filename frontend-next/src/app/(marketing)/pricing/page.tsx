import type { Metadata } from "next"
import { PricingGrid } from "@/components/marketing/pricing-grid"
import { TrialBanner } from "@/components/marketing/trial-banner"
import { Faq } from "@/components/marketing/faq"

export const metadata: Metadata = {
  title: "定价 · AIVideoTrans",
  description: "AIVideoTrans 套餐与定价：Free / Plus / Pro 三档透明定价，支持免费试用。",
}

/**
 * `/pricing` — dedicated pricing page.
 *
 * Sections:
 *   1. Header copy
 *   2. Trial banner (not a fourth plan card — DESIGN.md §3.5)
 *   3. Three-tier grid (Free / Plus / Pro, Plus highlighted) — consumed from
 *      `GET /api/plans` via <PricingGrid />
 *   4. Pricing-focused FAQ with the "what happens after trial ends" answer
 *
 * All numeric facts come from the gateway truth source. There are no hardcoded
 * prices, minutes, concurrency limits, or quotas in this file.
 */
export default function PricingPage() {
  return (
    <>
      <section className="marketing-reading-surface pt-16 pb-10 sm:pt-20 sm:pb-12">
        <div className="mx-auto max-w-3xl px-4 text-center sm:px-6 lg:px-8">
          <p className="text-xs font-semibold uppercase tracking-wider text-primary">
            定价
          </p>
          <h1 className="mt-3 text-4xl font-bold tracking-tight text-foreground sm:text-5xl">
            简单透明，为实际产出买单
          </h1>
          <p className="mt-5 zh-body-lg text-muted-foreground">
            三档套餐覆盖从个人尝试到团队稳定输出的不同需求。改一句台词只扣一句的钱，不为整条片子再付一次。
          </p>
        </div>
      </section>

      <section className="marketing-reading-surface pb-20 sm:pb-24">
        <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
          <TrialBanner />
          <div className="mt-10">
            <PricingGrid />
          </div>
        </div>
      </section>

      <Faq variant="pricing" />
    </>
  )
}
