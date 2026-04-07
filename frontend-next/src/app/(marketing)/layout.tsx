import { SiteHeader } from "@/components/marketing/site-header"
import { SiteFooter } from "@/components/marketing/site-footer"

/**
 * Marketing route group layout — provides header + footer for the homepage,
 * pricing, and trial pages. DESIGN.md §3 applies strongly here.
 *
 * This layout is deliberately thin: header, footer, and a `<main>` landmark.
 * All visual expression lives inside section components under
 * `src/components/marketing/*` so the foundations stay shareable with (app).
 */
export default function MarketingLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <div className="flex min-h-screen flex-col bg-background">
      <SiteHeader />
      <main id="main-content" className="flex-1">
        {children}
      </main>
      <SiteFooter />
    </div>
  )
}
