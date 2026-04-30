import { cn } from "@/lib/utils"
import { SealStamp } from "./seal-stamp"

/**
 * Brand mark for the marketing layer.
 *
 * Renders the cinnabar seal stamp (朱砂方印) with the engraved character「译」.
 * This replaces the previous "AV" letter block — see plan §8 D15.
 *
 * The seal is the single source of truth for brand visual identity in the
 * marketing layer; site header, footer, and any other lockup composition
 * should call into this module rather than re-implementing the mark.
 */
export function BrandMark({ className, size = 32 }: { className?: string; size?: number }) {
  return (
    <span className={cn("inline-flex shrink-0 items-center justify-center", className)}>
      <SealStamp size={size} ariaLabel="AIVideoTrans" />
    </span>
  )
}

export function BrandLockup({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center gap-2.5", className)}>
      <BrandMark />
      <span className="ink-heading font-semibold text-foreground tracking-tight">
        AIVideoTrans
      </span>
    </div>
  )
}
