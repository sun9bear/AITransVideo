import { Hero } from "@/components/marketing/hero"
import { PainPoints } from "@/components/marketing/pain-points"
import { ProductProof } from "@/components/marketing/product-proof"
import { FeaturedDemos } from "@/components/marketing/featured-demos"
import { Features } from "@/components/marketing/features"
import { WorkflowShowcase } from "@/components/marketing/workflow-showcase"
import { SuitedScenarios } from "@/components/marketing/suited-scenarios"
import { ToolComparison } from "@/components/marketing/tool-comparison"
import { TrustBanner } from "@/components/marketing/trust-banner"
import { PricingPreview } from "@/components/marketing/pricing-preview"
import { Faq } from "@/components/marketing/faq"
import { FinalCta } from "@/components/marketing/final-cta"

/**
 * Homepage (`/`).
 *
 * Narrative arc (revised 2026-05-01 per ChatGPT consult round 2 + featured
 * demos addition):
 *   [问题] Hero → PainPoints (4 concrete frustrations)
 *   [演示] ProductProof (real screenshots) → FeaturedDemos (real dub clips)
 *          → WorkflowShowcase → Features
 *   [对位] SuitedScenarios (是不是给我用的) → ToolComparison (vs 一键生成)
 *   [信任] TrustBanner → PricingPreview (with TrialBanner) → FAQ
 *   [行动] FinalCta
 *
 * FeaturedDemos was added 2026-05-01 to give "output-level" proof right
 * after the workspace-level proof from ProductProof. Visitors hear what the
 * actual dubbed content sounds like in 5 hand-picked 50-70s clips with
 * EN/CN tab switching. See: docs/specs/2026-05-01-marketing-featured-demos-design.md
 *
 * SuitedScenarios + ToolComparison bridge "what the product does" to "is
 * this for me / why not the simpler one-click tool".
 *
 * Server component — all client-side concerns (session-aware CTA, plans fetch)
 * live inside children. PricingPreview / TrialBanner are async Server Components
 * that read gateway data so prices ship in initial HTML.
 */
export default function HomePage() {
  return (
    <>
      <Hero />
      <PainPoints />
      <ProductProof />
      <FeaturedDemos />
      <WorkflowShowcase />
      <Features />
      <SuitedScenarios />
      <ToolComparison />
      <TrustBanner />
      <PricingPreview />
      <Faq variant="home" />
      <FinalCta />
    </>
  )
}
