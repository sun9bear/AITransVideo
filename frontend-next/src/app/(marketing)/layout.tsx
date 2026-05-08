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
export default function MarketingLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <div
      data-theme="ink"
      className="flex min-h-screen flex-col bg-background"
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
