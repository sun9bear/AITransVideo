"use client"

/**
 * Credits summary card for the billing page (V3-2).
 *
 * Shows: total available credits, bucket breakdown, trial expiry.
 * Consumes shadow credits data — does NOT represent V2 billing truth.
 */

import { useEffect, useState } from "react"
import { useTranslations } from "next-intl"
import { Coins, Clock, Gift, CreditCard, ShoppingBag, Wrench } from "lucide-react"
import {
  getMyCredits,
  getMyCreditsLedger,
  type CreditsResponse,
  type LedgerEntry,
} from "@/lib/billing/get-credits"

/** Translator scoped to the `appBilling` namespace (relative keys). */
type BillingTranslator = ReturnType<typeof useTranslations<"appBilling">>
/** Literal message-key type for appBilling — lets label-key maps stay typed. */
type BillingKey = Parameters<BillingTranslator>[0]

const BUCKET_LABEL_KEYS: Record<string, BillingKey> = {
  free: "credits.bucket.free",
  trial: "credits.bucket.trial",
  subscription: "credits.bucket.subscription",
  topup: "credits.bucket.topup",
  manual_adjustment: "credits.bucket.manual_adjustment",
}

const BUCKET_ICONS: Record<string, typeof Coins> = {
  free: Gift,
  trial: Clock,
  subscription: CreditCard,
  topup: ShoppingBag,
  manual_adjustment: Wrench,
}

const DIRECTION_LABEL_KEYS: Record<string, BillingKey> = {
  grant: "credits.direction.grant",
  reserve: "credits.direction.reserve",
  capture: "credits.direction.capture",
  release: "credits.direction.release",
  refund: "credits.direction.refund",
  rollback: "credits.direction.rollback",
}

function formatRelativeTime(t: BillingTranslator, isoStr: string | null): string {
  if (!isoStr) return ""
  const date = new Date(isoStr)
  const now = new Date()
  const diffMs = date.getTime() - now.getTime()
  if (diffMs <= 0) return t("credits.expired")
  const diffDays = Math.ceil(diffMs / (1000 * 60 * 60 * 24))
  if (diffDays === 1) return t("credits.expiresTomorrow")
  return t("credits.expiresInDays", { days: diffDays })
}

export function CreditsSummary() {
  const t = useTranslations("appBilling")
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
        setError(err instanceof Error ? err.message : t("credits.loadError"))
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [t])

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
            {t("credits.balanceTitle")}
          </h3>
        </div>

        <p className="text-3xl font-bold font-heading text-foreground mb-1">
          {credits.total_available.toLocaleString()}
          <span className="text-base font-normal text-muted-foreground ml-2">
            {t("credits.availableCredits")}
          </span>
        </p>

        {credits.in_trial && credits.trial_expires_at && (
          <p className="text-sm text-cyan-400 flex items-center gap-1 mb-4">
            <Clock className="h-3.5 w-3.5" />
            {t("credits.trialRemaining", {
              remaining: formatRelativeTime(t, credits.trial_expires_at),
            })}
          </p>
        )}

        {/* Bucket breakdown */}
        {activeBuckets.length > 0 && (
          <div className="mt-4 space-y-2">
            <p className="text-xs text-muted-foreground font-medium">{t("credits.breakdownTitle")}</p>
            {activeBuckets.map((bucket) => {
              const Icon = BUCKET_ICONS[bucket.type] ?? Coins
              const available = Math.max(0, bucket.remaining - bucket.reserved)
              const bucketLabelKey = BUCKET_LABEL_KEYS[bucket.type]
              return (
                <div
                  key={bucket.id}
                  className="flex items-center justify-between py-1.5 px-3 rounded-lg bg-muted/30"
                >
                  <div className="flex items-center gap-2 text-sm">
                    <Icon className="h-3.5 w-3.5 text-muted-foreground" />
                    <span className="text-foreground">
                      {bucketLabelKey ? t(bucketLabelKey) : bucket.type}
                    </span>
                    {bucket.expires_at && (
                      <span className="text-xs text-muted-foreground">
                        ({formatRelativeTime(t, bucket.expires_at)})
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
          {t("credits.previewNotice")}
        </p>
      </div>

      {/* Recent ledger entries */}
      {ledger.length > 0 && (
        <div className="rounded-xl border border-border bg-card p-6">
          <h3 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider mb-3">
            {t("credits.recentChanges")}
          </h3>
          <div className="space-y-1.5">
            {ledger.slice(0, 8).map((entry) => (
              <div
                key={entry.id}
                className="flex items-center justify-between py-1.5 px-3 rounded-lg bg-muted/20 text-sm"
              >
                <div className="flex items-center gap-2">
                  <span className="text-muted-foreground">
                    {DIRECTION_LABEL_KEYS[entry.direction]
                      ? t(DIRECTION_LABEL_KEYS[entry.direction])
                      : entry.direction}
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
