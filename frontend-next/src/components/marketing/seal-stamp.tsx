import { cn } from "@/lib/utils"

/**
 * Cinnabar seal stamp (朱砂方印) as a pure SVG component.
 *
 * Style: 阴文 — solid cinnabar block with the character recessed (paper color
 * carved through). The reading: a stone seal pressed onto rice paper. Both
 * the BLOCK EDGE and the CHARACTER EDGE share the same physical erosion so
 * the seal looks carved-and-stamped, not vector-drawn.
 *
 * Used in three places per docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md:
 *   1. Site header / footer brand mark (replaces `AV` letter block)
 *   2. Hero composition lower-right (over rice-paper card)
 *   3. Decorative accent in trust banner / final CTA
 *
 * Design rules (do NOT regress without explicit reason):
 *   - Single Chinese character, default 「译」 (translate).
 *   - Cinnabar `#C73E3A` for the block; paper-color `#F5F0E6` for the
 *     carved-out character. NOT `var(--ink-paper)` — the seal must render
 *     correctly outside the [data-theme="ink"] scope (e.g. on tooling pages
 *     that drop the marketing layer wrapper).
 *   - Edge erosion runs at scale=5 with low-frequency noise (baseFrequency
 *     0.16) — big enough features that the displacement reads as carved
 *     stone, not as anti-aliasing. Below scale=3 the seal looks like a
 *     digital sticker; above scale=7 the rectangle starts to fall apart.
 *   - Paste density wash: a second turbulence converted to alpha mask,
 *     composited at opacity 0.6 over the block for ink-load variation.
 *     Without this the cinnabar fill looks flat and printed.
 *   - Five splatter dots OUTSIDE the block — the telltale of a hand-pressed
 *     stamp. Vector seals without splatter look fake.
 *   - Font fallback chain prefers Songti-derived Chinese fonts that read as
 *     more "篆隶" in proportion (STZhongsong / STFangsong / FangSong) before
 *     falling through to Noto Serif SC. True 篆书 fonts are rarely installed
 *     on user machines, so we maximize visual filling of the negative space
 *     with a heavy Songti rather than chasing a font most users don't have.
 *
 * AI-generated seal stamps embedded in raster hero images MUST be masked or
 * removed upstream — this SVG is the single source of truth so resolution
 * scales perfectly.
 */
export interface SealStampProps {
  /** Single Chinese character to engrave. Defaults to "译". */
  char?: string
  /** Pixel size (square). Defaults to 64. */
  size?: number
  /** Override base color. Defaults to cinnabar. Use sparingly. */
  color?: string
  /** Slight rotation degrees, simulates a hand-pressed stamp. */
  rotation?: number
  /** Extra Tailwind classes. */
  className?: string
  /** Aria label override for accessibility. Defaults to brand name. */
  ariaLabel?: string
}

export function SealStamp({
  char = "译",
  size = 64,
  color = "#C73E3A",
  rotation = -3,
  className,
  ariaLabel = "AIVideoTrans 章印",
}: SealStampProps) {
  // Stable distress seeded by character. Same `char` always renders the same
  // distress pattern so the header logo + hero seal look like the same
  // physical stamp pressed twice, not two different seals.
  const seed = char.charCodeAt(0) % 100
  const erodeFilter = `seal-erode-${seed}`
  const pasteFilter = `seal-paste-${seed}`
  const paperColor = "#F5F0E6"

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 100 100"
      width={size}
      height={size}
      role="img"
      aria-label={ariaLabel}
      className={cn("inline-block select-none overflow-visible", className)}
      style={{ transform: `rotate(${rotation}deg)` }}
    >
      <defs>
        {/* Edge erosion: low-frequency turbulence + significant displacement
            so the rect reads as a hand-carved stone face. Applied to the
            whole seal group (block + character) so edges and the character's
            negative space share the same physical irregularity. */}
        <filter
          id={erodeFilter}
          x="-15%"
          y="-15%"
          width="130%"
          height="130%"
        >
          <feTurbulence
            type="fractalNoise"
            baseFrequency="0.16"
            numOctaves="3"
            seed={seed}
            result="rough"
          />
          <feDisplacementMap
            in="SourceGraphic"
            in2="rough"
            scale="5"
            xChannelSelector="R"
            yChannelSelector="G"
          />
        </filter>

        {/* Cinnabar paste density variation: high-frequency noise turned into
            an alpha mask, composited over the rect so the fill looks wetter
            on some pixels and drier on others. Without this the block looks
            like a flat printed swatch. */}
        <filter id={pasteFilter} x="0%" y="0%" width="100%" height="100%">
          <feTurbulence
            type="fractalNoise"
            baseFrequency="2.2"
            numOctaves="2"
            seed={seed + 7}
            result="grain"
          />
          <feColorMatrix
            in="grain"
            type="matrix"
            values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 0.55 -0.15"
          />
          <feComposite in2="SourceGraphic" operator="in" />
        </filter>
      </defs>

      {/* Main carved seal face: solid cinnabar block + recessed character.
          Both pushed through the erode filter together so the block edge
          and the character's negative space share the same hand-carved bite. */}
      <g filter={`url(#${erodeFilter})`}>
        <rect x="10" y="10" width="80" height="80" fill={color} />
        <text
          x="50"
          y="52"
          textAnchor="middle"
          dominantBaseline="central"
          fontFamily='"STZhongsong", "STFangsong", "FangSong", "FZShuTi", "STKaiti", "Noto Serif SC", "Source Han Serif SC", "Songti SC", "STSong", serif'
          fontSize="60"
          fontWeight="900"
          fill={paperColor}
        >
          {char}
        </text>
      </g>

      {/* Paste density wash — sits over the eroded block, varies cinnabar
          saturation across the face for wet/dry hand-pressed look. Not
          eroded itself (extends slightly past the visible edge to bleed
          into the frayed corners where ink would actually pool). */}
      <rect
        x="10"
        y="10"
        width="80"
        height="80"
        fill={color}
        filter={`url(#${pasteFilter})`}
        opacity="0.6"
      />

      {/* External splatter — five tiny cinnabar dots placed asymmetrically
          outside the block. The signature of a hand-pressed stamp; without
          these every vector seal reads as fake. */}
      <circle cx="5" cy="50" r="0.8" fill={color} opacity="0.5" />
      <circle cx="95" cy="22" r="0.6" fill={color} opacity="0.4" />
      <circle cx="48" cy="97" r="0.7" fill={color} opacity="0.45" />
      <circle cx="14" cy="93" r="0.4" fill={color} opacity="0.35" />
      <circle cx="92" cy="80" r="0.5" fill={color} opacity="0.4" />
    </svg>
  )
}
