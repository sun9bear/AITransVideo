import { CheckCircle2 } from "lucide-react"
import type { CSSProperties } from "react"
import { getTranslations } from "next-intl/server"
import { InkDivider } from "./ink-divider"

/**
 * Real product screenshots — full-height captures (no crop) shown inside a
 * fixed 3:2 viewport. Each frame auto-scrolls vertically so the visitor sees
 * the entire UI over time (top → bottom → top, ~18s cycle, pauses on hover).
 *
 * Why full-height + scroll instead of static crop: the prod UIs are taller
 * than 3:2 (the voice-selection page is nearly square; the translation review
 * page is 0.63 W:H). Cropping to a 3:2 frame either hides the stepper or
 * hides the actual editing controls — there's no good single frame. Auto-
 * scrolling shows everything without making the section a wall of tall
 * mockups. Reduced-motion users see the top crop (frozen at translateY 0).
 *
 * Source images live in /marketing/screenshots/ as webp (preferred) + jpg
 * fallback. Pass each instance its natural width/height so the component can
 * compute exactly how much to scroll: scroll-y-end =
 *   max(-(1 - 2*W/(3*H)) * 100%, 0) — i.e. zero when image is already
 *   wider than 3:2 (no scroll needed), otherwise the % of the image's own
 *   height that overflows the viewport.
 */
function ScrollingScreenshot({
  src,
  alt,
  naturalW,
  naturalH,
}: {
  src: string
  alt: string
  naturalW: number
  naturalH: number
}) {
  const VIEWPORT_RATIO = 3 / 2
  const imgRatio = naturalW / naturalH
  const scrollPct =
    imgRatio >= VIEWPORT_RATIO ? 0 : (1 - (2 * naturalW) / (3 * naturalH)) * 100
  // Animate only when there's >=4% to scroll. Below that the motion is too
  // subtle to be worth the GPU cost / distraction.
  const animate = scrollPct >= 4
  // webp sibling for the picture <source>: same path with .webp suffix.
  const webpSrc = src.replace(/\.(jpg|jpeg|png)$/i, ".webp")

  const animStyle: CSSProperties | undefined = animate
    ? ({ "--scroll-y-end": `-${scrollPct.toFixed(1)}%` } as CSSProperties)
    : undefined

  return (
    <div
      className="group relative overflow-hidden rounded-lg border border-border bg-muted/40 shadow-md"
      style={{ aspectRatio: "3 / 2" }}
    >
      <picture>
        <source type="image/webp" srcSet={webpSrc} />
        <img
          src={src}
          alt={alt}
          loading="lazy"
          decoding="async"
          width={naturalW}
          height={naturalH}
          className={`absolute inset-x-0 top-0 h-auto w-full select-none ${
            animate ? "scroll-y-shot" : ""
          }`}
          style={animStyle}
          draggable={false}
        />
      </picture>
    </div>
  )
}

/**
 * Product proof — replaces text-mockup UI with real screenshot slots.
 *
 * See: docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md §5.2 第二幕「演示」
 *
 * The previous version rendered text-based imitations of product UI (Job IDs
 * like "Bed88548..." were placeholder hashes inside fake card layouts). That
 * undermined the section title "真实产品证明". This version (2026-05-01) wires
 * four real screenshots — 新建翻译任务页 / 项目结果列表 / 翻译复核 / 三引擎音色选择 —
 * captured at full UI height and auto-scrolled inside a fixed 3:2 frame so
 * each one reveals every key control without forcing tall mockups in the
 * layout. All sourced from the live workspace UI.
 *
 * Anchor `id="product-proof"` lets the Hero secondary CTA jump here.
 */

