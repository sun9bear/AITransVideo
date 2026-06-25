import { getLocale } from "next-intl/server"
import { COMPANY_NAME, SUPPORT_EMAIL } from "@/components/marketing/company-info"
import {
  absoluteUrl,
  brandNames,
  localeSeo,
  siteUrl,
  type Locale,
} from "@/lib/seo/site"
import { JsonLd } from "./json-ld"

/** schema.org BCP-47 language tags per UI page locale. */
const IN_LANGUAGE: Record<Locale, string> = { zh: "zh-CN", en: "en-US" }

/**
 * Site-level Organization + WebSite + SoftwareApplication structured data.
 *
 * Mount on the homepage only. Google guidelines: site-wide entity schema
 * (Organization, WebSite) appears once at the canonical root; repeating
 * on every page provides no extra signal and can dilute attribution.
 *
 * Truth sources (per docs/plans/2026-05-03-geo-optimization-plan.md §7.5):
 *   - `name` / `alternateName`     → lib/seo/site.ts (siteName + brandNames)
 *   - `legalName` / `email`        → components/marketing/company-info.ts
 *     (the same constants /terms, /privacy, /refund, /contact already use,
 *      so payment-partner audit reads one consistent legal identity)
 *
 * NOT INCLUDED (would require frozen-true gating):
 *   - `Offer` — see plan §6.1; gated on `plan.price_cny_fen.monthly` for
 *     plan-level and `trial.frozen === true` for trial-level. Can be added
 *     later as a separate component in this directory once business decides
 *     pricing is stable enough to surface in rich results.
 *   - `aggregateRating` / `Review` — no real verifiable reviews yet
 *   - `FAQPage` — visible FAQ text suffices; schema only adds value when
 *     a specific search term wins rich-result placement (TBD).
 */
export async function SiteJsonLd() {
  // Locale-driven structural schema (UI-03a). `getLocale()` resolves the active
  // UI page locale from the [locale] segment. zh output stays byte-identical to
  // the pre-migration hardcoded values (红线 1); en pages emit en-US + bilingual
  // availableLanguage now that English support is real.
  const locale = (await getLocale()) as Locale
  const inLanguage = IN_LANGUAGE[locale] ?? IN_LANGUAGE.zh
  const seo = localeSeo[locale] ?? localeSeo.zh
  // contactPoint availableLanguage reflects actual support: zh always; en added
  // only on the en surface so the default zh JSON-LD stays byte-identical.
  const availableLanguage = locale === "en" ? ["zh-CN", "en-US"] : ["zh-CN"]

  const alternateName = brandNames.filter((n) => n !== seo.siteName)

  const organization = {
    "@context": "https://schema.org",
    "@type": "Organization",
    "@id": `${siteUrl}#organization`,
    name: seo.siteName,
    alternateName,
    legalName: COMPANY_NAME,
    url: siteUrl,
    logo: absoluteUrl("/marketing/seal-yi.png"),
    email: SUPPORT_EMAIL,
    contactPoint: [
      {
        "@type": "ContactPoint",
        contactType: "customer support",
        email: SUPPORT_EMAIL,
        availableLanguage,
      },
    ],
  }

  const website = {
    "@context": "https://schema.org",
    "@type": "WebSite",
    "@id": `${siteUrl}#website`,
    name: seo.siteName,
    alternateName,
    url: siteUrl,
    inLanguage,
    publisher: { "@id": `${siteUrl}#organization` },
  }

  const application = {
    "@context": "https://schema.org",
    "@type": "WebApplication",
    "@id": `${siteUrl}#webapp`,
    name: seo.siteName,
    alternateName,
    url: siteUrl,
    applicationCategory: "MultimediaApplication",
    operatingSystem: "Web",
    inLanguage,
    description: seo.defaultDescription,
    publisher: { "@id": `${siteUrl}#organization` },
  }

  return (
    <>
      <JsonLd id="ld-organization" data={organization} />
      <JsonLd id="ld-website" data={website} />
      <JsonLd id="ld-webapp" data={application} />
    </>
  )
}
