"use client"

import Link from "next/link"
import { buttonVariants } from "@/components/ui/button"
import { useSession } from "@/components/providers/session-provider"
import { cn } from "@/lib/utils"
import type { Plan } from "@/lib/billing/types"

/**
 * Session-aware CTA for a single plan card.
 *
 * Extracted as a client component so the parent `PricingGrid` can stay a
 * Server Component (so prices land in the initial HTML for SEO / first-paint).
 * This is the only reactive surface inside the pricing card.
 *
 * Routing rules (mirrors the previous monolithic PricingGrid logic):
 *   - Guest: every tier → `/auth` (phone-first registration)
 *   - Logged-in: every tier → `/translations/new` (workspace), regardless of
 *     plan code. Upgrade flows are a later milestone (Task 4).
 */
function planCtaHref(
  plan: Plan,
  isAuthenticated: boolean,
): { href: string; label: string } {
  const authedHref = "/translations/new"
  const guestHref = "/auth"
  if (plan.code === "free") {
    return {
      href: isAuthenticated ? authedHref : guestHref,
      label: isAuthenticated ? "进入工作台" : "免费开始",
    }
  }
  if (plan.self_serve) {
    return {
      href: isAuthenticated ? authedHref : guestHref,
      label: isAuthenticated ? "进入工作台" : `选择 ${plan.display_name}`,
    }
  }
  return {
    href: isAuthenticated ? authedHref : guestHref,
    label: "联系我们",
  }
}

export function PlanCardCta({
  plan,
  highlight,
}: {
  plan: Plan
  highlight: boolean
}) {
  const { user } = useSession()
  const isAuthenticated = Boolean(user)
  const cta = planCtaHref(plan, isAuthenticated)

  return (
    <Link
      href={cta.href}
      className={cn(
        buttonVariants({ variant: highlight ? "default" : "outline", size: "lg" }),
        "h-11 w-full",
      )}
    >
      {cta.label}
    </Link>
  )
}
