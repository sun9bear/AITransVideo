import { Check, Minus } from "lucide-react"
import { InkDivider } from "./ink-divider"

/**
 * Tool comparison — "为什么比一键生成工具更适合长视频".
 *
 * Sits after SuitedScenarios. Goal: clarify positioning vs the broader class
 * of "AI video translation" tools without naming any competitor by brand
 * (FAQ already references Rask/HeyGen/ElevenLabs by name; this surface
 * intentionally stays category-level so it's not read as an attack).
 *
 * Four-row table is rendered as a 2-column card grid on desktop + stacked on
 * mobile — keeps both columns equally readable at any width. Each row is the
 * same dimension shown on both sides so the contrast stays clean.
 *
 * Copy from ChatGPT marketing consult (2026-05-01).
 */
const ROWS: Array<{
  dimension: string
  oneClick: string
  workbench: string
}> = [
  {
    dimension: "适用视频长度",
    oneClick: "更适合短视频试水（几十秒到几分钟）",
    workbench: "支持 1–3 小时长视频，针对访谈、课程、播客优化",
  },
  {
    dimension: "结果是否能修改",
    oneClick: "生成后基本只能整条重跑，修改成本高",
    workbench: "工作台逐句复核译文和配音，单段重生成",
  },
  {
    dimension: "交付物种类",
    oneClick: "通常只拿到一个成片",
    workbench: "配音视频 / 配音音频 / 字幕 / 翻译文本 / 素材包 / 剪映草稿",
  },
  {
    dimension: "成本可控性",
    oneClick: "改一句要重跑全片，分钟数堆积快",
    workbench: "改一句只算一句的钱，按时长精细计费",
  },
]

export function ToolComparison() {
  return (
    <section className="marketing-reading-surface py-20 sm:py-24">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-3xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            和一键生成工具的差异
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            一键生成 vs 长视频翻译配音工作台
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            这不是评测竞品，而是说明两种产品形态。短视频试水可以选一键生成；持续做长视频本地化、需要复核和精修，工作台模式更省时省钱。
          </p>
        </div>

        <div className="mt-10">
          <InkDivider variant={0} className="text-foreground/35" />
        </div>

        {/* Header row — only visible at md+ to anchor the columns. On mobile
            each row already labels its two halves, so the header would be
            redundant and crowd the viewport. */}
        <div className="mt-12 hidden grid-cols-[1fr_1.4fr_1.4fr] gap-4 px-2 text-xs font-semibold uppercase tracking-widest text-muted-foreground md:grid">
          <div>对比维度</div>
          <div className="text-foreground/85">一键生成工具</div>
          <div
            className="ink-heading"
            style={{ color: "var(--cinnabar)" }}
          >
            爱译视频工作台
          </div>
        </div>

        <ul className="mt-4 space-y-3" role="list">
          {ROWS.map((row) => (
            <li
              key={row.dimension}
              className="grid gap-4 rounded-xl border border-border bg-card p-5 md:grid-cols-[1fr_1.4fr_1.4fr] md:items-start md:p-6"
            >
              <div className="ink-heading text-sm font-semibold text-foreground md:text-base">
                {row.dimension}
              </div>
              <div className="flex items-start gap-2 text-sm text-muted-foreground">
                <Minus
                  className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground/60"
                  aria-hidden="true"
                />
                <div>
                  <span className="md:hidden text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    一键生成工具：
                  </span>
                  <span className="zh-body leading-relaxed">{row.oneClick}</span>
                </div>
              </div>
              <div
                className="flex items-start gap-2 text-sm"
                style={{ color: "var(--ink-black, #1A1A1A)" }}
              >
                <Check
                  className="mt-0.5 h-4 w-4 shrink-0"
                  style={{ color: "var(--cinnabar)" }}
                  aria-hidden="true"
                />
                <div>
                  <span
                    className="md:hidden text-xs font-semibold uppercase tracking-wider"
                    style={{ color: "var(--cinnabar)" }}
                  >
                    爱译视频工作台：
                  </span>
                  <span className="zh-body leading-relaxed text-foreground/90">
                    {row.workbench}
                  </span>
                </div>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
