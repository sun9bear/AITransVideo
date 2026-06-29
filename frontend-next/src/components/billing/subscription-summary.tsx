"use client"

/**
 * Subscription summary block (Task 6).
 *
 * Extracted from the T5 in-page `SubscriptionSnapshot` helper into a standalone
 * component under `components/billing/` so the page can compose it next to the
 * new billing status banner and order history.
 *
 * Rules:
 * - Never invent numeric trial facts. If `trial.ends_at` is null we do NOT
 *   render a countdown; we just report that the trial was granted.
 * - Never imply auto-renew, cancel, or refund capabilities that do not exist
 *   yet. The copy stays workmanlike and factual.
 * - Never map trial to `plus` / `pro` here — trial bookkeeping is its own
 *   slot, distinct from paid subscription.
 */

import type { MeSubscriptionResponse } from "@/lib/billing/get-subscription"
import { useIntlLocale } from "@/lib/intl-locale"

const PERIOD_LABELS: Record<string, string> = {
  monthly: "月付",
  quarterly: "季付",
  annual: "年付",
}

const PROVIDER_LABELS: Record<string, string> = {
  fake: "测试支付",
  alipay: "支付宝",
  wechatpay: "微信支付",
  stripe: "Stripe",
}

function formatDate(iso: string | null, formatLocale: string): string {
  if (!iso) return "--"
  try {
    const d = new Date(iso)
    return d.toLocaleDateString(formatLocale, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    })
  } catch {
    return "--"
  }
}

function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider
}

function periodLabel(period: string): string {
  return PERIOD_LABELS[period] ?? period
}

function TrialLine({
  granted_at,
  ends_at,
}: {
  granted_at: string | null
  ends_at: string | null
}) {
  const formatLocale = useIntlLocale()
  // No granted_at → user has never gone through the phone-auth trial-grant
  // path. Render nothing; the summary still reads cleanly.
  if (!granted_at) return null

  if (!ends_at) {
    return (
      <p className="mt-3 rounded-md border border-dashed border-border bg-muted/30 p-3 text-xs text-muted-foreground">
        试用已于 {formatDate(granted_at, formatLocale)} 发放。
      </p>
    )
  }

  return (
    <p className="mt-3 rounded-md border border-dashed border-border bg-muted/30 p-3 text-xs text-muted-foreground">
      试用已于 {formatDate(granted_at, formatLocale)} 发放,到期时间 {formatDate(ends_at, formatLocale)}。
    </p>
  )
}

export function SubscriptionSummary({
  subscription,
}: {
  subscription: MeSubscriptionResponse
}) {
  const active = subscription.subscription
  const formatLocale = useIntlLocale()

  if (subscription.subscription_status !== "active" || !active) {
    return (
      <section
        aria-label="当前订阅"
        className="rounded-lg border border-border bg-card p-6"
      >
        <h2 className="text-base font-semibold text-foreground">当前订阅</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          你还没有付费订阅。当前账户按
          <span className="mx-1 font-medium text-foreground">
            {subscription.plan_code.toUpperCase()}
          </span>
          套餐运行。
        </p>
        <TrialLine
          granted_at={subscription.trial.granted_at}
          ends_at={subscription.trial.ends_at}
        />
      </section>
    )
  }

  return (
    <section
      aria-label="当前订阅"
      className="rounded-lg border border-border bg-card p-6"
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-foreground">当前订阅</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            以下信息为服务器实际记录,不含自动续费承诺。
          </p>
        </div>
        <span className="inline-flex items-center rounded-full border border-primary/30 bg-primary/5 px-2.5 py-0.5 text-xs font-medium text-primary">
          {active.status === "active" ? "生效中" : active.status}
        </span>
      </div>

      <dl className="mt-5 grid gap-4 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-xs text-muted-foreground">套餐</dt>
          <dd className="mt-0.5 font-medium text-foreground">
            {active.plan_code.toUpperCase()}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">计费周期</dt>
          <dd className="mt-0.5 font-medium text-foreground">
            {periodLabel(active.billing_period)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">支付方式</dt>
          <dd className="mt-0.5 font-medium text-foreground">
            {providerLabel(active.provider)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">本期开始</dt>
          <dd className="mt-0.5 font-medium text-foreground">
            {formatDate(active.current_period_start, formatLocale)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">本期结束</dt>
          <dd className="mt-0.5 font-medium text-foreground">
            {formatDate(active.current_period_end, formatLocale)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">订阅起始</dt>
          <dd className="mt-0.5 font-medium text-foreground">
            {formatDate(active.started_at, formatLocale)}
          </dd>
        </div>
      </dl>

      <TrialLine
        granted_at={subscription.trial.granted_at}
        ends_at={subscription.trial.ends_at}
      />
    </section>
  )
}
