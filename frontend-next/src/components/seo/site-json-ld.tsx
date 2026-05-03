import { COMPANY_NAME, SUPPORT_EMAIL } from "@/components/marketing/company-info"
import {
  absoluteUrl,
  brandNames,
  defaultDescription,
  siteName,
  siteUrl,
} from "@/lib/seo/site"
import { JsonLd } from "./json-ld"

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
export function SiteJsonLd() {
  const alternateName = brandNames.filter((n) => n !== siteName)

  const organization = {
    "@context": "https://schema.org",
    "@type": "Organization",
    "@id": `${siteUrl}#organization`,
    name: siteName,
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
        availableLanguage: ["zh-CN"],
      },
    ],
  }

  const website = {
    "@context": "https://schema.org",
    "@type": "WebSite",
    "@id": `${siteUrl}#website`,
    name: siteName,
    alternateName,
    url: siteUrl,
    inLanguage: "zh-CN",
    publisher: { "@id": `${siteUrl}#organization` },
  }

  const application = {
    "@context": "https://schema.org",
    "@type": "WebApplication",
    "@id": `${siteUrl}#webapp`,
    name: siteName,
    alternateName,
    url: siteUrl,
    applicationCategory: "MultimediaApplication",
    operatingSystem: "Web",
    inLanguage: "zh-CN",
    description: defaultDescription,
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
