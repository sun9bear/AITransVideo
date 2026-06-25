"use client"

import { Link } from "@/i18n/navigation"
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
 * Routing rules:
 *   - Guest: every tier → `/auth` (phone-first registration)
 *   - Logged-in free tier → `/translations/new` (workspace)
 *   - Logged-in self-serve paid tier → `/settings/billing?plan={code}` —
 *     CheckoutCard reads `?plan=` to preselect. Server-side create_order
 *     still owns the same/lower-tier guard.
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
      href: isAuthenticated
        ? `/settings/billing?plan=${encodeURIComponent(plan.code)}`
        : guestHref,
      label: isAuthenticated
        ? `升级到 ${plan.display_name}`
        : `选择 ${plan.display_name}`,
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
