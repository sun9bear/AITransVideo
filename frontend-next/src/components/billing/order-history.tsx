"use client"

/**
 * Recent billing history (Task 6).
 *
 * Shows the current user's recent invoices newest-first. Strictly minimal:
 * - no pagination framework (we render whatever the API returns)
 * - no filters / search
 * - no CSV / PDF export
 * - no refund / cancel / invoice-download actions
 * - no admin tenant-wide variant
 *
 * Fetches its own data so the parent page can mount it independently of the
 * subscription summary. If the history endpoint 500s, the component renders
 * a calm inline error state rather than crashing the whole page.
 */

import { useEffect, useState } from "react"
import { useTranslations } from "next-intl"
import { FileText, Loader2 } from "lucide-react"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"
import {
  getBillingHistory,
  type BillingInvoice,
} from "@/lib/billing/get-order-history"
import { useIntlLocale } from "@/lib/intl-locale"

/** Translator scoped to the `appBilling` namespace (relative keys). */
type BillingTranslator = ReturnType<typeof useTranslations<"appBilling">>
/** Literal message-key type for appBilling — lets label-key maps stay typed. */
type BillingKey = Parameters<BillingTranslator>[0]

type State =
  | { status: "loading" }
  | { status: "ready"; invoices: BillingInvoice[] }
  | { status: "error"; message: string }

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
  paypal: "provider.paypal",
}

// Status → { label key, tone }. The label is resolved at render time via the
// active translator; tone drives the pill colour (purely presentational).
const STATUS_META: Record<
  string,
  { labelKey: BillingKey; tone: "ok" | "warn" | "error" | "neutral" }
> = {
  paid: { labelKey: "history.status.paid", tone: "ok" },
  failed: { labelKey: "history.status.failed", tone: "error" },
  refunded: { labelKey: "history.status.refunded", tone: "warn" },
}

function formatYuan(amountFen: number, currency: string): string {
  const yuan = amountFen / 100
  const body = yuan % 1 === 0 ? yuan.toFixed(0) : yuan.toFixed(2)
  if (currency === "CNY") return `¥${body}`
  return `${body} ${currency}`
}

function formatInvoiceAmount(inv: BillingInvoice): string {
  // PayPal invoices are charged in USD — show the actual USD amount the buyer
  // paid. amount_cny stays the canonical ledger value used by other rails.
  if (inv.provider === "paypal" && inv.charged_usd_cents != null) {
    return `$${(inv.charged_usd_cents / 100).toFixed(2)}`
  }
  return formatYuan(inv.amount_cny, inv.currency)
}

function formatDateTime(iso: string | null, formatLocale: string): string {
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

function StatusPill({ status, t }: { status: string; t: BillingTranslator }) {
  const meta = STATUS_META[status]
  const label = meta ? t(meta.labelKey) : status
  const tone = meta?.tone ?? "neutral"
  const colorClass =
    tone === "ok"
      ? "border-primary/30 bg-primary/5 text-primary"
      : tone === "error"
        ? "border-destructive/40 bg-destructive/5 text-destructive"
        : tone === "warn"
          ? "border-amber-500/40 bg-amber-500/5 text-amber-600 dark:text-amber-400"
          : "border-border bg-muted/30 text-muted-foreground"
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium",
        colorClass,
      )}
    >
      {label}
    </span>
  )
}

function EmptyState({ t }: { t: BillingTranslator }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-md border border-dashed border-border bg-muted/20 py-10 text-center">
      <FileText
        className="h-8 w-8 text-muted-foreground/60"
        aria-hidden="true"
      />
      <p className="mt-3 text-sm font-medium text-foreground">{t("history.emptyTitle")}</p>
      <p className="mt-1 text-xs text-muted-foreground">
        {t("history.emptyBody")}
      </p>
    </div>
  )
}

function LoadingState() {
  return (
    <div className="space-y-2">
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-10 w-full" />
    </div>
  )
}

export function OrderHistory() {
  const t = useTranslations("appBilling")
  const [state, setState] = useState<State>({ status: "loading" })
  const formatLocale = useIntlLocale()

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await getBillingHistory()
        if (cancelled) return
        setState({ status: "ready", invoices: res.invoices ?? [] })
      } catch (err) {
        if (cancelled) return
        const message = err instanceof Error ? err.message : t("history.loadError")
        setState({ status: "error", message })
      }
    })()
    return () => {
      cancelled = true
    }
  }, [t])

  return (
    <section
      aria-label={t("history.sectionLabel")}
      className="rounded-lg border border-border bg-card p-6"
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-foreground">{t("history.title")}</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {t("history.subtitle")}
          </p>
        </div>
      </div>

      <div className="mt-5">
        {state.status === "loading" && <LoadingState />}
        {state.status === "error" && (
          <div className="flex items-center gap-2 rounded-md border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span>{t("history.loadFailedInline")}</span>
          </div>
        )}
        {state.status === "ready" && state.invoices.length === 0 && (
          <EmptyState t={t} />
        )}
        {state.status === "ready" && state.invoices.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
                  <th className="py-2 pr-4 font-medium">{t("history.colTime")}</th>
                  <th className="py-2 pr-4 font-medium">{t("history.colPlan")}</th>
                  <th className="py-2 pr-4 font-medium">{t("history.colPeriod")}</th>
                  <th className="py-2 pr-4 font-medium">{t("history.colProvider")}</th>
                  <th className="py-2 pr-4 font-medium">{t("history.colStatus")}</th>
                  <th className="py-2 text-right font-medium">{t("history.colAmount")}</th>
                </tr>
              </thead>
              <tbody>
                {state.invoices.map((inv) => (
                  <tr
                    key={inv.id}
                    className="border-b border-border/60 last:border-0"
                  >
                    <td className="py-3 pr-4 text-foreground">
                      {formatDateTime(inv.paid_at ?? inv.created_at ?? inv.issued_at, formatLocale)}
                    </td>
                    <td className="py-3 pr-4 font-medium text-foreground">
                      {inv.plan_code.toUpperCase()}
                    </td>
                    <td className="py-3 pr-4 text-muted-foreground">
                      {PERIOD_LABEL_KEYS[inv.billing_period]
                        ? t(PERIOD_LABEL_KEYS[inv.billing_period])
                        : inv.billing_period}
                    </td>
                    <td className="py-3 pr-4 text-muted-foreground">
                      {PROVIDER_LABEL_KEYS[inv.provider]
                        ? t(PROVIDER_LABEL_KEYS[inv.provider])
                        : inv.provider}
                    </td>
                    <td className="py-3 pr-4">
                      <StatusPill status={inv.status} t={t} />
                    </td>
                    <td className="py-3 text-right font-medium tabular-nums text-foreground">
                      {formatInvoiceAmount(inv)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}
