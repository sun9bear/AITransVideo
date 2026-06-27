import type { Metadata } from "next"
import { getTranslations } from "next-intl/server"
import { ShieldCheck, ArrowRight } from "lucide-react"
import { TrialDetails } from "@/components/marketing/trial-details"
import { PrimaryCta } from "@/components/marketing/primary-cta"
import { LinkButton } from "@/components/marketing/link-button"
import { getPlansSafeServer } from "@/lib/billing/get-plans"
import { BreadcrumbJsonLd } from "@/components/seo/breadcrumb-json-ld"
import { absoluteUrl, hreflangLanguages, type Locale } from "@/lib/seo/site"

/**
 * `/trial` metadata（UI-03d-1 翻旗）。title/description/OG 从 `seo` 字典取 localized 值，
 * canonical/hreflang 路由感知。
 *
 * **红线 R1（默认 zh 字节一致）**：zh 字典值逐字节复刻改造前内联字面量 ——
 * title "免费试用"、description 旧 PAGE_DESCRIPTION、ogTitle "免费试用 · 爱译视频"；canonical
 * 保留相对 `"/trial"`（en 才用绝对 `/en/trial`）。title 仍为短串，root layout template 加后缀。
 */
export async function generateMetadata({
  params,
}: {
  params: Promise<{ locale: Locale }>
}): Promise<Metadata> {
  const { locale } = await params
  const t = await getTranslations("seo.trial")
  return {
    title: t("title"),
    description: t("description"),
    alternates: {
      canonical: locale === "zh" ? "/trial" : absoluteUrl("/trial", locale),
      languages: hreflangLanguages("/trial"),
    },
    openGraph: {
      title: t("ogTitle"),
      description: t("description"),
      url: absoluteUrl("/trial", locale),
      locale: locale === "en" ? "en_US" : "zh_CN",
      type: "website",
    },
  }
}

/**
 * `/trial` — marketing landing page for trial conversion.
 *
 * IMPORTANT: this is NOT the Task 3 phone/SMS auth page. It is a marketing
 * surface whose only job is to explain trial value, establish trust, and hand
 * the visitor off to the existing `/auth/register` flow.
 *
 * Numeric trial facts (days / source_minutes / Studio inclusion) come from the
 * gateway `GET /api/plans` response and are only rendered when frozen === true.
 * The lead paragraph below was previously hardcoded — fixed during the
 * 2026-04-29 marketing redesign so trial copy stays in sync with policy.
 *
 * See: docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md §1.1 issue 4
 */
export default async function TrialPage({
  params,
}: {
  params: Promise<{ locale: Locale }>
}) {
  const { locale } = await params
  const t = await getTranslations("marketing.trial")
  const tSeo = await getTranslations("seo")
  const data = await getPlansSafeServer()
  const trial = data.trial
  const hasNumbers = Boolean(
    trial &&
      trial.frozen &&
      typeof trial.days === "number" &&
      typeof trial.source_minutes === "number",
  )
  const leadParagraph = hasNumbers
    ? t("leadWithNumbers", {
        days: trial!.days!,
        minutes: trial!.source_minutes!,
        studio: trial!.includes_studio ? t("studioSuffix") : "",
      })
    : t("leadFallback")

  return (
    <>
      <BreadcrumbJsonLd
        locale={locale}
        items={[
          { name: tSeo("breadcrumb.home"), path: "/" },
          { name: tSeo("breadcrumb.trial"), path: "/trial" },
        ]}
      />
      <section className="marketing-reading-surface pt-16 pb-8 sm:pt-20">
        <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
          <div className="max-w-2xl">
            <div className="inline-flex items-center gap-2 rounded-full border border-[color:var(--cinnabar,#C73E3A)]/30 bg-[color:var(--cinnabar,#C73E3A)]/5 px-3 py-1 text-xs font-semibold text-[color:var(--cinnabar,#C73E3A)]">
              <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
              {t("badge")}
            </div>
            <h1 className="ink-display mt-5 text-4xl tracking-tight text-foreground sm:text-5xl">
              {t("heading")}
            </h1>
            <p className="mt-5 zh-body-lg text-muted-foreground">{leadParagraph}</p>
          </div>
        </div>
      </section>

      <section className="marketing-reading-surface pb-20 sm:pb-24">
        <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
          <div className="grid gap-10 lg:grid-cols-5 lg:gap-16">
            <div className="lg:col-span-3">
              <TrialDetails />
            </div>

            <aside className="lg:col-span-2" aria-label={t("asideLabel")}>
              <div className="sticky top-24 rounded-2xl border border-border bg-card p-6 shadow-sm">
                <h2 className="ink-heading text-xl font-semibold text-foreground">{t("asideTitle")}</h2>
                <p className="mt-2 text-sm text-muted-foreground">
                  {t("asideDescription")}
                </p>
                <div className="mt-6 space-y-3">
                  <PrimaryCta className="w-full" />
                  <LinkButton href="/pricing" variant="outline" className="w-full gap-1.5">
                    {t("asidePricingLink")}
                    <ArrowRight className="h-4 w-4" aria-hidden="true" />
                  </LinkButton>
                </div>
                <div className="mt-6 space-y-2 border-t border-border pt-5 text-xs text-muted-foreground">
                  <p>{t("asideNote1")}</p>
                  <p>{t("asideNote2")}</p>
                  <p>{t("asideNote3")}</p>
                </div>
              </div>
            </aside>
          </div>
        </div>
      </section>
    </>
  )
}
