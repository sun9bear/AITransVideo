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
    q: "为什么你们强调长视频？",
    a: "很多知识类内容不是几十秒短视频，而是 30 分钟、1 小时甚至 3 小时的访谈、课程、播客、演讲。我们的 Pro 套餐支持单条视频最长 180 分钟，正是为这类长内容做翻译和中文配音准备的。",
  },
  {
    q: "支持哪些源语言？",
    a: "现阶段聚焦海外英文长视频翻译为中文配音版本——访谈、课程、播客、产品演示这类内容效果最稳定。其他源语言（日语、西语等）正在评估中，暂未开放。如果你计划上传非英文素材，建议先与客服确认。",
  },
  {
    q: "支持哪些视频来源？",
    a: "支持您本人或已获授权内容的导入：本地视频文件上传、YouTube 链接（适用于您自己频道或已获授权的视频）、其他视频链接。使用前请确认您对相关视频拥有合法授权，详见《服务条款》。",
  },
  {
    q: "和 Rask、HeyGen、ElevenLabs 有什么区别？",
    a: "Rask、HeyGen、ElevenLabs 都是优秀的全球化平台，有些在数字人或声音克隆上非常强。爱译视频的重点不一样——我们更关注中文创作者处理海外英文长视频的实际需求：更长的视频时长、更低的使用门槛、多种结果导出，以及生成后还能逐句修改和单段重生成。",
  },
  {
    q: "如果 AI 翻译不满意怎么办？",
    a: "AI 生成的是第一版，不是最终版。你可以在工作台里逐句检查译文、字幕和配音，对不满意的句子直接修改，并单独重生成对应的片段。",
  },
  {
    q: "修改一句话需要重新生成整条视频吗？",
    a: "不需要。爱译视频支持增量重生成：你只改某一句时，系统只重新处理对应的片段，不会重复合成整条视频，节省时间也节省成本。",
  },
  {
    q: "Studio 模式和 Express 模式有什么区别？",
    a: "Express 模式自动跑完全流程，速度快、成本低，适合先验证效果或批量出片；Studio 模式可以在工作台里逐句复核译文、选择更合适的中文配音音色，质量更稳，更适合需要发布或交付客户的内容。在获得合法授权的前提下，Studio 也支持声音克隆能力。",
  },
  {
    q: "可以下载哪些结果？",
    a: "任务完成后可下载：中文配音视频、配音音频、中文字幕（含英文/双语字幕）、翻译文本、原始素材包。需要继续精剪时也可以尝试导出剪映草稿工程，在剪映里继续处理字幕、转场和细节（剪映草稿属于工作流增强能力，建议以实际导出结果为准）。",
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
  {
    q: "「失败不计费」具体指什么？",
    a: "如果任务因系统处理失败未生成可下载结果，不扣除对应处理额度；用户主动取消未完成的任务也不计费。注意「不满意」不属于此情形——已完成并交付的结果按时长正常计费，但你可以在工作台里逐句修改并单段重生成，避免重复付费跑全片。",
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