export async function ProductProof() {
  const t = await getTranslations("marketing.productProof")
  const taskFlow = t.raw("taskFlow") as string[]
  const resultFlow = t.raw("resultFlow") as string[]

  return (
    <section
      id="product-proof"
      className="marketing-reading-surface py-14 sm:py-16"
    >
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-3xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            {t("eyebrow")}
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            {t("heading")}
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            {t("lead")}
          </p>
        </div>

        <div className="mt-10">
          <InkDivider variant={0} className="text-foreground/35" />
        </div>

        <div className="mt-12 grid gap-8 lg:grid-cols-2">
          <article className="rounded-2xl border border-border bg-card p-6 shadow-sm">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
                  {t("realUi", { n: "01" })}
                </p>
                <h3 className="ink-heading mt-2 text-xl text-foreground">
                  {t("card1Title")}
                </h3>
              </div>
              <span
                className="rounded-md border px-3 py-1 text-xs font-medium"
                style={{
                  borderColor: "color-mix(in oklab, var(--cinnabar) 25%, transparent)",
                  backgroundColor: "var(--cinnabar-soft)",
                  color: "var(--cinnabar)",
                }}
              >
                {t("card1Badge")}
              </span>
            </div>

            <div className="mt-5">
              <ScrollingScreenshot
                src="/marketing/screenshots/new-translation.jpg"
                alt={t("card1Alt")}
                naturalW={1280}
                naturalH={956}
              />
            </div>

            <ul className="mt-5 space-y-3 text-sm text-muted-foreground">
              {taskFlow.map((item) => (
                <li key={item} className="flex items-start gap-2">
                  <CheckCircle2
                    className="mt-0.5 h-4 w-4 shrink-0"
                    style={{ color: "var(--cinnabar)" }}
                  />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </article>

          <article className="rounded-2xl border border-border bg-card p-6 shadow-sm">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
                  {t("realUi", { n: "02" })}
                </p>
                <h3 className="ink-heading mt-2 text-xl text-foreground">
                  {t("card2Title")}
                </h3>
              </div>
              <span
                className="rounded-md border px-3 py-1 text-xs font-medium"
                style={{
                  borderColor: "color-mix(in oklab, var(--cinnabar) 25%, transparent)",
                  backgroundColor: "var(--cinnabar-soft)",
                  color: "var(--cinnabar)",
                }}
              >
                {t("card2Badge")}
              </span>
            </div>

            <div className="mt-5">
              <ScrollingScreenshot
                src="/marketing/screenshots/project-list.jpg"
                alt={t("card2Alt")}
                naturalW={1280}
                naturalH={1181}
              />
            </div>

            <ul className="mt-5 space-y-3 text-sm text-muted-foreground">
              {resultFlow.map((item) => (
                <li key={item} className="flex items-start gap-2">
                  <CheckCircle2
                    className="mt-0.5 h-4 w-4 shrink-0"
                    style={{ color: "var(--cinnabar)" }}
                  />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </article>
        </div>

        {/* Two more screenshot slots — Studio review timeline + Three-engine voice tabs.
            These are referenced from WorkflowShowcase too; keeping them here makes the
            "real product evidence" zone complete. */}
        <div className="mt-8 grid gap-8 lg:grid-cols-2">
          <article className="rounded-2xl border border-border bg-card p-6 shadow-sm">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
                  {t("realUi", { n: "03" })}
                </p>
                <h3 className="ink-heading mt-2 text-xl text-foreground">
                  {t("card3Title")}
                </h3>
              </div>
            </div>
            <div className="mt-5">
              <ScrollingScreenshot
                src="/marketing/screenshots/translation-review.jpg"
                alt={t("card3Alt")}
                naturalW={1280}
                naturalH={2029}
              />
            </div>
          </article>

          <article className="rounded-2xl border border-border bg-card p-6 shadow-sm">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
                  {t("realUi", { n: "04" })}
                </p>
                <h3 className="ink-heading mt-2 text-xl text-foreground">
                  {t("card4Title")}
                </h3>
              </div>
            </div>
            <div className="mt-5">
              <ScrollingScreenshot
                src="/marketing/screenshots/voice-selection.jpg"
                alt={t("card4Alt")}
                naturalW={1280}
                naturalH={1381}
              />
            </div>
          </article>
        </div>

        {/* "购买内容 / 你买到的不只是一个视频文件" + DIGITAL_DELIVERABLES grid
            removed 2026-05-02. The information was redundant — visitors get
            the same "concrete deliverables" message from FeaturedDemos
            (real dubbed clips), and from the per-tier benefits already
            listed in <PricingPreview />. Compressing the page reduces
            scroll fatigue without losing any factual claim. */}
      </div>
    </section>
  )
}
