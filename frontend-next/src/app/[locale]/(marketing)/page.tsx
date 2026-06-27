import type { Metadata } from "next"
import { Hero } from "@/components/marketing/hero"
import type { Locale } from "@/lib/seo/site"
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
import { SiteJsonLd } from "@/components/seo/site-json-ld"
import { absoluteUrl, hreflangLanguages, localeSeo } from "@/lib/seo/site"

/**
 * Homepage metadata（UI-03d-1 翻旗）。static `metadata` → `generateMetadata` 后按
 * UI page locale 产 localized canonical / hreflang / OG。
 *
 * **红线 R1（默认 zh 字节一致）**：zh 分支输出与改造前 static 对象逐字节相同 ——
 *   - canonical 保留相对字符串 `"/"`（与旧 static 完全一致；Next 相对 metadataBase 解析）；
 *     en 分支才用 `absoluteUrl("/", "en")` 绝对形。
 *   - OG title/description/url 用 `localeSeo[locale]`（zh 镜像顶层 defaultTitle/Description）
 *     与 `absoluteUrl("/", locale)`（zh → siteUrl，与旧 `absoluteUrl("/")` 同值）。
 *   - `hreflangLanguages("/")` 现含 zh-Hans + en + x-default（home 属 localizedRoutes）。
 */
export async function generateMetadata({
  params,
}: {
  params: Promise<{ locale: Locale }>
}): Promise<Metadata> {
  const { locale } = await params
  const seo = localeSeo[locale]
  return {
    alternates: {
      canonical: locale === "zh" ? "/" : absoluteUrl("/", locale),
      languages: hreflangLanguages("/"),
    },
    openGraph: {
      title: seo.defaultTitle,
      description: seo.defaultDescription,
      url: absoluteUrl("/", locale),
      locale: locale === "en" ? "en_US" : "zh_CN",
      type: "website",
    },
  }
}

/**
 * Homepage (`/`).
 *
 * Narrative arc (revised 2026-05-02 per ChatGPT consult round 3):
 *   [问题] Hero → PainPoints (4 concrete frustrations)
 *   [证据] FeaturedDemos (real dub clips, output-level evidence)
 *   [机制] ProductProof (workspace screenshots) → WorkflowShowcase → Features
 *   [对位] SuitedScenarios (是不是给我用的) → ToolComparison (vs 一键生成)
 *   [信任] TrustBanner → PricingPreview (with TrialBanner) → FAQ
 *   [行动] FinalCta
 *
 * FeaturedDemos moved 2026-05-02 from after ProductProof to right after
 * PainPoints. Rationale: the strongest objection a visitor has is "does
 * this actually sound natural?" — they need to *hear* a dubbed clip
 * before they care about workspace screenshots. Problem framing
 * (PainPoints) sets the question; output samples (FeaturedDemos) answer
 * it; mechanism evidence (ProductProof + WorkflowShowcase) shows how.
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
      <SiteJsonLd />
      <Hero />
      <PainPoints />
      <FeaturedDemos />
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
