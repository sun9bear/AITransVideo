import type { Metadata } from "next"
import { getTranslations } from "next-intl/server"
import { PricingGrid } from "@/components/marketing/pricing-grid"
import { PricingAssurance } from "@/components/marketing/pricing-assurance"
import { TrialBanner } from "@/components/marketing/trial-banner"
import { Faq } from "@/components/marketing/faq"
import { BreadcrumbJsonLd } from "@/components/seo/breadcrumb-json-ld"
import { absoluteUrl, hreflangLanguages, type Locale } from "@/lib/seo/site"

/**
 * `/pricing` metadata（UI-03d-1 翻旗）。title/description/OG 从 `seo` 字典取 localized 值，
 * canonical/hreflang 路由感知。
 *
 * **红线 R1（默认 zh 字节一致）**：zh 字典值逐字节复刻改造前内联字面量 ——
 * title "定价"、description 旧 PAGE_DESCRIPTION、ogTitle "定价 · 爱译视频"；canonical 保留相对
 * `"/pricing"`（en 才用绝对 `/en/pricing`）。title 仍为短串，root layout template 加品牌后缀。
 */
export async function generateMetadata({
  params,
}: {
  params: Promise<{ locale: Locale }>
}): Promise<Metadata> {
  const { locale } = await params
  const t = await getTranslations("seo.pricing")
  return {
    title: t("title"),
    description: t("description"),
    alternates: {
      canonical: locale === "zh" ? "/pricing" : absoluteUrl("/pricing", locale),
      languages: hreflangLanguages("/pricing"),
    },
    openGraph: {
      title: t("ogTitle"),
      description: t("description"),
      url: absoluteUrl("/pricing", locale),
      locale: locale === "en" ? "en_US" : "zh_CN",
      type: "website",
    },
  }
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
export default async function PricingPage({
  params,
}: {
  params: Promise<{ locale: Locale }>
}) {
  const { locale } = await params
  const t = await getTranslations("marketing.pricing")
  const tSeo = await getTranslations("seo")
  return (
    <>
      <BreadcrumbJsonLd
        locale={locale}
        items={[
          { name: tSeo("breadcrumb.home"), path: "/" },
          { name: tSeo("breadcrumb.pricing"), path: "/pricing" },
        ]}
      />
      <section className="marketing-reading-surface pt-16 pb-10 sm:pt-20 sm:pb-12">
        <div className="mx-auto max-w-3xl px-4 text-center sm:px-6 lg:px-8">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            {t("eyebrow")}
          </p>
          <h1 className="ink-display mt-3 text-4xl tracking-tight text-foreground sm:text-5xl">
            {t("heading")}
          </h1>
          <p className="mt-5 zh-body-lg text-muted-foreground">
            {t("lead")}
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
