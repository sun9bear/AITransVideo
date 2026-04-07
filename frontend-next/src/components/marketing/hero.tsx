import { Sparkles } from "lucide-react"
import { PrimaryCta } from "./primary-cta"
import { LinkButton } from "./link-button"

/**
 * Marketing homepage hero.
 *
 * DESIGN.md §3.3 — concise Chinese headline, direct subheadline, one primary CTA
 * and one secondary link. No abstract poetic slogans, no oversized type that runs
 * ahead of current product facts.
 *
 * Uses `marketing-hero-surface` for a dark-capable media panel per §3.1.
 */
export function Hero() {
  return (
    <section className="relative overflow-hidden marketing-hero-surface">
      <div className="mx-auto max-w-6xl px-4 py-20 sm:px-6 sm:py-24 lg:px-8 lg:py-28">
        <div className="mx-auto max-w-3xl text-center">
          <div className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3 py-1 text-xs font-medium text-white/80">
            <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
            专注长视频的 AI 翻译配音工作台
          </div>
          <h1 className="mt-6 text-4xl font-bold tracking-tight text-white sm:text-5xl">
            精准对齐的视频翻译，
            <br className="hidden sm:block" />
            直出剪映草稿
          </h1>
          <p className="mt-5 text-lg leading-relaxed text-white/75 sm:text-xl">
            AI 翻译配音 + DSP 级精准对齐，支持导出剪映工程，让多语种内容分发更专业、更高效。
          </p>
          <div className="mt-9 flex flex-col items-center justify-center gap-3 sm:flex-row">
            <PrimaryCta className="min-w-[10rem]" />
            <LinkButton
              href="/pricing"
              variant="outline"
              className="min-w-[10rem] border-white/20 bg-white/5 text-white hover:bg-white/10 hover:text-white"
            >
              查看定价
            </LinkButton>
          </div>
          <p className="mt-5 text-xs text-white/55">
            无需绑卡 · 试用结束不会自动扣费
          </p>
        </div>
      </div>
    </section>
  )
}
