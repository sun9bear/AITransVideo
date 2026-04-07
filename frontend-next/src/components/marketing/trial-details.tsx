"use client"

import { Check, Lock, CreditCard, HelpCircle } from "lucide-react"
import { usePlans } from "./use-plans"

/**
 * Trial benefits panel used on `/trial`.
 *
 * Copy boundary — DESIGN.md §3.6 + T2 preflight:
 *
 * - While `trial.frozen === false`, this component shows qualitative trust cues
 *   and a neutral user-facing note about reviewing exact allowances after
 *   sign-up. It must NOT mention days, minutes, Studio inclusion, a specific
 *   tier name (e.g. "Plus"), or internal-sounding phrases like "仍在最终确认中".
 * - When the gateway flips trial.frozen to true and adds fields like `days` /
 *   `source_minutes` / `phone_required`, extend this file to render those
 *   fields from the API response. Never hardcode fallback numbers here.
 */
const QUALITATIVE_BENEFITS: Array<{ icon: typeof Check; title: string; body: string }> = [
  {
    icon: Check,
    title: "体验完整工作流",
    body: "从导入、翻译、配音、复核到导出剪映草稿,每一步都可以亲手走一遍。",
  },
  {
    icon: Lock,
    title: "项目安全保留",
    body: "试用期间产生的项目数据保留在你的账户中,升级后可以无缝继续编辑。",
  },
  {
    icon: CreditCard,
    title: "无需绑卡",
    body: "不需要提前绑定支付方式,也不会有任何隐藏扣费。",
  },
  {
    icon: HelpCircle,
    title: "结束后会怎样?",
    body: "试用到期后不会自动扣费。你可以继续使用账户中的免费额度,或在需要时主动升级。",
  },
]

export function TrialDetails() {
  const state = usePlans()
  const trial = state.status === "ready" ? state.data.trial : null
  const frozen = trial?.frozen === true

  return (
    <div className="space-y-8">
      <ul className="space-y-5" role="list">
        {QUALITATIVE_BENEFITS.map((b) => {
          const Icon = b.icon
          return (
            <li key={b.title} className="flex gap-4">
              <div className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Icon className="h-4 w-4" aria-hidden="true" />
              </div>
              <div>
                <h3 className="text-base font-semibold text-foreground">{b.title}</h3>
                <p className="mt-1 zh-body text-muted-foreground text-[0.95rem]">{b.body}</p>
              </div>
            </li>
          )
        })}
      </ul>

      {/* Trial allowance — renders frozen numeric facts from the API. */}
      {state.status === "ready" && frozen && trial && (
        <div className="rounded-lg border border-primary/20 bg-primary/5 p-4">
          <p className="text-sm font-medium text-foreground">试用权益</p>
          <ul className="mt-2 space-y-1 text-sm text-muted-foreground">
            {trial.days && <li>· 试用时长：{trial.days} 天</li>}
            {trial.source_minutes && <li>· 源视频额度：{trial.source_minutes} 分钟</li>}
            {trial.includes_studio && <li>· 包含 Studio 精校模式</li>}
            <li>· 试用结束不会自动扣费</li>
          </ul>
        </div>
      )}
      {state.status === "ready" && !frozen && (
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="text-sm text-muted-foreground">
            注册后即可查看并领取你的试用额度。
          </p>
        </div>
      )}
    </div>
  )
}
