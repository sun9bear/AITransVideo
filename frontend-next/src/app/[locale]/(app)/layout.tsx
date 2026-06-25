import { AppShell } from "@/components/app-shell"

/**
 * (app) route group layout — workspace / studio / admin / settings.
 *
 * Theme is chosen inside AppShell based on the `darkMode` state so that the
 * Sun/Moon toggle actually switches surfaces:
 *   darkMode = true  → data-theme="ink-dark" (warm dark gray + cinnabar + ochre + paper grain)
 *   darkMode = false → data-theme="ink"      (rice paper + cinnabar, same as marketing)
 *
 * Both variants share the same brand language and `--cinnabar` etc. tokens,
 * so per-page CSS works identically in either mode.
 */
export default function AppLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return <AppShell>{children}</AppShell>
}
