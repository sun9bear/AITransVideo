import { InkDivider } from "./ink-divider"

/**
 * Suited scenarios — "is this product for me?".
 *
 * Sits between Features and ToolComparison. Goal: high-代入感. After visitors
 * see the workflow + features, they need a beat to recognise themselves in
 * the use cases. Keeping this surface compact (5 short tag+body pairs) so it
 * reads as a sanity-check, not a sales pitch.
 *
 * Copy adapted from ChatGPT marketing consult (2026-05-01). Five buckets
 * intentionally chosen to span solo creators → MCN → B2B content teams.
 *
 * No icons — the typographic tag style keeps the section airy and aligned
 * with the ink-aesthetic. Each card is plain border + cinnabar tag + body.
 */
const SCENARIOS: Array<{ tag: string; body: string }> = [
  {
    tag: "知识博主",
    body: "把海外访谈、播客、课程翻译成中文配音版本，让长内容跨过语言门槛继续传播。",
  },
  {
    tag: "视频号 / 抖音创作者",
    body: "快速本地化海外英文长视频素材，保留原片节奏、人物语气和讲解结构。",
  },
  {
    tag: "教育培训团队",
    body: "把英文课程、讲座、公开课转成中文学习内容，配齐字幕和讲师配音。",
  },
  {
    tag: "MCN / 内容工作室",
    body: "批量处理长视频，降低翻译和配音成本，统一团队的中文化交付标准。",
  },
  {
    tag: "企业内容团队",
    body: "处理海外发布会、产品演示、客户案例视频，输出可发布的中文版本。",
  },
]

export function SuitedScenarios() {
  return (
    <section className="marketing-reading-surface py-20 sm:py-24">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-3xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            适合这些场景
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            谁在用爱译视频做长视频本地化
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            如果你的工作流里有海外英文长视频要持续翻译配音、还要逐条复核交付，下面这些场景多半也是你的日常。
          </p>
        </div>

        <div className="mt-10">
          <InkDivider variant={2} className="text-foreground/35" />
        </div>

        <ul
          className="mt-12 grid gap-5 md:grid-cols-2 lg:grid-cols-3"
          role="list"
        >
          {SCENARIOS.map((s) => (
            <li
              key={s.tag}
              className="rounded-xl border border-border bg-card p-6 transition-shadow hover:shadow-md"
            >
              <span
                className="ink-heading inline-flex items-center rounded-md px-2.5 py-1 text-xs font-semibold uppercase tracking-wider"
                style={{
                  backgroundColor: "var(--cinnabar-soft)",
                  color: "var(--cinnabar)",
                }}
              >
                {s.tag}
              </span>
              <p className="mt-4 zh-body text-sm leading-relaxed text-muted-foreground">
                {s.body}
              </p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
