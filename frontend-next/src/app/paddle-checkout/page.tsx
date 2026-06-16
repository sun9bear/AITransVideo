"use client"

/**
 * Paddle default-payment-link handoff page (plan 2026-06-08, P1d).
 *
 * Backend `create_checkout` returns Paddle's `checkout.url`, which points here
 * with a `?_ptxn={transaction_id}` query param. This page only needs to load
 * Paddle.js and call `Paddle.Initialize({ token })`; Paddle.js then AUTOMATICALLY
 * opens the overlay checkout for the transaction in `_ptxn` (confirmed against
 * Paddle's default-payment-link docs — no explicit Paddle.Checkout.open needed).
 *
 * Checkout settings passed at Initialize (applied to the auto-opened checkout):
 * - locale zh-Hans + theme matching the workspace theme — without these Paddle
 *   renders by browser locale (often English) on a light surface.
 * - successUrl as a redirect fallback in case our eventCallback never runs
 *   (the banner then recovers the order id from the localStorage stash).
 *
 * On checkout.completed we bounce back to /settings/billing with
 * `provider=paddle&status=processing&order_id=...` so BillingStatusBanner polls
 * the order until the webhook settles it. checkout.closed bounces back with
 * `status=closed` so the banner can say "window closed" instead of pretending
 * a payment is confirming. The webhook is the source of truth; these redirects
 * are only for UX.
 *
 * cdn.paddle.com is an offshore CDN: from mainland networks it can hang in a
 * half-open state where <Script onError> never fires. A watchdog surfaces a
 * retry UI if no Paddle checkout event arrives within WATCHDOG_MS.
 *
 * Security: the ONLY Paddle value used here is the client-side token via
 * NEXT_PUBLIC_PADDLE_CLIENT_TOKEN, which Paddle designs to be public/browser-safe.
 * No API key, webhook secret, or internal price id appears in the frontend.
 */

import { Suspense, useEffect, useRef, useState } from "react"
import Script from "next/script"
import { useRouter, useSearchParams } from "next/navigation"
import { Loader2 } from "lucide-react"
import { readWorkspaceTheme } from "@/lib/theme"

type PaddleCheckoutEvent = {
  name?: string
  data?: { custom_data?: { order_id?: string } | null } | null
}

type PaddleCheckoutSettings = {
  displayMode?: "overlay" | "inline"
  locale?: string
  theme?: "light" | "dark"
  successUrl?: string
}

type PaddleGlobal = {
  Environment?: { set: (env: string) => void }
  Initialize: (opts: {
    token: string
    eventCallback?: (event: PaddleCheckoutEvent) => void
    checkout?: { settings?: PaddleCheckoutSettings }
  }) => void
}

declare global {
  interface Window {
    Paddle?: PaddleGlobal
  }
}

const PADDLE_JS_SRC = "https://cdn.paddle.com/paddle/v2/paddle.js"

// How long to wait for the first Paddle checkout event (checkout.loaded fires
// when the overlay opens) before assuming the CDN / transaction is stuck.
const WATCHDOG_MS = 15000

const TIMEOUT_MESSAGE =
  "支付页加载超时,可能是网络原因。可以重试,或返回账单页重新发起支付。"

