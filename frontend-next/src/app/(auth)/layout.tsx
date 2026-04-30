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
 */
export default function AuthLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <div
      data-theme="ink"
      className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background"
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
      <div className="relative z-10 w-full">{children}</div>
    </div>
  )
}
