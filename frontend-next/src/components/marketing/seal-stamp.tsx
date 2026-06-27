"use client"

import { useTranslations } from "next-intl"
import { cn } from "@/lib/utils"

/**
 * Cinnabar seal stamp (朱砂方印).
 *
 * v3 — switched from a procedurally drawn SVG to a hand-illustrated raster
 * asset (`/marketing/seal-yi.png`) supplied by the design owner. The earlier
 * SVG iterations (v1 朱文 hollow frame, v2 阴文 with feTurbulence erosion)
 * never looked sufficiently like a carved stone seal — the procedural noise
 * couldn't capture the asymmetric chips, double-frame line breaks, and the
 * specific 「译」 character form the brand wanted. The PNG version reads as a
 * real seal at every size we use it.
 *
 * Used in three places per docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md:
 *   1. Site header / footer brand mark (replaces `AV` letter block)
 *   2. Hero composition lower-right (over rice-paper card)
 *   3. Decorative accent in trust banner / final CTA
 *
 * Asset processing notes (for future replacement):
 *   - Source: white-background PNG from the design tool, ~1254×1254.
 *   - Pre-processing strips white via per-pixel alpha based on distance from
 *     pure white, crops to the seal bounding box + margin, and downsizes to
 *     512px max edge. Output saved to `/public/marketing/seal-yi.png` at
 *     ~320 KB. Done once per asset; not part of any build pipeline.
 *   - The image is square-ish (507×512); we render in a square box and let
 *     `object-contain` letterbox the tiny aspect difference invisibly.
 *
 * The `char` prop on the public API is preserved for backward compat but
 * ignored — the rendered seal is fixed to 「译」 because it's the brand mark.
 * If a future seal needs a different character, replace seal-yi.png with a
 * new asset (and consider whether the site really needs more than one seal).
 */
export interface SealStampProps {
  /**
   * IGNORED in v3 — kept for backward compat with existing call sites that
   * pass `char="译"` defensively. The brand seal is fixed to the 译 asset.
   */
  char?: string
  /** Pixel size (square). Defaults to 64. */
  size?: number
  /**
   * IGNORED in v3 — the cinnabar color is baked into the PNG. Kept for
   * backward compat.
   */
  color?: string
  /** Slight rotation degrees, simulates a hand-pressed stamp. */
  rotation?: number
  /** Extra Tailwind classes. */
  className?: string
  /** Aria label override for accessibility. Defaults to brand name. */
  ariaLabel?: string
}

export function SealStamp({
  size = 64,
  rotation = -3,
  className,
  ariaLabel,
}: SealStampProps) {
  const t = useTranslations("marketing.sealStamp")
  const label = ariaLabel ?? t("ariaLabel")
  return (
    <span
      role="img"
      aria-label={label}
      className={cn(
        "inline-block select-none align-middle",
        className,
      )}
      style={{
        width: size,
        height: size,
        transform: `rotate(${rotation}deg)`,
      }}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/marketing/seal-yi.png"
        alt=""
        width={size}
        height={size}
        draggable={false}
        decoding="async"
        className="h-full w-full object-contain pointer-events-none"
      />
    </span>
  )
}
