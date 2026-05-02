import { Clock, Package2, Pencil, RefreshCw, Coins, Film } from "lucide-react"

/**
 * Six core value proofs — re-prioritised 2026-04-30 per ChatGPT consult.
 *
 * Long-video support is now the lead because it's the most differentiated
 * thing the product does (most competitors cap at 30–60 min). 剪映草稿 has
 * been demoted to position #6 as a workflow assist — previously it was the
 * hero claim, but it overstated a still-stabilising feature and obscured
 * the bigger wins (length, editability, incremental re-gen, price).
 *
 * Tone is benefit-first, not feature-first. No "DSP 级帧级对齐" or other
 * technical claims that a) require validation we can't yet ship and b) read
 * as marketing aggrandisement to the actual user base (Chinese knowledge
 * creators). Concrete use-cases (访谈/课程/播客) replace abstract adjectives.
 */
const FEATURES: Array<{
  icon: typeof Clock
  title: string
  body: string
}> = [
  {
    icon: Clock,
    title: "支持真正的长视频",
    body: "最长支持 180 分钟单条视频，适合 1–3 小时的访谈、课程、播客、演讲、纪录片解读，不只适合几十秒短视频试水。",
  },
  {
    icon: Package2,
    title: "一次生成多种交付结果",
    body: "可下载中文配音视频、配音音频、中文字幕、翻译文本和原始素材包。直接发布或继续后期加工，按用途自由选择。",
  },
  {
    icon: Pencil,
    title: "工作台里逐句复核",
    body: "AI 生成的是第一版，不是最终版。在工作台里逐句查看译文、字幕和配音；人名、术语、语气、断句不满意都能改。",
  },
  {
    icon: RefreshCw,
    title: "不满意就局部重生成",
    body: "客户只改一句，不必整条视频重跑。哪句不顺，就改哪句、单独重生成那一段，省时间也省成本。",
  },
  {
    icon: Coins,
    title: "更适合中文创作者的价格",
    body: "用国内创作者更能接受的价格，覆盖长视频翻译、AI 配音和后期修改的全流程，高频出片也用得起。",
  },
  {
    icon: Film,
    title: "可继续进入剪辑流程",
    body: "需要精剪时可下载素材包，并尝试导出剪映草稿工程，在剪映里继续处理字幕、转场和音视频细节。",
  },
]

export function Features() {
  return (
    <section className="marketing-reading-surface py-14 sm:py-16">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-2xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            为什么选择爱译视频
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            把"翻译"做完，把"能发布"做对
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            AI 第一版只是起点。我们把长视频处理、多格式交付、逐句修改和增量重生成一起做对。
          </p>
        </div>

        <div className="mt-14 grid gap-6 md:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((f, i) => {
            const Icon = f.icon
            return (
              <article
                key={f.title}
                className="rounded-xl border border-border bg-card p-6 transition-shadow hover:shadow-md"
              >
                <div className="flex items-center gap-3">
                  <div
                    className="flex h-10 w-10 items-center justify-center rounded-lg"
                    style={{
                      backgroundColor: "var(--cinnabar-soft)",
                      color: "var(--cinnabar)",
                    }}
                  >
                    <Icon className="h-5 w-5" aria-hidden="true" />
                  </div>
                  <span
                    className="ink-num text-xs font-bold"
                    style={{ color: "var(--cinnabar)" }}
                  >
                    {String(i + 1).padStart(2, "0")}
                  </span>
                </div>
                <h3 className="ink-heading mt-5 text-lg font-semibold text-foreground">
                  {f.title}
                </h3>
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
