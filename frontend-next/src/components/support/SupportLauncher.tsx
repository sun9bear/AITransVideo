"use client"

import { useTranslations } from "next-intl"

/**
 * Floating button that opens the SupportWidget panel.
 *
 * Stays in the bottom-right at desktop and shifts up slightly on mobile
 * so it does not overlap CTAs that already live at the page foot. The
 * component is dumb — open / close state lives in SupportWidget.
 */
export function SupportLauncher({
  open,
  onToggle,
  unreadHint,
}: {
  open: boolean
  onToggle: () => void
  unreadHint?: boolean
}) {
  const t = useTranslations("appSupport")
  return (
    <button
      type="button"
      aria-label={t("labels.launcherTooltip")}
      title={t("labels.launcherTooltip")}
      onClick={onToggle}
      className={
        "fixed z-50 bottom-4 right-4 sm:bottom-6 sm:right-6 " +
        "flex h-12 w-12 items-center justify-center rounded-full " +
        "bg-[color:var(--cinnabar,#C73E3A)] text-white shadow-lg " +
        "transition-transform hover:scale-105 active:scale-95 " +
        (open ? "rotate-45" : "")
      }
    >
      {open ? (
        <span className="text-2xl leading-none" aria-hidden>
          ×
        </span>
      ) : (
        <span className="relative">
          <span aria-hidden className="text-lg">
            💬
          </span>
          {unreadHint ? (
            <span
              aria-hidden
              className="absolute -right-1 -top-1 h-2.5 w-2.5 rounded-full bg-yellow-300 ring-2 ring-white"
            />
          ) : null}
        </span>
      )}
      <span className="sr-only">{t("labels.launcherLabel")}</span>
    </button>
  )
}
