"use client"

import Link from "next/link"
import { buttonVariants } from "@/components/ui/button"
import { useSession } from "@/components/providers/session-provider"
import { cn } from "@/lib/utils"

type PrimaryCtaProps = {
  /** When set, overrides the default guest/logged-in label switch. */
  guestLabel?: string
  loggedInLabel?: string
  className?: string
}

/**
 * Marketing primary CTA button with minimal session awareness.
 *
 * Default behavior:
 * - SSR / unauthenticated → "免费开始试用" → /auth (phone-first main path)
 * - Hydrated with active session → "进入工作台" → /translations/new
 *
 * We intentionally do NOT branch on plan / subscription / trial state here.
 * That belongs to later milestones (Task 4). See DESIGN.md §2.1.
 *
 * Task 3: guest CTA points at `/auth`, not `/auth/register`. Public email
 * registration has been closed; `/auth/register` is now a notice page that
 * redirects users to `/auth` anyway, so we skip the extra hop.
 *
 * Rendered as a styled <Link> (not a <Button>) because this codebase's base-ui
 * Button primitive does not support Slot-style asChild composition.
 */
export function PrimaryCta({
  guestLabel = "免费开始试用",
  loggedInLabel = "进入工作台",
  className,
}: PrimaryCtaProps) {
  const { user } = useSession()
  const isAuthenticated = Boolean(user)
  const href = isAuthenticated ? "/translations/new" : "/auth"
  const label = isAuthenticated ? loggedInLabel : guestLabel
  return (
    <Link
      href={href}
      className={cn(buttonVariants({ variant: "default", size: "lg" }), "h-11 px-6", className)}
    >
      {label}
    </Link>
  )
}
