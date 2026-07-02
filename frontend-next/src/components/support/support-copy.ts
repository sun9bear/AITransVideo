/**
 * Path → entrypoint classifier used by SupportWidget.
 *
 * Display copy (fallback greeting/quick-questions/labels) used to live here
 * as plain string constants; CM-04 migrated it to the `appSupport` next-intl
 * namespace (`messages/{zh,en}/appSupport.json`) so it can be localized.
 * Consumer components now call `useTranslations("appSupport")` directly
 * instead of importing string constants from this module. See
 * `SupportWidget.tsx` / `SupportConversationPanel.tsx` / `SupportLauncher.tsx`
 * / `SupportHandoffBanner.tsx`.
 *
 * The greeting + quick-question seeds still come from the gateway via
 * /api/support/config; the `appSupport.fallback.*` keys are only the
 * fallback values used during the initial render (before the config fetch
 * resolves). Note: the server-emitted config content itself (gateway
 * `/api/support/config` payload) is out of scope for this unit — see
 * UI-BE-01 (backend error-code / localization tracked separately).
 */

export const ENTRYPOINT_FROM_PATH = (
  path: string | null | undefined,
):
  | "marketing_home"
  | "pricing"
  | "trial"
  | "contact"
  | "workspace"
  | "task_detail"
  | "billing"
  | "help"
  | "faq"
  | "notification"
  | "auth"
  | "unknown" => {
  if (!path) return "unknown"
  if (path === "/" || path === "") return "marketing_home"
  if (path.startsWith("/pricing")) return "pricing"
  if (path.startsWith("/trial")) return "trial"
  if (path.startsWith("/contact")) return "contact"
  if (path.startsWith("/help")) return "help"
  if (path.startsWith("/account/billing")) return "billing"
  if (path.startsWith("/notifications")) return "notification"
  if (path.startsWith("/workspace/")) return "task_detail"
  if (path.startsWith("/workspace")) return "workspace"
  if (path.startsWith("/login") || path.startsWith("/register"))
    return "auth"
  if (path.startsWith("/#faq") || path.includes("#faq")) return "faq"
  return "unknown"
}
