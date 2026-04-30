/**
 * "How it works" section — a short four-step workflow explanation.
 * DESIGN.md §3.2 — between features and pricing.
 *
 * No mockup image yet; when Task 2 gets a real demo asset, drop it into the
 * right column. The placeholder below stays intentionally quiet (no dramatic
 * animation, no hero-style oversized type).
 */
const STEPS: Array<{ index: string; title: string; body: string }> = [
  {
    index: "01",
    title: "导入视频",
    body: "上传您本人或已获授权的视频（本地文件或视频链接），系统自动完成 ASR 转写与说话人分离。",
  },
  {
    index: "02",
    title: "翻译与配音",
    body: "选择目标语言与音色，支持 Express 快速模式和 Studio 精校模式。",
  },
  {
    index: "03",
    title: "人工复核",
    body: "在时间轴上逐句校对译文与配音，命中的问题句可以单独重新生成。",
  },
  {
    index: "04",
    title: "导出成片",
    body: "一键导出剪映草稿工程，在剪映中继续精调字幕、转场与细节。",
  },
]

export function WorkflowShowcase() {
  return (
    <section className="bg-muted/40 py-20 sm:py-24">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-2xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            工作流
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            从导入到导出，掌控每一个细节
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            四个步骤，每一步都可以人工介入，每一步都有明确的状态反馈。
          </p>
        </div>

        <ol className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-4" role="list">
          {STEPS.map((step) => (
            <li
              key={step.index}
              className="rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-md"
            >
              <span className="ink-num text-base font-bold text-[color:var(--cinnabar,#C73E3A)]">
                {step.index}
              </span>
              <h3 className="ink-heading mt-2 text-base font-semibold text-foreground">
                {step.title}
              </h3>
              <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                {step.body}
              </p>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}
