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
 * On checkout.completed we bounce back to /settings/billing with
 * `provider=paddle&status=processing&order_id=...` so BillingStatusBanner polls
 * the order until the webhook settles it. The webhook is the source of truth;
 * this redirect is only for UX.
 *
 * Security: the ONLY Paddle value used here is the client-side token via
 * NEXT_PUBLIC_PADDLE_CLIENT_TOKEN, which Paddle designs to be public/browser-safe.
 * No API key, webhook secret, or internal price id appears in the frontend.
 */

import { Suspense, useRef, useState } from "react"
import Script from "next/script"
import { useRouter, useSearchParams } from "next/navigation"
import { Loader2 } from "lucide-react"

type PaddleCheckoutEvent = {
  name?: string
  data?: { custom_data?: { order_id?: string } | null } | null
}

type PaddleGlobal = {
  Environment?: { set: (env: string) => void }
  Initialize: (opts: {
    token: string
    eventCallback?: (event: PaddleCheckoutEvent) => void
  }) => void
}

declare global {
  interface Window {
    Paddle?: PaddleGlobal
  }
}

const PADDLE_JS_SRC = "https://cdn.paddle.com/paddle/v2/paddle.js"

function PaddleCheckoutInner() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const transactionId = searchParams.get("_ptxn")
  const clientToken = process.env.NEXT_PUBLIC_PADDLE_CLIENT_TOKEN
  const environment =
    process.env.NEXT_PUBLIC_PADDLE_ENV === "production" ? "production" : "sandbox"

  const [error, setError] = useState<string | null>(null)
  const handledRef = useRef(false)

  const handleEvent = (event: PaddleCheckoutEvent) => {
    if (handledRef.current) return
    if (event?.name === "checkout.completed") {
      handledRef.current = true
      const orderId = event.data?.custom_data?.order_id ?? ""
      const query = new URLSearchParams({ provider: "paddle", status: "processing" })
      if (orderId) query.set("order_id", orderId)
      router.replace(`/settings/billing?${query.toString()}`)
    } else if (event?.name === "checkout.closed") {
      handledRef.current = true
      router.replace("/settings/billing")
    }
  }

  const initPaddle = () => {
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
      paddle.Environment?.set(environment)
      // Paddle.js auto-opens the overlay for the `_ptxn` transaction after init.
      paddle.Initialize({ token: clientToken, eventCallback: handleEvent })
    } catch {
      setError("无法打开支付页,请稍后重试。")
    }
  }

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
          <button
            type="button"
            onClick={() => router.replace("/settings/billing")}
            className="rounded-md border border-border px-4 py-2 text-sm text-foreground transition-colors hover:border-primary/40"
          >
            返回账单页
          </button>
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
