import { cn } from "@/lib/utils"

/**
 * Minimal brand mark for the marketing layer. No gradients, no AI-template purple.
 * DESIGN.md §2.2 — "deep blue / steel cyan / signal teal" and no loud neon gradients.
 *
 * Uses the foundation primary token so a future brand update only needs to touch
 * globals.css, not every surface that happens to render the logo.
 */
export function BrandMark({ className, size = 28 }: { className?: string; size?: number }) {
  return (
    <div
      className={cn(
        "flex items-center justify-center rounded-md bg-primary text-primary-foreground font-semibold shrink-0",
        className,
      )}
      style={{ width: size, height: size, fontSize: Math.round(size * 0.46) }}
      aria-hidden="true"
    >
      AV
    </div>
  )
}

export function BrandLockup({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center gap-2.5", className)}>
      <BrandMark />
      <span className="font-semibold text-foreground tracking-tight">AIVideoTrans</span>
    </div>
  )
}
