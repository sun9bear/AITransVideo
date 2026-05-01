import { InkDivider } from "./ink-divider"

/**
 * Pain points section — sits between Hero and ProductProof.
 *
 * Recasts the conversion narrative around what users actually struggle with
 * when localising long-form videos: length, fragility of edits, narrow
 * output formats, and runaway cost. Each bullet leads with a "用户在哪卡住"
 * line and lands on the implicit "我们正好把这件事做了" close.
 *
 * Replaces the previous in-Hero 钩子 ("口型对不上 / 改一句要重跑全片 /
 * 剪辑师拿到的是死字幕") which over-promised lip-sync and over-emphasised
 * the 剪映 export angle. New copy is honest about what AI翻译配音 actually
 * fixes and what users still control.
 */
const POINTS: Array<{ title: string; body: string }> = [
  {
    title: "视频太长",
    body: "很多工具更适合几十秒到几分钟的短视频试水；一旦遇到 30 分钟、1 小时甚至 3 小时的访谈、课程和播客，处理成本和等待时间都会迅速上升。",
  },
  {
    title: "结果不好改",
    body: "AI 第一版总会有人名、术语、断句、语气的问题。改一句却常常要把整条视频重新跑一遍，时间和成本同时浪费。",
  },
  {
    title: "交付物太单一",
    body: "只拿到一个成片不够。创作者还需要中文字幕、配音音频、原始素材包，方便继续剪辑、二次发布或客户审稿。",
  },
  {
    title: "成本不好控",
    body: "海外工具效果不错，但分钟数、修改次数、增加段落都会快速堆积费用。高频出片时一个月账单常常超出预期。",
  },
]

export function PainPoints() {
  return (
    <section className="marketing-reading-surface py-20 sm:py-24">
      <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-3xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar)]">
            为什么需要专门的工作台
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            海外长视频翻译，真正麻烦的是后面 80%
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            面向英文源、中文输出。AI 生成第一版不难，难的是把它真的改到可以发布。
          </p>
        </div>

        <div className="mt-10 mx-auto max-w-3xl">
          <InkDivider variant={1} className="text-foreground/35" />
        </div>

        <ol className="mt-12 grid gap-5 md:grid-cols-2" role="list">
          {POINTS.map((p, i) => (
            <li
              key={p.title}
              className="rounded-xl border border-border bg-card p-6 transition-shadow hover:shadow-md"
            >
              <div className="flex items-start gap-3">
                <span
                  className="ink-num shrink-0 text-base font-bold"
                  style={{ color: "var(--cinnabar)" }}
                >
                  {String(i + 1).padStart(2, "0")}
                </span>
                <div>
                  <h3 className="ink-heading text-lg font-semibold text-foreground">
                    {p.title}
                  </h3>
                  <p className="mt-2 zh-body text-sm leading-relaxed text-muted-foreground">
                    {p.body}
                  </p>
                </div>
              </div>
            </li>
          ))}
        </ol>

        <p className="mx-auto mt-12 max-w-3xl text-center zh-body text-foreground/85">
          爱译视频不是只做"一键生成"，而是把<strong className="text-foreground">长视频翻译、AI 配音、多格式交付和逐句修改</strong>放进同一个工作台。
        </p>
      </div>
    </section>
  )
}
