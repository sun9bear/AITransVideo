"use client"

/**
 * Credits summary card for the billing page (V3-2).
 *
 * Shows: total available credits, bucket breakdown, trial expiry.
 * Consumes shadow credits data — does NOT represent V2 billing truth.
 */

import { useEffect, useState } from "react"
import { Coins, Clock, Gift, CreditCard, ShoppingBag, Wrench } from "lucide-react"
import {
  getMyCredits,
  getMyCreditsLedger,
  type CreditsResponse,
  type LedgerEntry,
} from "@/lib/billing/get-credits"

const BUCKET_LABELS: Record<string, string> = {
  free: "免费赠送",
  trial: "试用赠送",
  subscription: "订阅配额",
  topup: "充值余额",
  manual_adjustment: "人工调整",
}

const BUCKET_ICONS: Record<string, typeof Coins> = {
  free: Gift,
  trial: Clock,
  subscription: CreditCard,
  topup: ShoppingBag,
  manual_adjustment: Wrench,
}

const DIRECTION_LABELS: Record<string, string> = {
  grant: "获得",
  reserve: "预扣",
  capture: "消费",
  release: "退还",
  refund: "退款",
  rollback: "回滚",
}

function formatRelativeTime(isoStr: string | null): string {
  if (!isoStr) return ""
  const date = new Date(isoStr)
  const now = new Date()
  const diffMs = date.getTime() - now.getTime()
  if (diffMs <= 0) return "已过期"
  const diffDays = Math.ceil(diffMs / (1000 * 60 * 60 * 24))
  if (diffDays === 1) return "明天到期"
  return `${diffDays} 天后到期`
}

export function CreditsSummary() {
  const [credits, setCredits] = useState<CreditsResponse | null>(null)
  const [ledger, setLedger] = useState<LedgerEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [creditsRes, ledgerRes] = await Promise.all([
          getMyCredits(),
          getMyCreditsLedger(10),
        ])
        if (cancelled) return
        setCredits(creditsRes)
        setLedger(ledgerRes.entries)
      } catch (err) {
        if (cancelled) return
        setError(err instanceof Error ? err.message : "加载失败")
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (loading) {
    return (
      <div className="rounded-xl border border-border bg-card p-6 animate-pulse">
        <div className="h-6 w-32 bg-muted rounded mb-4" />
        <div className="h-10 w-48 bg-muted rounded mb-6" />
        <div className="space-y-2">
          <div className="h-4 w-full bg-muted rounded" />
          <div className="h-4 w-3/4 bg-muted rounded" />
        </div>
      </div>
    )
  }

  if (error || !credits) {
    return null // Silently hide if credits not available (shadow mode)
  }

  const activeBuckets = credits.buckets.filter((b) => b.remaining > 0 || b.reserved > 0)

  return (
    <div className="space-y-4">
      {/* Credits balance card */}
      <div className="rounded-xl border border-border bg-card p-6">
        <div className="flex items-center gap-2 mb-3">
          <Coins className="h-5 w-5 text-primary" />
          <h3 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider">
            点数余额
          </h3>
        </div>

        <p className="text-3xl font-bold font-heading text-foreground mb-1">
          {credits.total_available.toLocaleString()}
          <span className="text-base font-normal text-muted-foreground ml-2">
            可用点数
          </span>
        </p>

        {credits.in_trial && credits.trial_expires_at && (
          <p className="text-sm text-cyan-400 flex items-center gap-1 mb-4">
            <Clock className="h-3.5 w-3.5" />
            试用期 {formatRelativeTime(credits.trial_expires_at)}
          </p>
        )}

        {/* Bucket breakdown */}
        {activeBuckets.length > 0 && (
          <div className="mt-4 space-y-2">
            <p className="text-xs text-muted-foreground font-medium">分项明细</p>
            {activeBuckets.map((bucket) => {
              const Icon = BUCKET_ICONS[bucket.type] ?? Coins
              const available = Math.max(0, bucket.remaining - bucket.reserved)
              return (
                <div
                  key={bucket.id}
                  className="flex items-center justify-between py-1.5 px-3 rounded-lg bg-muted/30"
                >
                  <div className="flex items-center gap-2 text-sm">
                    <Icon className="h-3.5 w-3.5 text-muted-foreground" />
                    <span className="text-foreground">
                      {BUCKET_LABELS[bucket.type] ?? bucket.type}
                    </span>
                    {bucket.expires_at && (
                      <span className="text-xs text-muted-foreground">
                        ({formatRelativeTime(bucket.expires_at)})
                      </span>
                    )}
                  </div>
                  <span className="text-sm font-mono font-medium text-foreground">
                    {available.toLocaleString()}
                  </span>
                </div>
              )
            })}
          </div>
        )}

        <p className="mt-4 text-xs text-muted-foreground">
          点数数据为预览版本，当前计费仍以套餐额度为准。
        </p>
      </div>

      {/* Recent ledger entries */}
      {ledger.length > 0 && (
        <div className="rounded-xl border border-border bg-card p-6">
          <h3 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider mb-3">
            最近点数变动
          </h3>
          <div className="space-y-1.5">
            {ledger.slice(0, 8).map((entry) => (
              <div
                key={entry.id}
                className="flex items-center justify-between py-1.5 px-3 rounded-lg bg-muted/20 text-sm"
              >
                <div className="flex items-center gap-2">
                  <span className="text-muted-foreground">
                    {DIRECTION_LABELS[entry.direction] ?? entry.direction}
                  </span>
                  {entry.related_job_id && (
                    <span className="text-xs text-muted-foreground font-mono">
                      {entry.related_job_id.slice(0, 8)}...
                    </span>
                  )}
                </div>
                <span
                  className={`font-mono font-medium ${
                    entry.credits_delta > 0
                      ? "text-emerald-400"
                      : entry.credits_delta < 0
                        ? "text-red-400"
                        : "text-muted-foreground"
                  }`}
                >
                  {entry.credits_delta > 0 ? "+" : ""}
                  {entry.credits_delta}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
