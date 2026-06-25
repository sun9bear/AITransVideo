"use client"

import { useTransition } from "react"
import { useLocale, useTranslations } from "next-intl"
import { useSearchParams } from "next/navigation"
import { useRouter, usePathname } from "@/i18n/navigation"
import { routing } from "@/i18n/routing"
import { cn } from "@/lib/utils"

/**
 * 界面语言切换器（UI page locale，UI-02 / 方案 §1.2 / 红线 8）。
 *
 * - **语言真源是 URL**：切换经 next-intl navigation 的 `router.replace(href, { locale })`，
 *   按 `localePrefix:'as-needed'` 产出正确路径（zh 裸路径 / en 带 `/en`）。
 * - next-intl 在显式 locale 切换时写 `NEXT_LOCALE` 偏好 cookie（`routing.localeCookie`）；
 *   但 `localeDetection:false` → 该 cookie **不驱动**裸 `/` 自动重定向（红线 8）。本组件
 *   只做开关本身，**不**实现自动恢复横幅（留给 UI-03）。
 * - 文案全部走 message key（`common.localeSwitcher.*`）：组件源码零内联 CJK（守 cjk-guard）。
 * - 主题中立：只用语义 token（border-border / text-muted-foreground / foreground），
 *   在营销 ink 主题与 app Synthetix 主题下都正确。
 */
const LABEL_KEY: Record<string, "labelZh" | "labelEn"> = { zh: "labelZh", en: "labelEn" }
const ARIA_KEY: Record<string, "ariaZh" | "ariaEn"> = { zh: "ariaZh", en: "ariaEn" }

export function LocaleSwitcher({ className }: { className?: string }) {
  const activeLocale = useLocale()
  const t = useTranslations("common.localeSwitcher")
  const router = useRouter()
  const pathname = usePathname()
  const searchParams = useSearchParams()
  const [isPending, startTransition] = useTransition()

  function switchTo(nextLocale: (typeof routing.locales)[number]) {
    if (nextLocale === activeLocale || isPending) return
    // usePathname (next-intl) 返回去 locale 前缀的路径；query 不在其内，显式保留。
    const query = searchParams.toString()
    const href = query ? `${pathname}?${query}` : pathname
    startTransition(() => {
      router.replace(href, { locale: nextLocale })
    })
  }

  return (
    <div
      role="group"
      aria-label={t("groupLabel")}
      className={cn(
        "inline-flex items-center rounded-[10px] border border-border p-0.5",
        isPending && "opacity-60",
        className,
      )}
    >
      {routing.locales.map((locale) => {
        const isActive = locale === activeLocale
        return (
          <button
            key={locale}
            type="button"
            onClick={() => switchTo(locale)}
            aria-pressed={isActive}
            aria-label={t(ARIA_KEY[locale])}
            disabled={isPending}
            className={cn(
              "inline-flex h-7 min-w-7 items-center justify-center rounded-[7px] px-1.5",
              "text-xs font-medium transition-colors",
              "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1",
              "focus-visible:outline-[color:var(--cinnabar,currentColor)]",
              isActive
                ? "bg-foreground/10 text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {t(LABEL_KEY[locale])}
          </button>
        )
      })}
    </div>
  )
}
