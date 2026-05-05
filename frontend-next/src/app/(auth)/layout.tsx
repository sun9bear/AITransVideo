import type { Metadata } from "next"

/**
 * Auth route group layout — ink-themed (rice paper + cinnabar) so /auth,
 * /auth/login, /auth/register, /auth/forgot-password feel continuous with
 * the marketing surface that visitors arrive from.
 *
 * Activated via `data-theme="ink"` so the entire scope picks up the marketing
 * tokens. Any shadcn Form / Input / Button primitives below this layer
 * automatically resolve --background, --card, --primary, etc. through the
 * ink theme (see globals.css §[data-theme="ink"]). No per-form rewrites
 * required.
 *
 * The layout also paints a subtle paper-noise backdrop (faint cinnabar wash
 * top + radial vignette) so the form card sits over a textured ground rather
 * than a flat color — extending the marketing aesthetic into the conversion
 * surface where most decisions are made.
 *
 * SEO: noindex covers the whole route group. The three child pages
 * (login / register / forgot-password) all start with `"use client"` and
 * therefore can't `export const metadata` themselves — declaring it on this
 * server-component layout propagates `robots` to all children. See
 * docs/plans/2026-05-03-geo-optimization-plan.md §7.2.
 */
export const metadata: Metadata = {
  robots: { index: false, follow: false },
}

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <div
      data-theme="ink"
      className="relative flex min-h-dvh items-start justify-center overflow-x-hidden overflow-y-auto bg-background px-3 py-6 sm:min-h-screen sm:items-center sm:px-4 sm:py-10"
    >
      {/* Faint cinnabar wash from top — visually echoes the hero/final-cta blocks */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-48"
        style={{
          background:
            "linear-gradient(180deg, color-mix(in oklab, var(--cinnabar) 5%, transparent) 0%, transparent 100%)",
        }}
      />
      {/* Soft rice-paper vignette so the form card sits over textured ground */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(ellipse 60% 50% at 50% 30%, color-mix(in oklab, var(--ink-paper-2) 40%, transparent) 0%, transparent 70%)",
        }}
      />
      {/* Mobile browsers expose short dynamic viewports while their address
          and toolbar chrome are visible. Keep auth pages scrollable and avoid
          double horizontal padding so fixed-width captcha iframes do not
          overflow the form card. */}
      <div className="relative z-10 w-full max-w-md">{children}</div>
    </div>
  )
}
