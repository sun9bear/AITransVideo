"use client"

import Link from "next/link"
import { buttonVariants } from "@/components/ui/button-variants"
import { cn } from "@/lib/utils"
import type { VariantProps } from "class-variance-authority"
import type { ComponentProps } from "react"

type LinkButtonProps = ComponentProps<typeof Link> &
  VariantProps<typeof buttonVariants> & {
    /** Additional tailwind class names appended after the variant classes. */
    className?: string
  }

/**
 * A `next/link` styled to look and behave like the shared UI button.
 *
 * Exists because this codebase's `Button` primitive is a base-ui client-only
 * component that cannot compose with `asChild` the way shadcn's Slot does.
 * Using `buttonVariants()` directly inside server components also fails (it
 * lives in a `"use client"` module). This tiny client wrapper lets server-side
 * marketing pages render button-styled anchors without duplicating class strings.
 *
 * Ink-aware override for `variant="outline"`:
 *   The shared outline variant carries `dark:bg-input/30 dark:hover:bg-input/50`
 *   rules. The site forces `html.dark` for the workspace's Synthetix theme, so
 *   even inside the marketing data-theme="ink" scope those `dark:` rules fire
 *   and collapse the button background to ~30% alpha. On the cream paper hero
 *   surface the result is "barely visible button outline + invisible text" —
 *   exactly the bug we fixed for the header 登录 button (commit 3862269).
 *
 *   The override below force-paints solid paper-2 background, ink-black text,
 *   and a cinnabar-tinted border so outline LinkButtons render correctly in
 *   both ink and ink-dark scopes regardless of html.dark state. Hover lifts
 *   to cinnabar-soft + cinnabar text.
 */
export function LinkButton({
  variant,
  size = "lg",
  className,
  children,
  ...props
}: LinkButtonProps) {
  const isOutlineInk = variant === "outline"
  return (
    <Link
      {...props}
      className={cn(
        buttonVariants({ variant, size }),
        "h-11 px-6",
        isOutlineInk && [
          "bg-[color:var(--ink-paper-2,#EDE6D6)]",
          "text-[color:var(--ink-black,#1A1A1A)]",
          "border border-[color:var(--cinnabar,#C73E3A)]/40",
          "hover:bg-[color:var(--cinnabar-soft,rgba(199,62,58,0.08))]",
          "hover:text-[color:var(--cinnabar,#C73E3A)]",
          "hover:border-[color:var(--cinnabar,#C73E3A)]/70",
        ],
        className,
      )}
    >
      {children}
    </Link>
  )
}
