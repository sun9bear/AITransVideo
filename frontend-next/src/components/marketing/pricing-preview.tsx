import { getTranslations } from "next-intl/server"
import { PricingGrid } from "./pricing-grid"
import { TrialBanner } from "./trial-banner"
import { LinkButton } from "./link-button"

/**
 * Homepage pricing teaser. Same three-tier grid as `/pricing`, but framed as a
 * preview with a link to the full pricing page. DESIGN.md §3.5 — Trial is a
 * banner / conversion entry above the grid, not a fourth card.
 */
export async function PricingPreview() {
  const t = await getTranslations("marketing.pricingPreview")

  return (
    <section
      id="pricing"
      className="marketing-reading-surface py-14 sm:py-16"
    >
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-2xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            {t("eyebrow")}
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            {t("heading")}
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            {t("lead")}
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
            {t("fullComparisonCta")}
          </LinkButton>
        </div>
      </div>
    </section>
  )
}
