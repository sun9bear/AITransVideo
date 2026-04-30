import { AppShell } from "@/components/app-shell"

/**
 * (app) route group layout — workspace / studio / admin / settings.
 *
 * Theme: ink-dark (warm dark gray + cinnabar + ochre + faint paper grain).
 * Activated via `data-theme="ink-dark"` so the entire (app) scope reads as
 * continuous with the marketing ink theme rather than the legacy steel-blue
 * .dark variant. See globals.css §[data-theme="ink-dark"] for token defs.
 *
 * The wrapper carries `bg-background text-foreground` so the resolved ink-dark
 * tokens paint the page surface immediately, before any AppShell internals
 * apply their own backgrounds. min-h-screen guarantees the warm-paper grain
 * pseudo-element fills the viewport even on short pages.
 */
export default function AppLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <div
      data-theme="ink-dark"
      className="min-h-screen bg-background text-foreground"
    >
      <AppShell>{children}</AppShell>
    </div>
  )
}
