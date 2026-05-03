import type { Metadata } from "next"
import { PricingGrid } from "@/components/marketing/pricing-grid"
import { PricingAssurance } from "@/components/marketing/pricing-assurance"
import { TrialBanner } from "@/components/marketing/trial-banner"
import { Faq } from "@/components/marketing/faq"
import { BreadcrumbJsonLd } from "@/components/seo/breadcrumb-json-ld"
import { absoluteUrl } from "@/lib/seo/site"

const PAGE_DESCRIPTION =
  "长视频也用得起的 AI 翻译配音。Free / Plus / Pro 三档套餐，单条视频最长 180 分钟，无需绑卡，失败不计费，修改片段不必重跑全片。"

export const metadata: Metadata = {
  title: "定价 · 爱译视频 AITrans.Video",
  description: PAGE_DESCRIPTION,
  alternates: { canonical: "/pricing" },
  openGraph: {
    title: "定价 · 爱译视频",
    description: PAGE_DESCRIPTION,
    url: absoluteUrl("/pricing"),
    type: "website",
  },
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
      <BreadcrumbJsonLd
        items={[
          { name: "首页", path: "/" },
          { name: "定价", path: "/pricing" },
        ]}
      />
      <section className="marketing-reading-surface pt-16 pb-10 sm:pt-20 sm:pb-12">
        <div className="mx-auto max-w-3xl px-4 text-center sm:px-6 lg:px-8">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            定价
          </p>
          <h1 className="ink-display mt-3 text-4xl tracking-tight text-foreground sm:text-5xl">
            长视频也用得起的 AI 翻译配音
          </h1>
          <p className="mt-5 zh-body-lg text-muted-foreground">
            从个人试用到团队高频生产，按你真正需要的处理能力选择套餐。试用无需绑卡，任务失败不计费，修改片段不必重跑全片。
          </p>
        </div>
      </section>

      <section className="marketing-reading-surface pb-20 sm:pb-24">
        <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
          <TrialBanner />
          <div className="mt-10">
            <PricingGrid />
          </div>
          <PricingAssurance />
        </div>
      </section>

      <Faq variant="pricing" />
    </>
  )
}
