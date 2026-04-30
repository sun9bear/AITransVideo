import { cn } from "@/lib/utils"

/**
 * Cinnabar seal stamp (朱砂方印) as a pure SVG component.
 *
 * Used in three places per docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md:
 *   1. Site header / footer brand mark (replaces `AV` letter block)
 *   2. Hero composition lower-right (over ink-wash backdrop)
 *   3. Decorative accent in trust banner / final CTA
 *
 * Design rules (do NOT regress):
 *   - Single Chinese character, default 「译」 (translate). Stay single-char — two chars
 *     crowd the seal and lose calligraphic balance.
 *   - Cinnabar `#C73E3A` only. Don't tint based on context.
 *   - Subtle organic edge (irregular border + slight rotation) to read as carved stone,
 *     not a digital sticker.
 *   - Text uses platform Songti / Noto Serif fallback so the engraved look is real
 *     glyph-driven, not raster-imitated.
 *
 * AI-generated seal stamps embedded in raster hero images MUST be masked or removed
 * upstream — this SVG is the single source of truth so resolution scales perfectly.
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
  // SVG viewBox is normalized 100×100 — all internal math stays simple.
  // Outer border path uses small jitter to imply carved edges without
  // looking glitched.
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 100 100"
      width={size}
      height={size}
      role="img"
      aria-label={ariaLabel}
      className={cn("inline-block select-none", className)}
      style={{ transform: `rotate(${rotation}deg)` }}
    >
      <defs>
        {/* Slight ink-bleed filter for organic edge without GPU cost */}
        <filter id="seal-bleed" x="-5%" y="-5%" width="110%" height="110%">
          <feTurbulence
            type="fractalNoise"
            baseFrequency="0.9"
            numOctaves="2"
            seed="3"
            result="noise"
          />
          <feDisplacementMap
            in="SourceGraphic"
            in2="noise"
            scale="1.2"
            xChannelSelector="R"
            yChannelSelector="G"
          />
        </filter>
        {/* Light noise overlay to mimic uneven ink loading */}
        <filter id="seal-grain" x="0%" y="0%" width="100%" height="100%">
          <feTurbulence
            type="fractalNoise"
            baseFrequency="2.5"
            numOctaves="1"
            seed="7"
            result="grain"
          />
          <feColorMatrix
            in="grain"
            type="matrix"
            values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 0.18 0"
          />
          <feComposite in2="SourceGraphic" operator="in" />
        </filter>
      </defs>

      {/* Outer carved border. Two layers: solid fill below, grain overlay above. */}
      <g filter="url(#seal-bleed)">
        <rect
          x="6"
          y="6"
          width="88"
          height="88"
          fill={color}
          rx="2"
          ry="2"
        />
        {/* Inner negative cut — gives the carved-stone illusion */}
        <rect
          x="11"
          y="11"
          width="78"
          height="78"
          fill="none"
          stroke={color}
          strokeWidth="2"
          rx="1"
          ry="1"
          opacity="0.85"
        />
      </g>

      {/* Engraved character — paper color so it reads as a relief cut */}
      <text
        x="50"
        y="50"
        textAnchor="middle"
        dominantBaseline="central"
        fontFamily='"Noto Serif SC", "Source Han Serif SC", "Songti SC", "STSong", serif'
        fontSize="56"
        fontWeight="700"
        fill="var(--ink-paper, #F5F0E6)"
        filter="url(#seal-bleed)"
      >
        {char}
      </text>

      {/* Grain overlay clipped to seal shape for distressed feel */}
      <rect
        x="6"
        y="6"
        width="88"
        height="88"
        fill={color}
        opacity="0.0"
        filter="url(#seal-grain)"
      />
    </svg>
  )
}
