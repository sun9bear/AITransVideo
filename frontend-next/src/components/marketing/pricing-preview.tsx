import { PricingGrid } from "./pricing-grid"
import { TrialBanner } from "./trial-banner"
import { LinkButton } from "./link-button"

/**
 * Homepage pricing teaser. Same three-tier grid as `/pricing`, but framed as a
 * preview with a link to the full pricing page. DESIGN.md §3.5 — Trial is a
 * banner / conversion entry above the grid, not a fourth card.
 */
export function PricingPreview() {
  return (
    <section className="marketing-reading-surface py-20 sm:py-24">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-2xl text-center">
          <p className="text-xs font-semibold uppercase tracking-wider text-primary">
            简单透明的定价
          </p>
          <h2 className="mt-3 text-3xl font-bold tracking-tight text-foreground sm:text-4xl">
            只为你真正用到的分钟数买单
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            独创的增量重生成计费模式，改一句台词只扣一句的钱。
          </p>
        </div>

        <div className="mt-10">
          <TrialBanner />
        </div>

        <div className="mt-10">
          <PricingGrid />
        </div>

        <div className="mt-10 text-center">
          <LinkButton href="/pricing" variant="outline">
            查看完整套餐对比
          </LinkButton>
        </div>
      </div>
    </section>
  )
}
