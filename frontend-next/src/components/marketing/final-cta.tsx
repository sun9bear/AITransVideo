import { PrimaryCta } from "./primary-cta"
import { LinkButton } from "./link-button"

/**
 * Bottom-of-page CTA block. DESIGN.md §3.4 — direct Chinese phrasing, solid
 * primary button, one secondary fallback. No hero-level drama, no oversized
 * headline that overshadows the rest of the page.
 */
export function FinalCta() {
  return (
    <section className="relative overflow-hidden marketing-hero-surface">
      <div className="mx-auto max-w-4xl px-4 py-20 sm:px-6 sm:py-24 lg:px-8">
        <div className="text-center">
          <h2 className="text-3xl font-bold tracking-tight text-white sm:text-4xl">
            准备好提升本地化效率了吗？
          </h2>
          <p className="mt-4 zh-body-lg text-white/75">
            免费开始，验证对齐质量与剪映草稿导出，再决定是否升级。
          </p>
          <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
            <PrimaryCta />
            <LinkButton
              href="/pricing"
              variant="outline"
              className="border-white/20 bg-white/5 text-white hover:bg-white/10 hover:text-white"
            >
              查看套餐
            </LinkButton>
          </div>
        </div>
      </div>
    </section>
  )
}
