import { PrimaryCta } from "./primary-cta"
import { SealStamp } from "./seal-stamp"

/**
 * Marketing homepage hero — Ink Aesthetic redesign.
 *
 * See: docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md
 *
 * Composition (desktop):
 *   - Full-bleed background image (hero-paper.webp): West oil → ink wash transition.
 *     Anchored so the rice-paper right half is the text canvas.
 *   - Text content right-aligned, vertically centered, occupies the rice-paper area.
 *   - Pre-headline (痛点钩子) → Display-class headline → cinnabar accent line →
 *     subhead (对照式定位) → primary CTA (cinnabar) + secondary link → trust line.
 *   - Cinnabar SealStamp sits lower-right over the rice paper, replacing the
 *     AI-painted seal that was masked out during asset processing.
 *
 * Composition (mobile):
 *   - Image stacks above text. Image is wide-cropped to keep the right half
 *     (mountain + rice paper) visible. Text fills below in centered single column.
 *
 * Hard rules:
 *   - No `text-white` / `bg-white/N` carryover from the old dark hero — colors
 *     come from the [data-theme="ink"] token chain via Tailwind utilities.
 *   - The image is decorative (alt=""); semantic h1 carries page meaning.
 *   - LCP target: image is `priority` so it preloads, with explicit sizes hint
 *     to avoid downloading the 1920w on phones.
 */
export function Hero() {
  return (
    <section className="relative overflow-hidden marketing-hero-surface">
      {/* Full-width hero background — shows the complete oil → ink → paper
          narrative. Text legibility is solved by an explicit paper card behind
          the text content (rendered below), not by trying to fade the image. */}
      <div className="absolute inset-0 z-0 overflow-hidden" aria-hidden="true">
        <picture>
          <source
            type="image/webp"
            srcSet="/marketing/hero-paper-768.webp 768w, /marketing/hero-paper-1280.webp 1280w, /marketing/hero-paper-1920.webp 1920w"
            sizes="100vw"
          />
          <img
            src="/marketing/hero-paper-1920.jpg"
            srcSet="/marketing/hero-paper-768.jpg 768w, /marketing/hero-paper-1280.jpg 1280w, /marketing/hero-paper-1920.jpg 1920w"
            sizes="100vw"
            alt=""
            fetchPriority="high"
            decoding="async"
            className="absolute inset-0 h-full w-full object-cover object-center"
          />
        </picture>
      </div>

      <div className="relative z-10 mx-auto max-w-7xl px-4 py-24 sm:px-6 sm:py-28 lg:px-8 lg:py-32">
        <div className="grid grid-cols-1 md:grid-cols-12 gap-8 md:gap-12">
          {/* Left half on desktop: empty (image speaks). On mobile: hidden. */}
          <div className="hidden md:block md:col-span-5" aria-hidden="true" />

          {/* Right half: text canvas with rice-paper card backdrop. The card
              gives text a guaranteed-legible surface regardless of what's
              behind, and visually echoes a folded paper laid over the
              ink-wash backdrop — extending the metaphor rather than fighting it. */}
          <div
            className="md:col-span-7 md:pl-4 lg:pl-8 rounded-md md:bg-[rgba(245,240,230,0.92)] md:p-8 lg:p-10 md:backdrop-blur-[2px] md:shadow-[0_8px_40px_-20px_rgba(26,26,26,0.18)]"
          >
            {/* Pre-headline 钩子 — pain-point led */}
            <p className="ink-heading text-sm md:text-base leading-relaxed text-foreground/70 max-w-xl">
              长视频翻译总在三件事翻车 ——
              <br className="hidden sm:block" />
              口型对不上、改一句要重跑全片、剪辑师拿到的是死字幕
            </p>

            {/* Display-class headline */}
            <h1
              className="ink-display mt-5 text-4xl sm:text-5xl lg:text-6xl leading-tight text-foreground"
              style={{ letterSpacing: "-0.01em" }}
            >
              精准对齐的视频翻译，
              <br />
              直出剪映草稿
            </h1>

            {/* Cinnabar accent rule — visual anchor between display and subhead */}
            <div
              className="mt-6 h-px w-16"
              style={{ backgroundColor: "var(--cinnabar)" }}
              aria-hidden="true"
            />

            {/* Subhead — contrast positioning */}
            <p className="zh-body-lg mt-6 text-foreground/80 max-w-xl">
              不是又一个 AI 配音工具。是把这三件事单独做对的中文创作者本地化工作台。
            </p>

            {/* CTAs */}
            <div className="mt-9 flex flex-col items-start gap-3 sm:flex-row sm:items-center">
              <PrimaryCta className="min-w-[10rem]" />
              <a
                href="#product-proof"
                className="inline-flex items-center gap-1 text-sm font-medium text-foreground/80 hover:text-foreground transition-colors"
              >
                查看 30 秒产品演示
                <span aria-hidden="true">→</span>
              </a>
            </div>

            {/* Trust line under CTAs */}
            <p className="mt-5 text-xs text-foreground/55">
              无需绑卡 · 试用结束不会自动扣费
            </p>
          </div>
        </div>
      </div>

      {/* Seal stamp — lower-right, over rice paper.
          Replaces the AI-rendered seal that was removed during asset processing.
          Hidden on small screens to avoid clutter. */}
      <div className="absolute bottom-8 right-6 lg:bottom-12 lg:right-12 hidden md:block pointer-events-none">
        <SealStamp size={56} rotation={-4} />
      </div>
    </section>
  )
}
