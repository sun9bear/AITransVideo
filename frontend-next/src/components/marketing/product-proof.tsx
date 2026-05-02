import { CheckCircle2 } from "lucide-react"
import type { CSSProperties } from "react"
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

const TASK_FLOW = [
  "支持 YouTube 链接和视频上传两种入口，适合真实业务输入。",
  "快捷版适合自动流程，工作台版适合人工复核和精调。",
  "可选转录方案、说话人数与处理模式，不是模板表单占位页。",
]

const RESULT_FLOW = [
  "任务完成后保留项目列表、到期时间、处理状态和历史记录。",
  "每个项目都能下载配音视频、配音音频和素材包。",
  "这类结果页能直接证明网站提供的是可交付的数字化服务。",
]

export function ProductProof() {
  return (
    <section
      id="product-proof"
      className="marketing-reading-surface py-14 sm:py-16"
    >
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-3xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            真实产品证明
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            不是一次性生成工具，而是可复核、可修改、可下载的工作台
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            爱译视频在站内提供完整的视频翻译配音流程：创建任务、查看进度、逐句复核、修改与重生成，最后下载配音视频、配音音频、字幕和素材包。下面的截图来自当前正在运行的产品界面。
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
                  真实界面 01
                </p>
                <h3 className="ink-heading mt-2 text-xl text-foreground">
                  新建翻译任务
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
                任务创建页
              </span>
            </div>

            <div className="mt-5">
              <ScrollingScreenshot
                src="/marketing/screenshots/new-translation.jpg"
                alt="新建翻译任务页：YouTube 链接 / 上传视频切换 + 快捷版（Express）/ 工作台版（Studio）双方案选择 + 创建任务按钮"
                naturalW={1280}
                naturalH={956}
              />
            </div>

            <ul className="mt-5 space-y-3 text-sm text-muted-foreground">
              {TASK_FLOW.map((item) => (
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
                  真实界面 02
                </p>
                <h3 className="ink-heading mt-2 text-xl text-foreground">
                  项目结果与下载交付物
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
                结果列表页
              </span>
            </div>

            <div className="mt-5">
              <ScrollingScreenshot
                src="/marketing/screenshots/project-list.jpg"
                alt="项目结果列表页：每个任务卡片显示标题、过期时间、配音视频/音频/素材包下载按钮和修改入口"
                naturalW={1280}
                naturalH={1181}
              />
            </div>

            <ul className="mt-5 space-y-3 text-sm text-muted-foreground">
              {RESULT_FLOW.map((item) => (
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
                  真实界面 03
                </p>
                <h3 className="ink-heading mt-2 text-xl text-foreground">
                  时间轴上逐句复核译文
                </h3>
              </div>
            </div>
            <div className="mt-5">
              <ScrollingScreenshot
                src="/marketing/screenshots/translation-review.jpg"
                alt="Studio 翻译审核界面：九步进度条 + 说话人确认 + 中英文逐句对照编辑"
                naturalW={1280}
                naturalH={2029}
              />
            </div>
          </article>

          <article className="rounded-2xl border border-border bg-card p-6 shadow-sm">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
                  真实界面 04
                </p>
                <h3 className="ink-heading mt-2 text-xl text-foreground">
                  三引擎音色选择
                </h3>
              </div>
            </div>
            <div className="mt-5">
              <ScrollingScreenshot
                src="/marketing/screenshots/voice-selection.jpg"
                alt="Studio 音色选择界面：MiniMax / CosyVoice / 豆包 三引擎切换 + 试听 / 核对原音 / 克隆音色"
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
