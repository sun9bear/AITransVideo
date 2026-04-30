import { PrimaryCta } from "./primary-cta"
import { LinkButton } from "./link-button"
import { InkDivider } from "./ink-divider"
import { SealStamp } from "./seal-stamp"

/**
 * Final CTA — page-foot conversion block, ink-aesthetic refresh.
 *
 * See: docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md §5.2 第四幕「行动」
 *
 * Previous version used `marketing-hero-surface` (deep-blue gradient + white text)
 * which became visually jarring once the rest of the marketing page switched to
 * the ink theme's rice-paper canvas. This rewrite keeps the page tonally
 * consistent end-to-end:
 *
 *   - Rice-paper background (token `--ink-paper`, slight cinnabar wash on top edge)
 *   - Feibai (飞白) brush divider as the section opener — visually picks up
 *     where the prior section left off
 *   - Cinnabar accent strip + display-class headline; subhead in ink-gray
 *   - Primary CTA stays cinnabar-filled; secondary becomes a quiet ghost link
 *     so the eye lands on the primary first
 *   - SealStamp lower-right echoes the Hero placement, framing the page like a
 *     finished scroll
 *
 * The whole composition is centered, narrower than the global max-w to read as
 * "落款" (the calligrapher's signature/seal at the end of a scroll).
 */
export function FinalCta() {
  return (
    <section className="relative overflow-hidden bg-[color:var(--ink-paper,#F5F0E6)]">
      {/* Soft cinnabar wash at top edge — picks up the section above without
          becoming a hard bar. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-24"
        style={{
          background:
            "linear-gradient(180deg, color-mix(in oklab, var(--cinnabar) 6%, transparent) 0%, transparent 100%)",
        }}
      />

      <div className="relative mx-auto max-w-3xl px-4 py-20 sm:px-6 sm:py-24 lg:px-8">
        {/* Feibai opener — narrower than full width, centered as a calligraphic flourish */}
        <div className="mx-auto max-w-xs">
          <InkDivider variant={2} className="text-[color:var(--cinnabar,#C73E3A)]/55" />
        </div>

        <div className="mt-10 text-center">
          <p className="ink-heading text-xs uppercase tracking-[0.25em] text-[color:var(--cinnabar,#C73E3A)]">
            开始本地化
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl lg:text-5xl">
            把你的下一支视频，
            <br className="hidden sm:block" />
            交给精准对齐
          </h2>
          <p className="zh-body-lg mt-6 text-muted-foreground max-w-xl mx-auto">
            免费开始，先验证对齐质量与剪映草稿导出，再决定是否升级。
          </p>

          <div className="mt-9 flex flex-col items-center justify-center gap-3 sm:flex-row">
            <PrimaryCta className="min-w-[12rem]" />
            <LinkButton href="/pricing" variant="outline" className="min-w-[10rem]">
              先看定价
            </LinkButton>
          </div>

          <p className="mt-5 text-xs text-foreground/55">
            无需绑卡 · 试用结束不会自动扣费
          </p>
        </div>

        {/* Seal stamp — lower-right, frames the section like a calligrapher's
            signature at the end of a scroll. Hidden on small screens to avoid
            crowding the centered text column. */}
        <div className="absolute bottom-8 right-6 lg:bottom-10 lg:right-10 hidden md:block pointer-events-none">
          <SealStamp size={48} rotation={-3} />
        </div>
      </div>
    </section>
  )
}
