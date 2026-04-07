"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import { useSession } from "@/components/providers/session-provider"
import { BrandLockup } from "./brand-mark"
import { buttonVariants } from "@/components/ui/button"
import { cn } from "@/lib/utils"

const NAV_ITEMS: Array<{ href: string; label: string }> = [
  { href: "/", label: "首页" },
  { href: "/pricing", label: "定价" },
  { href: "/trial", label: "免费试用" },
]

/**
 * Marketing-layer header. Kept lightweight and contrast-led.
 *
 * DESIGN.md §3.1 — marketing pages are dark-capable but not universally dark;
 * the header sits on the current page background without injecting its own drama.
 */
export function SiteHeader() {
  const pathname = usePathname()
  const { user } = useSession()

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
                className={cn(buttonVariants({ variant: "default", size: "sm" }), "h-8 px-3")}
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
