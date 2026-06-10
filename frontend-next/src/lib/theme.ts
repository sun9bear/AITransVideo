/**
 * Workspace theme persistence, shared between AppShell (writer) and any
 * standalone page that must match the user's theme outside the (app) layout
 * scope — e.g. /paddle-checkout themes the Paddle overlay with it.
 */

export const WORKSPACE_THEME_STORAGE_KEY = "aivt-workspace-theme"

export type WorkspaceTheme = "light" | "dark"

export function readWorkspaceTheme(): WorkspaceTheme {
  if (typeof window === "undefined") return "light"
  try {
    return window.localStorage.getItem(WORKSPACE_THEME_STORAGE_KEY) === "dark"
      ? "dark"
      : "light"
  } catch {
    // localStorage unavailable (privacy mode etc.) — match AppShell's default.
    return "light"
  }
}
