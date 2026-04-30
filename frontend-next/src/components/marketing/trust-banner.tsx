import {
  ShieldCheck,
  CircleSlash,
  Repeat,
  CalendarClock,
  FileBadge,
  Receipt,
} from "lucide-react"
import { InkDivider } from "./ink-divider"

/**
 * Trust banner —集中爆发"不踩坑"信号 between Features and PricingPreview.
 *
 * See: docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md §5.2 第三幕「信任」
 *
 * Six promises, each: small icon + one-line guarantee. Designed for Chinese
 * SaaS buying-context anxieties: 自动扣费 / 失败计费 / 增量billing / 数据保留 /
 * 版权合规 / 退款.
 *
 * Hard rules:
 *   - Six items exactly. Adding a seventh is a yellow flag — usually means a
 *     bullet doesn't belong here. (TrustBanner is a "seal of confidence", not
 *     a feature list.)
 *   - Each line ≤ 18 Chinese chars. Longer lines = scanning fails.
 *   - Numbers (e.g. "7 天") are intentionally not Gateway-driven here because
 *     they're qualitative trust phrasing, not contract values. The actual
 *     Gateway-driven number lives in TrialBanner just below.
 */
const PROMISES = [
  {
    icon: ShieldCheck,
    title: "无需绑卡",
    detail: "试用结束不会自动扣费",
  },
  {
    icon: CircleSlash,
    title: "失败不计费",
    detail: "任务失败或取消零成本",
  },
  {
    icon: Repeat,
    title: "增量重生成",
    detail: "改一句只算一句的钱",
  },
  {
    icon: CalendarClock,
    title: "项目保留 7 天",
    detail: "到期前可随时下载",
  },
  {
    icon: FileBadge,
    title: "授权内容",
    detail: "仅处理您拥有合法授权的视频",
  },
  {
    icon: Receipt,
    title: "退款政策",
    detail: "条款公开，入口可达",
  },
] as const

export function TrustBanner() {
  return (
    <section className="marketing-reading-surface py-16 sm:py-20">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-2xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            为什么放心用
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            把六件事说清楚，不藏在条款里
          </h2>
        </div>

        <div className="mt-10">
          <InkDivider variant={1} className="text-foreground/40" />
        </div>

        <ul
          className="mt-12 grid grid-cols-1 gap-x-8 gap-y-10 sm:grid-cols-2 lg:grid-cols-3"
          role="list"
        >
          {PROMISES.map(({ icon: Icon, title, detail }) => (
            <li key={title} className="flex items-start gap-4">
              <span
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md"
                style={{
                  backgroundColor: "var(--cinnabar-soft)",
                  color: "var(--cinnabar)",
                }}
                aria-hidden="true"
              >
                <Icon className="h-5 w-5" strokeWidth={1.6} />
              </span>
              <div className="space-y-1">
                <p className="ink-heading text-base font-semibold text-foreground">
                  {title}
                </p>
                <p className="text-sm text-muted-foreground leading-relaxed">
                  {detail}
                </p>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
