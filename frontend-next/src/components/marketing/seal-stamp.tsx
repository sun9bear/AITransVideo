import { cn } from "@/lib/utils"

/**
 * Cinnabar seal stamp (朱砂方印) as a pure SVG component.
 *
 * Style: 朱文 — red character + red border, transparent interior. This is the
 * canonical "calligrapher's signature seal" reading: the carved stone leaves
 * raised character/border that pick up cinnabar paste, the rest of the seal
 * face is flat (so paper shows through when stamped). It reads unambiguously
 * as a Chinese seal at any size, where the inverse 阴文 style (solid red block
 * with negative-space character) tends to look like a sticker logo on small
 * marks.
 *
 * Used in three places per docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md:
 *   1. Site header / footer brand mark (replaces `AV` letter block)
 *   2. Hero composition lower-right (over ink-wash backdrop)
 *   3. Decorative accent in trust banner / final CTA
 *
 * Design rules (do NOT regress):
 *   - Single Chinese character, default 「译」 (translate). Stay single-char — two
 *     chars crowd the seal and lose calligraphic balance.
 *   - Cinnabar `#C73E3A` for both border and character; no other colors. Inside
 *     of the frame is fully transparent so it picks up whatever surface it sits on.
 *   - Subtle organic edge via low-amplitude turbulence — reads as carved stone
 *     rather than a digital outline. Don't crank the displacement scale; over
 *     ~0.8 the character starts looking damaged instead of stamped.
 *   - Ink dots in two corners — stamped seals always have minor cinnabar paste
 *     irregularities. Two faint dots are enough; more starts looking grungy.
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
  // Stable seeds keyed off the character so the same `char` always renders the
  // same distress pattern across the site (header logo + hero seal must look
  // like the same physical stamp pressed twice, not two different ones).
  const seed = char.charCodeAt(0) % 100
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
        {/* Edge distress: low-amplitude turbulence + small displacement.
            Scale=0.6 is the sweet spot — visible bite on the strokes without
            destroying glyph legibility. */}
        <filter
          id={`seal-distress-${seed}`}
          x="-4%"
          y="-4%"
          width="108%"
          height="108%"
        >
          <feTurbulence
            type="fractalNoise"
            baseFrequency="1.3"
            numOctaves="2"
            seed={seed}
            result="noise"
          />
          <feDisplacementMap
            in="SourceGraphic"
            in2="noise"
            scale="0.6"
            xChannelSelector="R"
            yChannelSelector="G"
          />
        </filter>

        {/* Ink-load mask: faint vignette of cinnabar, slightly heavier on one
            side to suggest a hand-pressed unevenness. Composited over the
            seal so high-density areas read as wetter ink. */}
        <radialGradient
          id={`seal-inkload-${seed}`}
          cx="42%"
          cy="38%"
          r="80%"
        >
          <stop offset="0%" stopColor={color} stopOpacity="0.06" />
          <stop offset="60%" stopColor={color} stopOpacity="0" />
        </radialGradient>
      </defs>

      {/* Inner ink-load wash — confined to a slightly smaller rect so it never
          paints outside the perceived seal face. Sits *behind* the strokes so
          the strokes still read crisply on top. */}
      <rect
        x="9"
        y="9"
        width="82"
        height="82"
        fill={`url(#seal-inkload-${seed})`}
      />

      <g filter={`url(#seal-distress-${seed})`}>
        {/* Outer carved border — thick stroke, hollow center.
            strokeWidth=3.5 keeps the frame visually weighted at small sizes
            (24-32px brand mark) without overwhelming the character at large
            sizes (56-72px hero seal). */}
        <rect
          x="8"
          y="8"
          width="84"
          height="84"
          fill="none"
          stroke={color}
          strokeWidth="3.5"
          strokeLinejoin="miter"
          rx="0.5"
          ry="0.5"
        />

        {/* Character — red strokes, large, heavy weight. fontWeight=900 makes
            the strokes substantial enough to read as carved rather than typed.
            Slight y-offset (51 instead of 50) compensates for the optical
            heaviness of Songti glyphs which sit slightly low in their box. */}
        <text
          x="50"
          y="51"
          textAnchor="middle"
          dominantBaseline="central"
          fontFamily='"Noto Serif SC", "Source Han Serif SC", "Songti SC", "STSong", serif'
          fontSize="62"
          fontWeight="900"
          fill={color}
        >
          {char}
        </text>
      </g>

      {/* Ink-paste imperfections — two tiny dots in corners. Real seals always
          have these; their absence is what makes vector seals look fake. */}
      <circle cx="20" cy="83" r="0.7" fill={color} opacity="0.5" />
      <circle cx="82" cy="18" r="0.5" fill={color} opacity="0.4" />
      <circle cx="14" cy="22" r="0.4" fill={color} opacity="0.35" />
    </svg>
  )
}
