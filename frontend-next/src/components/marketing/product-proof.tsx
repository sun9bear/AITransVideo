import {
  CheckCircle2,
  Download,
  FileVideo2,
  FolderArchive,
  Languages,
  Mic2,
  Sparkles,
  WandSparkles,
} from "lucide-react"
import { DIGITAL_DELIVERABLES } from "./company-info"
import { ScreenshotPlaceholder } from "./screenshot-placeholder"
import { InkDivider } from "./ink-divider"

/**
 * Product proof — replaces text-mockup UI with real screenshot slots.
 *
 * See: docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md §5.2 第二幕「演示」
 *
 * The previous version rendered text-based imitations of product UI (Job IDs
 * like "Bed88548..." were placeholder hashes inside fake card layouts). That
 * undermined the section title "真实产品证明". This version uses
 * <ScreenshotPlaceholder> slots labeled with the screenshot we expect to land
 * there; the user replaces them with <Image> once real captures are available.
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
      className="marketing-reading-surface py-20 sm:py-24"
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
              <ScreenshotPlaceholder
                label="新建翻译任务页"
                hint="3:2 · 含 YouTube/上传切换 + Express/Studio 选择 + 转录方案 + 创建按钮"
                aspectRatio="3 / 2"
                className="shadow-md"
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
              <ScreenshotPlaceholder
                label="项目结果列表页"
                hint="3:2 · 含到期提示 + 任务卡片 + 配音视频/音频/素材包下载按钮"
                aspectRatio="3 / 2"
                className="shadow-md"
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
              <ScreenshotPlaceholder
                label="Studio 时间轴复核界面"
                hint="3:2 · 含波形/字幕轨/译文编辑/单段重生成入口"
                aspectRatio="3 / 2"
                className="shadow-md"
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
              <ScreenshotPlaceholder
                label="Studio 三引擎选音色 Tab"
                hint="3:2 · MiniMax / CosyVoice / VolcEngine 三 Tab 对比 + 音色卡片"
                aspectRatio="3 / 2"
                className="shadow-md"
              />
            </div>
          </article>
        </div>

        <div className="mt-12 rounded-2xl border border-border bg-muted/40 p-6 sm:p-8">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
            <div className="max-w-2xl">
              <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
                购买内容
              </p>
              <h3 className="ink-heading mt-2 text-2xl text-foreground">
                购买的是数字化视频本地化服务和真实交付物
              </h3>
              <p className="mt-3 zh-body text-muted-foreground">
                付费套餐解锁的是账户内处理能力和工作台权益，不是单次静态下载页。任务完成后，用户可在站内查看项目状态、复核内容并下载交付结果。
              </p>
            </div>
            <div
              className="rounded-md border px-4 py-3 text-sm"
              style={{
                borderColor: "color-mix(in oklab, var(--cinnabar) 25%, transparent)",
                backgroundColor: "var(--cinnabar-soft)",
                color: "var(--cinnabar)",
              }}
            >
              支持长视频、多说话人、增量重生成和剪映草稿导出
            </div>
          </div>

          <div className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {DIGITAL_DELIVERABLES.map((item) => {
              const icon =
                item === "配音视频" ? (
                  <FileVideo2 className="h-4 w-4" />
                ) : item === "配音音频" ? (
                  <Mic2 className="h-4 w-4" />
                ) : item === "字幕/素材包" ? (
                  <FolderArchive className="h-4 w-4" />
                ) : item === "剪映草稿工程" ? (
                  <Download className="h-4 w-4" />
                ) : item === "人工复核工作台" ? (
                  <Languages className="h-4 w-4" />
                ) : item === "增量重生成能力" ? (
                  <WandSparkles className="h-4 w-4" />
                ) : (
                  <Sparkles className="h-4 w-4" />
                )

              return (
                <div
                  key={item}
                  className="flex items-center gap-3 rounded-md border border-border bg-card px-4 py-3 text-sm text-foreground shadow-sm"
                >
                  <span
                    className="rounded-full p-2"
                    style={{
                      backgroundColor: "var(--cinnabar-soft)",
                      color: "var(--cinnabar)",
                    }}
                  >
                    {icon}
                  </span>
                  <span>{item}</span>
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </section>
  )
}
