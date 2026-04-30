import { cn } from "@/lib/utils"

/**
 * Placeholder slot for product screenshots that the user will provide later.
 *
 * Per docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md §6.3 — the
 * marketing rewrite must not block on screenshot delivery. Components like
 * ProductProof and WorkflowShowcase render this placeholder by default so the
 * page structure ships first; users replace `<ScreenshotPlaceholder ...>` with
 * `<Image src="/marketing/screenshots/X.webp" ...>` once real assets land.
 *
 * Design intent:
 *   - Looks intentional, not broken: cinnabar dashed border + ink-gray fill +
 *     centered Chinese label that names the screenshot semantically.
 *   - Aspect ratio is parameterized (default 16:10) so adjacent placeholders
 *     align cleanly in grids.
 *   - No real <img>; purely CSS so the placeholder renders identically on
 *     server and client (no hydration flicker).
 *
 * NEVER ship to production with placeholders visible — a CI guard / human
 * checklist enforces this in plan §11.2.
 */
export interface ScreenshotPlaceholderProps {
  /** Short Chinese label naming the screenshot semantically. e.g. "新建任务页". */
  label: string
  /** Optional secondary hint (resolution / aspect / variant). */
  hint?: string
  /** Aspect ratio CSS string. Defaults to "16 / 10". */
  aspectRatio?: string
  /** Extra Tailwind classes (typically rounding / shadow). */
  className?: string
}

export function ScreenshotPlaceholder({
  label,
  hint,
  aspectRatio = "16 / 10",
  className,
}: ScreenshotPlaceholderProps) {
  return (
    <div
      className={cn(
        // Subtle paper-tinted fill, dashed cinnabar outline so it visibly says
        // "this is intentionally a slot, not a broken image".
        "relative w-full overflow-hidden rounded-lg",
        "border-2 border-dashed",
        "bg-[color:var(--ink-paper-2,#EDE6D6)]",
        "border-[color:var(--cinnabar,#C73E3A)]",
        className,
      )}
      style={{ aspectRatio }}
      role="img"
      aria-label={`产品截图占位：${label}`}
    >
      {/* Diagonal hatch pattern, faint, for "drafted but not painted yet" feel. */}
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "repeating-linear-gradient(45deg, transparent 0 14px, rgba(199, 62, 58, 0.05) 14px 16px)",
        }}
        aria-hidden="true"
      />

      <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 p-4 text-center">
        <span
          className="text-base font-semibold tracking-wide"
          style={{ color: "var(--ink-black, #1A1A1A)" }}
        >
          {label}
        </span>
        <span
          className="text-xs"
          style={{ color: "var(--ink-gray-2, #4A4A4A)" }}
        >
          产品截图位 · 待补图
          {hint ? ` · ${hint}` : ""}
        </span>
      </div>
    </div>
  )
}
