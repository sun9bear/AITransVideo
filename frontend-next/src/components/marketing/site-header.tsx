"use client"

import { useEffect, useState } from "react"
import Link from "next/link"
import { usePathname } from "next/navigation"
import { useSession } from "@/components/providers/session-provider"
import { BrandLockup } from "./brand-mark"
import { buttonVariants } from "@/components/ui/button-variants"
import { cn } from "@/lib/utils"

const NAV_ITEMS: Array<{ href: string; label: string }> = [
  { href: "/", label: "首页" },
  { href: "/pricing", label: "定价" },
  { href: "/trial", label: "免费试用" },
]

/**
 * Marketing-layer header. Sticky + scroll-aware CTA highlight.
 *
 * See: docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md §5.2 第四幕「行动」
 *   "新增 sticky 导航 CTA：滚动超过 hero 后，顶部'免费开始试用'按钮变高亮态 + 微动效"
 *
 * Behavior:
 *   - On non-home pages (`/pricing`, `/trial`, legal): CTA is highlighted from
 *     first paint — the visitor is already past the conversion-attention boundary.
 *   - On home (`/`): CTA stays in default state until the user scrolls beyond
 *     ~50vh (i.e. past the hero). After that point, a faint cinnabar ring and
 *     elevation kicks in so the CTA reads as "ready when you are".
 *   - Respects prefers-reduced-motion implicitly: the only animation is a CSS
 *     transition (which the global rule in globals.css clamps to 0.01ms when
 *     reduced motion is preferred). No keyframes, no infinite pulses.
 */
export function SiteHeader() {
  const pathname = usePathname()
  const { user } = useSession()

  const isHome = pathname === "/"
  // Derived state pattern (avoids `react-hooks/set-state-in-effect` rule):
  // `pastHero` is computed each render from `isHome` + scroll progress, so we
  // never call setState synchronously inside an effect body. The effect only
  // wires up the scroll listener and writes through the listener callback.
  const [scrolledPast, setScrolledPast] = useState(false)
  const pastHero = !isHome || scrolledPast

  useEffect(() => {
    // Non-home pages: pastHero is true via derived state; no listener required.
    if (!isHome) return

    // Threshold = min(400px, 50vh). Captures both short and tall hero sizes
    // without coupling to the hero component's specific markup.
    const compute = () => {
      const threshold = Math.min(400, window.innerHeight * 0.5)
      setScrolledPast(window.scrollY > threshold)
    }
    compute()
    window.addEventListener("scroll", compute, { passive: true })
    window.addEventListener("resize", compute)
    return () => {
      window.removeEventListener("scroll", compute)
      window.removeEventListener("resize", compute)
    }
  }, [isHome])

  return (
    <header className="sticky top-0 z-40 w-full border-b border-border/70 bg-background/85 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6 lg:px-8">
        <Link href="/" className="flex items-center" aria-label="AIVideoTrans 首页">
          <BrandLockup />
        </Link>

        <nav className="hidden items-center gap-1 md:flex" aria-label="主导航">
          {NAV_ITEMS.map((item) => {
            const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href)
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  active
                    ? "text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {item.label}
              </Link>
            )
          })}
        </nav>

        <div className="flex items-center gap-2">
          {user ? (
            <Link
              href="/translations/new"
              className={cn(buttonVariants({ variant: "outline", size: "sm" }), "h-8 px-3")}
            >
              进入工作台
            </Link>
          ) : (
            <>
              <Link
                href="/auth/login"
                className={cn(
                  buttonVariants({ variant: "ghost", size: "sm" }),
                  "hidden h-8 px-3 sm:inline-flex",
                )}
              >
                登录
              </Link>
              <Link
                href="/auth"
                data-attention={pastHero ? "true" : undefined}
                className={cn(
                  buttonVariants({ variant: "default", size: "sm" }),
                  "h-8 px-3 transition-all duration-300",
                  // Highlighted state: cinnabar ring halo + subtle elevation. The
                  // ring uses the design token directly so it tracks any future
                  // primary-color changes automatically.
                  pastHero
                    ? "ring-2 ring-[color:var(--cinnabar,#C73E3A)]/35 ring-offset-2 ring-offset-background shadow-md scale-[1.02]"
                    : "shadow-none",
                )}
              >
                免费开始试用
              </Link>
            </>
          )}
        </div>
      </div>
    </header>
  )
}
