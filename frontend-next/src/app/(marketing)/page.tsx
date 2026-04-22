import { Hero } from "@/components/marketing/hero"
import { ProductProof } from "@/components/marketing/product-proof"
import { Features } from "@/components/marketing/features"
import { WorkflowShowcase } from "@/components/marketing/workflow-showcase"
import { PricingPreview } from "@/components/marketing/pricing-preview"
import { Faq } from "@/components/marketing/faq"
import { FinalCta } from "@/components/marketing/final-cta"

/**
 * Homepage (`/`).
 *
 * Structure per DESIGN.md §3.2:
 *   Hero → product proof / demo → feature explanation → pricing preview → FAQ → final CTA
 *
 * This is a server component — all client-side concerns (session-aware CTA,
 * plans fetch) live inside the children, not here.
 */
export default function HomePage() {
  return (
    <>
      <Hero />
      <ProductProof />
      <Features />
      <WorkflowShowcase />
      <PricingPreview />
      <Faq variant="home" />
      <FinalCta />
    </>
  )
}
