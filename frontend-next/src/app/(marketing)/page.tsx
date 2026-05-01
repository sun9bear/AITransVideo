import { Hero } from "@/components/marketing/hero"
import { PainPoints } from "@/components/marketing/pain-points"
import { ProductProof } from "@/components/marketing/product-proof"
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
 * Narrative arc (revised 2026-05-01 per ChatGPT consult round 2):
 *   [问题] Hero → PainPoints (4 concrete frustrations)
 *   [演示] ProductProof (real screenshots) → WorkflowShowcase → Features
 *   [对位] SuitedScenarios (是不是给我用的) → ToolComparison (vs 一键生成)
 *   [信任] TrustBanner → PricingPreview (with TrialBanner) → FAQ
 *   [行动] FinalCta
 *
 * SuitedScenarios + ToolComparison were added 2026-05-01 to bridge the gap
 * between "what the product does" and "is this for me / why not the simpler
 * one-click tool". Without them visitors landed on Pricing without ever
 * deciding the product matched their use case.
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
