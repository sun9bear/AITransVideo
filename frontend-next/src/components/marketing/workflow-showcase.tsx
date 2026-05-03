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
    body: "上传本地视频，或粘贴你有权处理的视频链接。适合访谈、课程、播客、演讲、知识类长视频。",
  },
  {
    index: "02",
    title: "生成第一版",
    body: "系统自动完成语音识别、翻译、字幕生成和中文配音。先快速拿到一版可预览结果。",
  },
  {
    index: "03",
    title: "工作台精修",
    body: "逐句检查译文、字幕和配音。人名、术语、语气、断句不满意，直接修改并单独重生成。",
  },
  {
    index: "04",
    title: "下载结果",
    body: "下载中文配音视频、音频、字幕、素材包，或直接导出剪映草稿——在剪映里继续精剪不必从零铺时间线。",
  },
]

export function WorkflowShowcase() {
  return (
    <section className="bg-muted/40 py-14 sm:py-16">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-2xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            工作流
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            从英文视频到中文成片，四步完成
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            每一步都可以暂停、检查、修改。AI 生成第一版，你决定哪些片段需要再调一次。
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
