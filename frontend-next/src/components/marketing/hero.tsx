import { SealStamp } from "./seal-stamp"
import { HeroSamplePlayer } from "./hero-sample-player"
import { AnonymousTrialLauncher } from "./anonymous-trial-launcher"
import { loadFeaturedDemos } from "./featured-demos-data"

/**
 * Marketing homepage hero — Ink Aesthetic, sample-player-led (anonymous-preview
 * funnel Phase 1).
 *
 * The hero now leads with PROOF: a large A/B sample player (中文配音 ↔ 英文原片)
 * is the primary visual signal instead of a static painting. The Ink backdrop
 * (oil → ink wash → rice paper) and cinnabar SealStamp are retained as texture.
 *
 * Composition:
 *   - Desktop (lg+): two columns — text + CTA on the rice-paper card (left),
 *     sample player (right). Both above the fold.
 *   - Mobile: the player is visually first (it muted-autoplays in view), with
 *     the text card below. DOM order keeps the <h1> first for semantics/SEO via
 *     CSS `order`, so the visual swap doesn't cost heading priority.
 *
 * CTA: the primary action is `立即试用` (AnonymousTrialLauncher) — it opens the
 * anonymous trial panel WITHOUT routing to registration, per the funnel. The
 * registration path still lives in the header / pricing sections.
 *
 * Hard rules (unchanged):
 *   - No `text-white` / `bg-white/N` carryover — colors come from the
 *     [data-theme="ink"] token chain via Tailwind utilities.
 *   - The background image is decorative (alt=""); the semantic <h1> carries
 *     page meaning. The text card guarantees legibility over either half of the
 *     painting.
 */
export function Hero() {
  const demos = loadFeaturedDemos()
  const hasDemos = demos.length > 0

  return (
    <section className="relative overflow-hidden marketing-hero-surface">
      {/* Full-bleed Ink backdrop. The paper text card + the player's own dark
          frame keep their contents legible over it, so no image fade needed. */}
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

      <div className="relative z-10 mx-auto max-w-7xl px-4 py-16 sm:px-6 sm:py-20 lg:px-8 lg:py-24">
        <div className="grid grid-cols-1 items-center gap-8 lg:grid-cols-12 lg:gap-12">
          {/* Text canvas — DOM-first (so <h1> stays the first heading), but
              visually below the player on mobile and to the left on desktop. */}
          <div className="order-2 lg:order-1 lg:col-span-5 rounded-md bg-[rgba(245,240,230,0.95)] p-6 sm:p-7 md:bg-[rgba(245,240,230,0.92)] md:p-8 backdrop-blur-[2px] shadow-[0_8px_40px_-20px_rgba(26,26,26,0.18)]">
            <p className="ink-heading text-xs sm:text-sm uppercase tracking-[0.25em] text-[color:var(--cinnabar)]">
              爱译视频 · AITrans.Video
            </p>

            <h1
              className="ink-display mt-4 text-4xl leading-tight text-foreground sm:text-5xl lg:text-5xl xl:text-6xl"
              style={{ letterSpacing: "-0.01em" }}
            >
              让世界视频，
              <br className="hidden sm:block" />
              开口说中文
            </h1>

            <div
              className="mt-6 h-px w-16"
              style={{ backgroundColor: "var(--cinnabar)" }}
              aria-hidden="true"
            />

            <p className="zh-body-lg mt-6 max-w-xl text-foreground/80">
              把英文长视频变成可发布的中文配音版。免注册先预览效果——前 3 分钟中文配音，满意再注册下载、生成完整视频。
            </p>

            <div className="mt-8 flex flex-col items-start gap-3 sm:flex-row sm:items-center">
              <AnonymousTrialLauncher />
              <a
                href="#pricing"
                className="inline-flex items-center gap-1 text-sm font-medium text-foreground/80 transition-colors hover:text-foreground"
              >
                查看套餐价格
                <span aria-hidden="true">→</span>
              </a>
            </div>

            <p className="mt-5 text-xs text-foreground/55">
              免注册试用 · 英文转中文 · 失败不计费 · 支持长视频
            </p>
          </div>

          {/* Sample player — visually first on mobile (muted-autoplays in view),
              right column on desktop. */}
          {hasDemos ? (
            <div className="order-1 lg:order-2 lg:col-span-7">
              <HeroSamplePlayer demos={demos} />
              <p className="mt-3 hidden text-xs text-foreground/70 lg:block">
                鼠标移到画面上自动播放，点左上角{" "}
                <span className="font-medium text-foreground/85">开启声音</span> 试听；右上角切换{" "}
                <span className="font-medium text-foreground/85">英文原片 / 中文配音</span> 对比。
              </p>
            </div>
          ) : null}
        </div>
      </div>

      {/* Cinnabar seal — lower-right over rice paper. Hidden on small screens. */}
      <div className="pointer-events-none absolute bottom-8 right-6 hidden md:block lg:bottom-12 lg:right-12">
        <SealStamp size={56} rotation={-4} />
      </div>
    </section>
  )
}
