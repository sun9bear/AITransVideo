import { Waves, FileCode2, RefreshCw } from "lucide-react"

/**
 * Three core value proofs — the "why we're not a generic AI template" section.
 * DESIGN.md §3.2: feature explanation block between hero and pricing.
 *
 * Intentionally concrete and benefit-led. No abstract adjectives.
 */
const FEATURES: Array<{
  icon: typeof Waves
  title: string
  body: string
}> = [
  {
    icon: Waves,
    title: "DSP 级精准对齐",
    body: "基于数字信号处理的帧级对齐，不是简单的 LLM 时间戳猜测，口型与节奏更自然。",
  },
  {
    icon: FileCode2,
    title: "工程化输出",
    body: "直接导出剪映草稿工程，保留分轨信息与时间码，后续精调零摩擦。",
  },
  {
    icon: RefreshCw,
    title: "增量重生成",
    body: "改一句台词，只重新生成那一段。计费按增量走，不为整条片子再付一次钱。",
  },
]

export function Features() {
  return (
    <section className="marketing-reading-surface py-20 sm:py-24">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-2xl text-center">
          <p className="text-xs font-semibold uppercase tracking-wider text-primary">
            为什么选择 AIVideoTrans
          </p>
          <h2 className="mt-3 text-3xl font-bold tracking-tight text-foreground sm:text-4xl">
            专为专业创作者打造
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            告别口型错位与生硬机翻。我们把对齐、导出与成本这三件最难的事做对。
          </p>
        </div>

        <div className="mt-14 grid gap-6 md:grid-cols-3">
          {FEATURES.map((f) => {
            const Icon = f.icon
            return (
              <article
                key={f.title}
                className="rounded-xl border border-border bg-card p-6 transition-shadow hover:shadow-md"
              >
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <Icon className="h-5 w-5" aria-hidden="true" />
                </div>
                <h3 className="mt-5 text-lg font-semibold text-foreground">{f.title}</h3>
                <p className="mt-2 zh-body text-muted-foreground text-[0.95rem]">
                  {f.body}
                </p>
              </article>
            )
          })}
        </div>
      </div>
    </section>
  )
}