function PaddleCheckoutInner() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const transactionId = searchParams.get("_ptxn")
  const clientToken = process.env.NEXT_PUBLIC_PADDLE_CLIENT_TOKEN
  const environment =
    process.env.NEXT_PUBLIC_PADDLE_ENV === "production" ? "production" : "sandbox"

  const [error, setError] = useState<string | null>(null)
  const handledRef = useRef(false)
  const initializedRef = useRef(false)
  const sawPaddleEventRef = useRef(false)
  const watchdogRef = useRef<number | null>(null)

  const clearWatchdog = () => {
    if (watchdogRef.current !== null) {
      window.clearTimeout(watchdogRef.current)
      watchdogRef.current = null
    }
  }

  const armWatchdog = () => {
    clearWatchdog()
    watchdogRef.current = window.setTimeout(() => {
      if (!sawPaddleEventRef.current && !handledRef.current) {
        // Keep a more specific error (missing token etc.) if one is showing.
        setError((prev) => prev ?? TIMEOUT_MESSAGE)
      }
    }, WATCHDOG_MS)
  }

  const handleEvent = (event: PaddleCheckoutEvent) => {
    // Any checkout event (checkout.loaded etc.) proves Paddle.js is alive.
    sawPaddleEventRef.current = true
    clearWatchdog()
    if (handledRef.current) return
    // A late overlay (slow link) may arrive after the watchdog already
    // flagged a timeout — drop the stale error so the UI matches reality.
    setError(null)
    if (event?.name === "checkout.completed") {
      handledRef.current = true
      const orderId = event.data?.custom_data?.order_id ?? ""
      const query = new URLSearchParams({ provider: "paddle", status: "processing" })
      if (orderId) query.set("order_id", orderId)
      router.replace(`/settings/billing?${query.toString()}`)
    } else if (event?.name === "checkout.closed") {
      // Deliberate close ≠ payment confirming. The banner shows "window
      // closed" copy but still polls (stash/order_id): a WeChat buyer may
      // close the QR page while the async capture is in flight.
      handledRef.current = true
      const orderId = event.data?.custom_data?.order_id ?? ""
      const query = new URLSearchParams({ provider: "paddle", status: "closed" })
      if (orderId) query.set("order_id", orderId)
      router.replace(`/settings/billing?${query.toString()}`)
    }
  }

  const initPaddle = () => {
    // Mount effect + <Script onLoad> can both reach here (e.g. client-side
    // back-nav remounts the page with paddle.js already in the DOM, where
    // onLoad does NOT re-fire) — Initialize must run exactly once.
    if (initializedRef.current) return
    const paddle = window.Paddle
    if (!paddle) {
      setError("支付组件加载失败,请刷新页面重试。")
      return
    }
    if (!clientToken) {
      setError("支付暂未正确配置,请稍后再试或联系客服。")
      return
    }
    if (!transactionId) {
      setError("缺少支付凭证,请返回账单页重新发起支付。")
      return
    }
    try {
      initializedRef.current = true
      paddle.Environment?.set(environment)
      // Paddle.js auto-opens the overlay for the `_ptxn` transaction after init.
      paddle.Initialize({
        token: clientToken,
        eventCallback: handleEvent,
        checkout: {
          settings: {
            displayMode: "overlay",
            locale: "zh-Hans",
            theme: readWorkspaceTheme(),
            successUrl: `${window.location.origin}/settings/billing?provider=paddle&status=processing`,
          },
        },
      })
      armWatchdog()
    } catch {
      setError("无法打开支付页,请稍后重试。")
    }
  }

  useEffect(() => {
    // Arm the watchdog at mount, NOT at script-load: the motivating failure is
    // cdn.paddle.com hanging half-open on mainland networks, where neither
    // <Script onLoad> nor onError ever fires — a load-time-armed watchdog
    // would never start in exactly that scenario.
    armWatchdog()
    // Remount with paddle.js already loaded (client-side Back) — init now.
    if (window.Paddle) initPaddle()
    return clearWatchdog
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount-only; helpers are stable per render semantics
  }, [])

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 px-6 text-center">
      <Script
        src={PADDLE_JS_SRC}
        strategy="afterInteractive"
        onLoad={initPaddle}
        onError={() => setError("支付组件加载失败,请检查网络后刷新重试。")}
      />
      {error ? (
        <>
          <p className="text-sm text-foreground">{error}</p>
          <div className="flex flex-wrap items-center justify-center gap-3">
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="rounded-md bg-primary px-6 py-3 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
            >
              重试
            </button>
            <button
              type="button"
              onClick={() => router.replace("/settings/billing")}
              className="rounded-md border border-border px-6 py-3 text-sm text-foreground transition-colors hover:border-primary/40"
            >
              返回账单页
            </button>
          </div>
        </>
      ) : (
        <>
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-hidden="true" />
          <p className="text-sm text-muted-foreground">正在打开支付页,请稍候…</p>
        </>
      )}
    </main>
  )
}

export default function PaddleCheckoutPage() {
  return (
    <Suspense
      fallback={
        <main className="flex min-h-screen items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-hidden="true" />
        </main>
      }
    >
      <PaddleCheckoutInner />
    </Suspense>
  )
}
