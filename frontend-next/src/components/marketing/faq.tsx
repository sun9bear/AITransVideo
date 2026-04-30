/**
 * FAQ section. DESIGN.md §3.5 — pricing / FAQ surfaces should stay on neutral,
 * high-contrast reading backgrounds for dense Chinese text.
 *
 * Question order follows plan §5.2 第三幕「信任」: pre-purchase doubts come first
 * (视频来源 → Express vs Studio), then post-purchase mechanics (增量重生成 →
 * 导出格式). The reordering matches the conversion funnel — visitors hesitate on
 * "can I even use this?" before "how do I get my output back?".
 *
 * Answers intentionally avoid any numeric trial facts (days / minutes / Studio
 * inclusion) while those values remain unfrozen in the gateway catalog.
 * Answers that reference plan-specific numbers should be added after Task 0's
 * trial config is flipped to `frozen = true`.
 */
type FaqItem = { q: string; a: string }

const GENERAL_FAQ: FaqItem[] = [
  {
    q: "支持哪些视频来源？",
    a: "支持您本人或已获授权内容的导入：本地视频文件上传、YouTube 链接（适用于您自己频道或已获授权的视频）、其他视频链接。使用前请确认您对相关视频拥有合法授权，详见《服务条款》。",
  },
  {
    q: "Studio 模式和 Express 模式有什么区别？",
    a: "Express 模式速度快、成本低，适合快速出片与预览；Studio 模式在音色、节奏、情感表达上更接近专业录音，可启用人工复核。",
  },
  {
    q: "什么是“增量重生成”？",
    a: "如果只改了某一句台词，系统只重新合成那一段音频和时间戳，不会重复生成整条片子，计费也只按增量走。",
  },
  {
    q: "可以导出成哪些格式？",
    a: "除了常规的 MP4 与字幕文件，我们会同时生成剪映草稿工程，在剪映中可以继续精调字幕、转场、特效。",
  },
]

const PRICING_FAQ: FaqItem[] = [
  ...GENERAL_FAQ,
  {
    q: "试用结束后会怎样？",
    a: "试用结束后不会自动扣费。你可以继续以 Free 套餐的免费额度使用，也可以主动升级到 Plus 或 Pro。",
  },
  {
    q: "视频时长怎么计算？",
    a: "按源视频的实际时长统计。处理完成后，对应时长会计入当期额度；任务失败或取消不会计费。",
  },
]

export function Faq({ variant = "home" }: { variant?: "home" | "pricing" }) {
  const items = variant === "pricing" ? PRICING_FAQ : GENERAL_FAQ
  return (
    <section className="marketing-reading-surface py-20 sm:py-24">
      <div className="mx-auto max-w-3xl px-4 sm:px-6 lg:px-8">
        <div className="text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            常见问题
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            你可能想知道
          </h2>
        </div>
        <dl className="mt-12 space-y-4">
          {items.map((item) => (
            <div
              key={item.q}
              className="rounded-xl border border-border bg-card p-6 transition-shadow hover:shadow-md"
            >
              <dt className="ink-heading text-base font-semibold text-foreground">{item.q}</dt>
              <dd className="mt-2 zh-body text-muted-foreground">{item.a}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  )
}
