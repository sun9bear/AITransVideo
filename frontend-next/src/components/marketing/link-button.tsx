"use client"

import Link from "next/link"
import { buttonVariants } from "@/components/ui/button"
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
 */
export function LinkButton({
  variant,
  size = "lg",
  className,
  children,
  ...props
}: LinkButtonProps) {
  return (
    <Link
      {...props}
      className={cn(buttonVariants({ variant, size }), "h-11 px-6", className)}
    >
      {children}
    </Link>
  )
}
