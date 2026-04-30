import { Hero } from "@/components/marketing/hero"
import { PainPoints } from "@/components/marketing/pain-points"
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
 * Narrative arc (revised 2026-04-30 per ChatGPT consult):
 *   [问题] Hero (slogan + value prop) → PainPoints (4 concrete frustrations)
 *   [演示] ProductProof → WorkflowShowcase → Features (re-prioritised, 6 items)
 *   [信任] TrustBanner → PricingPreview (with TrialBanner) → FAQ
 *   [行动] FinalCta
 *
 * Why PainPoints sits between Hero and ProductProof: hero now leads with the
 * positive slogan ("让世界视频，开口说中文") rather than a 钩子 list, so the
 * 钩子 needs its own surface. PainPoints carries the "what's broken" beats
 * before ProductProof shows "what we built".
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
      <TrustBanner />
      <PricingPreview />
      <Faq variant="home" />
      <FinalCta />
    </>
  )
}
