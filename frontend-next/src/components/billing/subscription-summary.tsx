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

import { useTranslations } from "next-intl"
import type { MeSubscriptionResponse } from "@/lib/billing/get-subscription"
import { useIntlLocale } from "@/lib/intl-locale"

/** Translator scoped to the `appBilling` namespace (relative keys). */
type BillingTranslator = ReturnType<typeof useTranslations<"appBilling">>
/** Literal message-key type for appBilling — lets label-key maps stay typed. */
type BillingKey = Parameters<BillingTranslator>[0]

const PERIOD_LABEL_KEYS: Record<string, BillingKey> = {
  monthly: "period.monthly",
  quarterly: "period.quarterly",
  annual: "period.annual",
}

const PROVIDER_LABEL_KEYS: Record<string, BillingKey> = {
  fake: "provider.fake",
  alipay: "provider.alipay",
  wechatpay: "provider.wechatpay",
  stripe: "provider.stripe",
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

function providerLabel(t: BillingTranslator, provider: string): string {
  const key = PROVIDER_LABEL_KEYS[provider]
  return key ? t(key) : provider
}

function periodLabel(t: BillingTranslator, period: string): string {
  const key = PERIOD_LABEL_KEYS[period]
  return key ? t(key) : period
}

function TrialLine({
  granted_at,
  ends_at,
}: {
  granted_at: string | null
  ends_at: string | null
}) {
  const t = useTranslations("appBilling")
  const formatLocale = useIntlLocale()
  // No granted_at → user has never gone through the phone-auth trial-grant
  // path. Render nothing; the summary still reads cleanly.
  if (!granted_at) return null

  if (!ends_at) {
    return (
      <p className="mt-3 rounded-md border border-dashed border-border bg-muted/30 p-3 text-xs text-muted-foreground">
        {t("subscription.trialGranted", {
          date: formatDate(granted_at, formatLocale),
        })}
      </p>
    )
  }

  return (
    <p className="mt-3 rounded-md border border-dashed border-border bg-muted/30 p-3 text-xs text-muted-foreground">
      {t("subscription.trialGrantedWithEnd", {
        granted: formatDate(granted_at, formatLocale),
        ends: formatDate(ends_at, formatLocale),
      })}
    </p>
  )
}

export function SubscriptionSummary({
  subscription,
}: {
  subscription: MeSubscriptionResponse
}) {
  const t = useTranslations("appBilling")
  const active = subscription.subscription
  const formatLocale = useIntlLocale()

  if (subscription.subscription_status !== "active" || !active) {
    return (
      <section
        aria-label={t("subscription.sectionLabel")}
        className="rounded-lg border border-border bg-card p-6"
      >
        <h2 className="text-base font-semibold text-foreground">{t("subscription.title")}</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          {t("subscription.noneBody")}
          <span className="mx-1 font-medium text-foreground">
            {subscription.plan_code.toUpperCase()}
          </span>
          {t("subscription.planRunSuffix")}
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
      aria-label={t("subscription.sectionLabel")}
      className="rounded-lg border border-border bg-card p-6"
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-foreground">{t("subscription.title")}</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {t("subscription.subtitle")}
          </p>
        </div>
        <span className="inline-flex items-center rounded-full border border-primary/30 bg-primary/5 px-2.5 py-0.5 text-xs font-medium text-primary">
          {active.status === "active" ? t("subscription.active") : active.status}
        </span>
      </div>

      <dl className="mt-5 grid gap-4 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-xs text-muted-foreground">{t("subscription.planLabel")}</dt>
          <dd className="mt-0.5 font-medium text-foreground">
            {active.plan_code.toUpperCase()}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("subscription.periodLabel")}</dt>
          <dd className="mt-0.5 font-medium text-foreground">
            {periodLabel(t, active.billing_period)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("subscription.providerLabel")}</dt>
          <dd className="mt-0.5 font-medium text-foreground">
            {providerLabel(t, active.provider)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("subscription.periodStart")}</dt>
          <dd className="mt-0.5 font-medium text-foreground">
            {formatDate(active.current_period_start, formatLocale)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("subscription.periodEnd")}</dt>
          <dd className="mt-0.5 font-medium text-foreground">
            {formatDate(active.current_period_end, formatLocale)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("subscription.subscriptionStart")}</dt>
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
