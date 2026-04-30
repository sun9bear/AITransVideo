import { Hero } from "@/components/marketing/hero"
import { ProductProof } from "@/components/marketing/product-proof"
import { Features } from "@/components/marketing/features"
import { WorkflowShowcase } from "@/components/marketing/workflow-showcase"
import { TrustBanner } from "@/components/marketing/trust-banner"
import { PricingPreview } from "@/components/marketing/pricing-preview"
import { Faq } from "@/components/marketing/faq"
import { FinalCta } from "@/components/marketing/final-cta"

/**
 * Homepage (`/`).
 *
 * Narrative arc — see docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md §5:
 *   [问题] Hero
 *   [演示] ProductProof → WorkflowShowcase → Features
 *   [信任] TrustBanner → PricingPreview (with TrialBanner) → FAQ
 *   [行动] FinalCta
 *
 * Server component — all client-side concerns (session-aware CTA, plans fetch)
 * live inside children. PricingPreview / TrialBanner are async Server Components
 * that read gateway data so prices ship in initial HTML.
 */
export default function HomePage() {
  return (
    <>
      <Hero />
      <ProductProof />
      <WorkflowShowcase />
      <Features />
      <TrustBanner />
      <PricingPreview />
      <Faq variant="home" />
      <FinalCta />
    </>
  )
}
