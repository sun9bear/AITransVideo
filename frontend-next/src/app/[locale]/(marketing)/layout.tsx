import { EB_Garamond } from "next/font/google"
import { getLocale } from "next-intl/server"
import { SiteHeader } from "@/components/marketing/site-header"
import { SiteFooter } from "@/components/marketing/site-footer"
import { SupportWidget } from "@/components/support/SupportWidget"

/**
 * Marketing route group layout — header, footer, `<main>` landmark.
 *
 * Theme: ink (rice paper + cinnabar). Activated by `data-theme="ink"` so the
 * scope is *only* the marketing surface (homepage, pricing, trial, legal).
 * The (app) and (auth) route groups are unaffected and continue with the
 * default cool-toned theme.
 *
 * See: docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md §3 layering
 *
 * SupportWidget (plan 2026-05-08 §8.2) is mounted here so visitors can ask
 * pre-sales questions without leaving the page. It self-disables via
 * /api/support/config when the admin kill switch is off.
 */
/* East-West heading pair (plan 2026-06-11 Task 6): weight 600 for .ink-heading.
   weight 800 added by UI-03d-2 for the EN display track — on /en, .ink-display
   (CJK Noto Serif SC 900) faux-bolds Georgia for Latin; the `.locale-en
   .ink-display` rule in globals.css uses EB Garamond at 800 (its max — 900 would
   faux-bold, per the original note) so EN headings get a real display serif.
   Garamond is scoped to .marketing-root; zh display still uses Noto Serif SC. */
const ebGaramond = EB_Garamond({
  subsets: ["latin"],
  weight: ["600", "800"],
  variable: "--font-eb-garamond",
  display: "swap",
})

export default async function MarketingLayout({
  children,
}: {
  children: React.ReactNode
}) {
  // UI page locale → EN typography track. `locale-en` is appended ONLY for en
  // (with a leading space) so the default-zh class string is byte-identical to
  // before (no trailing/double space) — red line R1.
  const locale = await getLocale()
  return (
    <div
      data-theme="ink"
      className={`${ebGaramond.variable} marketing-root${locale === "en" ? " locale-en" : ""} flex min-h-screen flex-col bg-background`}
    >
      <SiteHeader />
      <main id="main-content" className="flex-1">
        {children}
      </main>
      <SiteFooter />
      <SupportWidget />
    </div>
  )
}
