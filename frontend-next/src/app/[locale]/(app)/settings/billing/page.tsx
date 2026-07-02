"use client"

/**
 * `/settings/billing` — baseline Billing UI (Task 6).
 *
 * T5 shipped this route as a minimal checkout entry: subscription snapshot +
 * checkout card. T6 extends the same route (per the "don't fork the billing
 * entry" guardrail) into a small, workmanlike billing center:
 *
 *   1. page header
 *   2. <BillingStatusBanner />     — reacts to T5 ?status=... redirects
 *   3. <SubscriptionSummary />     — current paid state + trial bookkeeping
 *   4. <CheckoutCard />            — existing upgrade action (unchanged surface)
 *   5. <OrderHistory />            — recent billing_invoices for this user
 *
 * Out of scope (stay inside T6):
 * - refund actions
 * - cancellation UX
 * - auto-renew management
 * - invoice download / export
 * - admin tenant-wide view
 * - auth polish
 *
 * Data flow: every fetch goes through client helpers that hit the existing
 * gateway endpoints. We deliberately do NOT try to become SSR-aware.
 */

import { Suspense, useEffect, useState } from "react"
import { useTranslations } from "next-intl"
import { Link } from "@/i18n/navigation"
import { Loader2 } from "lucide-react"
import { Skeleton } from "@/components/ui/skeleton"
import { Button } from "@/components/ui/button"
import { BillingStatusBanner } from "@/components/billing/billing-status-banner"
import { CheckoutCard } from "@/components/billing/checkout-card"
import { OrderHistory } from "@/components/billing/order-history"
import { CreditsSummary } from "@/components/billing/credits-summary"
import { SubscriptionSummary } from "@/components/billing/subscription-summary"
import { TopupPurchaseCard } from "@/components/billing/topup-purchase-card"
import { getPlans } from "@/lib/billing/get-plans"
import { getMySubscription } from "@/lib/billing/get-subscription"
import { getCheckoutConfig } from "@/lib/billing/get-checkout-config"
import { useApiErrorMessage } from "@/lib/api/error-localization"
import type { Plan } from "@/lib/billing/types"
import type { MeSubscriptionResponse } from "@/lib/billing/get-subscription"
import type { CheckoutConfigResponse } from "@/lib/billing/get-checkout-config"

type PageState =
  | { status: "loading" }
  | {
      status: "ready"
      plans: Plan[]
      subscription: MeSubscriptionResponse
      checkoutConfig: CheckoutConfigResponse
    }
  | { status: "error"; message: string }

export default function BillingPage() {
  const t = useTranslations("appSettings")
  const localizeError = useApiErrorMessage()
  const [state, setState] = useState<PageState>({ status: "loading" })
  const [refetchToken, setRefetchToken] = useState(0)

  // All state updates happen AFTER the await boundary, so the effect body
  // never calls setState synchronously. The `refetchToken` dependency lets
  // the retry button re-run the fetch without us hand-calling setState
  // synchronously inside a click handler-invoked helper.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [plansRes, subRes, cfgRes] = await Promise.all([
          getPlans(),
          getMySubscription(),
          getCheckoutConfig(),
        ])
        if (cancelled) return
        setState({
          status: "ready",
          plans: plansRes.plans,
          subscription: subRes,
          checkoutConfig: cfgRes,
        })
      } catch (err) {
        if (cancelled) return
        const message = err instanceof Error ? localizeError(err) : t("billing.loadFailed")
        setState({ status: "error", message })
      }
    })()
    return () => {
      cancelled = true
    }
  }, [refetchToken])

  const handleRetry = () => {
    setState({ status: "loading" })
    setRefetchToken((n) => n + 1)
  }

  const handleOrderSettled = () => {
    setRefetchToken((n) => n + 1)
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold text-foreground">{t("billing.title")}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {t("billing.subtitle")}
        </p>
      </header>

      {/*
        Status banner is wrapped in Suspense because `useSearchParams` requires
        a Suspense boundary during static generation. It's its own island so a
        failed Promise from the three primary fetches never hides the banner.
      */}
      <Suspense fallback={null}>
        <BillingStatusBanner onOrderSettled={handleOrderSettled} />
      </Suspense>

      {state.status === "loading" && (
        <div className="space-y-4">
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-80 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      )}

      {state.status === "error" && (
        <div className="rounded-lg border border-border bg-card p-6">
          <p className="text-sm text-muted-foreground">
            {state.message || t("billing.loadFailedRetry")}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={handleRetry}
          >
            <Loader2 className="mr-2 h-3 w-3" />
            {t("billing.retry")}
          </Button>
        </div>
      )}

      {state.status === "ready" && (
        <>
          <SubscriptionSummary subscription={state.subscription} />
          {/* key: remount after a settled order so the balance refetches */}
          <CreditsSummary key={refetchToken} />
          <TopupPurchaseCard
            checkoutConfig={state.checkoutConfig}
            onOrderSettled={handleOrderSettled}
          />
          <CheckoutCard
            plans={state.plans}
            subscription={state.subscription}
            checkoutConfig={state.checkoutConfig}
            onOrderSettled={handleOrderSettled}
          />
          {/*
            OrderHistory fetches its own data so it loads independently of the
            primary three-way Promise.all above. Keeps the page usable even if
            one of the endpoints is slow or unavailable.
          */}
          <OrderHistory />
          <p className="text-xs text-muted-foreground">
            {t("billing.footerView")}
            <Link
              href="/pricing"
              className="mx-1 text-primary hover:text-primary/80 transition-colors"
            >
              {t("billing.footerPlanCompare")}
            </Link>
            {t("billing.footerOrReturn")}
            <Link
              href="/settings"
              className="ml-1 text-primary hover:text-primary/80 transition-colors"
            >
              {t("billing.footerWorkspace")}
            </Link>
            {t("billing.footerPeriod")}
          </p>
        </>
      )}
    </div>
  )
}
