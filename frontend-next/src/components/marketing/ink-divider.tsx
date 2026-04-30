import { cn } from "@/lib/utils"

/**
 * Horizontal ink-stroke divider with feibai (飞白) — the broken-brush effect of a
 * Chinese calligraphic horizontal stroke that's running out of ink.
 *
 * Used between marketing sections to replace the default <hr> / linear-gradient
 * divider, giving the page a long-scroll "ink scroll" reading rhythm.
 *
 * Design rules:
 *   - Single brush stroke per render — do not stack.
 *   - Length is responsive (uses `width: 100%`); aspect ratio fixed via viewBox.
 *   - Color uses ink-gray-2 by default; can be tinted with cinnabar for emphasis,
 *     but cinnabar dividers should appear at most ONCE per page (sparing accent).
 *   - Three randomized variants prevent visual repetition; pass `seed` to lock a
 *     specific look in one place across renders.
 */
export interface InkDividerProps {
  /** Variant index 0..2 — picks one of three pre-baked stroke shapes. */
  variant?: 0 | 1 | 2
  /** Stroke color. Defaults to ink gray. */
  color?: string
  /** Extra Tailwind classes for outer container (margins, opacity, etc.). */
  className?: string
  /** Optional aria-label; default is empty (decorative). */
  ariaLabel?: string
}

// Three pre-tuned stroke paths. Each path is one continuous brush line at
// approximately y=10 in a 200×20 viewBox. Path control points create the
// natural taper + feibai breaks. Hand-tuned, not random, so they stay elegant.
const STROKES: readonly string[] = [
  // Variant 0: gentle left-to-right with mid-stroke break
  "M 4 11 C 30 8, 60 12, 92 9 L 96 10 M 104 11 C 130 8, 160 12, 196 10",
  // Variant 1: wider taper, rightward emphasis
  "M 6 12 C 40 9, 80 11, 120 10 C 150 9, 180 12, 196 10",
  // Variant 2: fragmented feibai with three breaks
  "M 4 10 C 20 9, 36 11, 52 10 M 60 11 C 80 9, 100 12, 120 10 M 130 11 C 160 9, 184 12, 196 10",
]

// Companion dot patterns — small ink droplets scattered along the stroke to
// reinforce the feibai look. Each dot list aligns with its stroke variant.
const DOTS: ReadonlyArray<ReadonlyArray<{ cx: number; cy: number; r: number }>> = [
  [{ cx: 99, cy: 11, r: 0.6 }],
  [{ cx: 145, cy: 11, r: 0.5 }, { cx: 78, cy: 10, r: 0.4 }],
  [{ cx: 56, cy: 10, r: 0.5 }, { cx: 124, cy: 10, r: 0.5 }, { cx: 192, cy: 10, r: 0.4 }],
]

export function InkDivider({
  variant = 0,
  color = "currentColor",
  className,
  ariaLabel,
}: InkDividerProps) {
  const v = Math.max(0, Math.min(2, variant)) as 0 | 1 | 2
  const stroke = STROKES[v]
  const dots = DOTS[v]

  return (
    <div className={cn("w-full", className)}>
      <svg
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 200 20"
        preserveAspectRatio="none"
        width="100%"
        height="20"
        aria-hidden={ariaLabel ? undefined : "true"}
        aria-label={ariaLabel}
        role={ariaLabel ? "img" : undefined}
        style={{ display: "block", color }}
      >
        <path
          d={stroke}
          fill="none"
          stroke={color}
          strokeWidth="1.4"
          strokeLinecap="round"
          opacity="0.55"
        />
        {dots.map((d, i) => (
          <circle key={i} cx={d.cx} cy={d.cy} r={d.r} fill={color} opacity="0.45" />
        ))}
      </svg>
    </div>
  )
}
