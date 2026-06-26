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
import { FileText, Loader2 } from "lucide-react"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"
import {
  getBillingHistory,
  type BillingInvoice,
} from "@/lib/billing/get-order-history"

type State =
  | { status: "loading" }
  | { status: "ready"; invoices: BillingInvoice[] }
  | { status: "error"; message: string }

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
  paypal: "PayPal",
}

const STATUS_META: Record<
  string,
  { label: string; tone: "ok" | "warn" | "error" | "neutral" }
> = {
  paid: { label: "已支付", tone: "ok" },
  failed: { label: "失败", tone: "error" },
  refunded: { label: "已退款", tone: "warn" },
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

function formatDateTime(iso: string | null): string {
  if (!iso) return "--"
  try {
    const d = new Date(iso)
    return d.toLocaleDateString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    })
  } catch {
    return "--"
  }
}

function StatusPill({ status }: { status: string }) {
  const meta = STATUS_META[status] ?? { label: status, tone: "neutral" as const }
  const colorClass =
    meta.tone === "ok"
      ? "border-primary/30 bg-primary/5 text-primary"
      : meta.tone === "error"
        ? "border-destructive/40 bg-destructive/5 text-destructive"
        : meta.tone === "warn"
          ? "border-amber-500/40 bg-amber-500/5 text-amber-600 dark:text-amber-400"
          : "border-border bg-muted/30 text-muted-foreground"
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium",
        colorClass,
      )}
    >
      {meta.label}
    </span>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center rounded-md border border-dashed border-border bg-muted/20 py-10 text-center">
      <FileText
        className="h-8 w-8 text-muted-foreground/60"
        aria-hidden="true"
      />
      <p className="mt-3 text-sm font-medium text-foreground">暂无账单记录</p>
      <p className="mt-1 text-xs text-muted-foreground">
        完成首次付费后,账单会在这里出现。
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
  const [state, setState] = useState<State>({ status: "loading" })

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await getBillingHistory()
        if (cancelled) return
        setState({ status: "ready", invoices: res.invoices ?? [] })
      } catch (err) {
        if (cancelled) return
        const message = err instanceof Error ? err.message : "加载账单失败"
        setState({ status: "error", message })
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <section
      aria-label="账单历史"
      className="rounded-lg border border-border bg-card p-6"
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-foreground">账单历史</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            显示你的付费记录,按创建时间倒序排列。
          </p>
        </div>
      </div>

      <div className="mt-5">
        {state.status === "loading" && <LoadingState />}
        {state.status === "error" && (
          <div className="flex items-center gap-2 rounded-md border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span>账单暂时无法加载,请稍后重试。</span>
          </div>
        )}
        {state.status === "ready" && state.invoices.length === 0 && (
          <EmptyState />
        )}
        {state.status === "ready" && state.invoices.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
                  <th className="py-2 pr-4 font-medium">时间</th>
                  <th className="py-2 pr-4 font-medium">套餐</th>
                  <th className="py-2 pr-4 font-medium">周期</th>
                  <th className="py-2 pr-4 font-medium">渠道</th>
                  <th className="py-2 pr-4 font-medium">状态</th>
                  <th className="py-2 text-right font-medium">金额</th>
                </tr>
              </thead>
              <tbody>
                {state.invoices.map((inv) => (
                  <tr
                    key={inv.id}
                    className="border-b border-border/60 last:border-0"
                  >
                    <td className="py-3 pr-4 text-foreground">
                      {formatDateTime(inv.paid_at ?? inv.created_at ?? inv.issued_at)}
                    </td>
                    <td className="py-3 pr-4 font-medium text-foreground">
                      {inv.plan_code.toUpperCase()}
                    </td>
                    <td className="py-3 pr-4 text-muted-foreground">
                      {PERIOD_LABELS[inv.billing_period] ?? inv.billing_period}
                    </td>
                    <td className="py-3 pr-4 text-muted-foreground">
                      {PROVIDER_LABELS[inv.provider] ?? inv.provider}
                    </td>
                    <td className="py-3 pr-4">
                      <StatusPill status={inv.status} />
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
