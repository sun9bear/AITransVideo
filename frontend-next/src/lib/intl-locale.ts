// Active-locale mapping for `Intl` date/number formatters (UI-07).
//
// The UI page locale (`next-intl`) is "zh" (default) or "en". `Intl`
// formatters want a BCP-47 tag, so map:
//   zh -> "zh-CN"  (keeps every existing zh render BYTE-IDENTICAL — red line 1)
//   en -> "en-US"
// Anything unexpected falls back to "zh-CN" (the byte-identical default), so a
// future locale can never silently change today's zh output.
//
// Currency *values* are never converted here — only the formatter locale
// (grouping/date order) follows the UI language (plan §1.6 / UI-07).

import { useLocale } from "next-intl"

/** Map a next-intl UI locale to the BCP-47 tag used by `Intl` formatters. */
export function intlLocale(locale: string): string {
  return locale === "en" ? "en-US" : "zh-CN"
}

/**
 * Client hook: the active BCP-47 formatter locale.
 * Call at component top level, then pass the result into `toLocale*` /
 * `Intl.*Format` so the formatter follows the UI language.
 */
export function useIntlLocale(): string {
  return intlLocale(useLocale())
}
