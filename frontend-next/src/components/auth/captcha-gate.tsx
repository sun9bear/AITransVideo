"use client"

import { useEffect, useRef, useState } from "react"
import { ShieldCheck, Check } from "lucide-react"
import { cn } from "@/lib/utils"

/**
 * Captcha gate component — supports Cloudflare Turnstile and fake (dev) modes.
 *
 * - `turnstile`: Loads Cloudflare Turnstile JS, renders a managed widget.
 *   On success the widget returns a token that the backend validates via
 *   a single POST to https://challenges.cloudflare.com/turnstile/v0/siteverify.
 *
 * - `fake`: Click-to-verify button for local development / tests.
 */
type CaptchaGateProps = {
  onVerify: (token: string) => void
  verified: boolean
  disabled?: boolean
}

const CAPTCHA_PROVIDER =
  (typeof window !== "undefined"
    ? (process.env.NEXT_PUBLIC_CAPTCHA_PROVIDER || "").trim().toLowerCase()
    : "") || "fake"

const TURNSTILE_SITE_KEY = process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY || ""

// ---------------------------------------------------------------------------
// Fake (dev) captcha
// ---------------------------------------------------------------------------

function FakeCaptchaGate({ onVerify, verified, disabled }: CaptchaGateProps) {
  const [loading, setLoading] = useState(false)

  const handleClick = async () => {
    if (verified || loading || disabled) return
    setLoading(true)
    await new Promise((r) => setTimeout(r, 250))
    onVerify("fake-ok")
    setLoading(false)
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={verified || loading || disabled}
      aria-pressed={verified}
      className={cn(
        "flex w-full items-center gap-3 rounded-lg border px-4 py-3 text-sm transition-colors",
        verified
          ? "border-primary/50 bg-primary/5 text-foreground"
          : "border-border bg-card text-muted-foreground hover:border-primary/40 hover:text-foreground",
        (loading || disabled) && "opacity-70",
      )}
    >
      <span
        className={cn(
          "flex h-5 w-5 shrink-0 items-center justify-center rounded border",
          verified
            ? "border-primary bg-primary text-primary-foreground"
            : "border-border bg-background",
        )}
        aria-hidden="true"
      >
        {verified ? <Check className="h-3.5 w-3.5" /> : null}
      </span>
      <span className="flex items-center gap-1.5">
        <ShieldCheck className="h-4 w-4" aria-hidden="true" />
        {loading
          ? "正在验证…"
          : verified
            ? "已完成人机验证"
            : "点击完成人机验证"}
      </span>
    </button>
  )
}

// ---------------------------------------------------------------------------
// Cloudflare Turnstile captcha
// ---------------------------------------------------------------------------

function TurnstileCaptchaGate({ onVerify, verified, disabled }: CaptchaGateProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const widgetIdRef = useRef<string | null>(null)
  const [error, setError] = useState<string | null>(
    !TURNSTILE_SITE_KEY
      ? "验证码配置缺失（NEXT_PUBLIC_TURNSTILE_SITE_KEY 未设置）"
      : null,
  )

  useEffect(() => {
    if (typeof window === "undefined") return
    if (!TURNSTILE_SITE_KEY || !containerRef.current) return

    const renderWidget = () => {
      if (!containerRef.current || widgetIdRef.current) return
      try {
        // @ts-expect-error — Turnstile global
        widgetIdRef.current = window.turnstile.render(containerRef.current, {
          sitekey: TURNSTILE_SITE_KEY,
          theme: "dark",
          language: "zh-cn",
          callback: (token: string) => {
            onVerify(token)
          },
          "error-callback": () => {
            setError("人机验证失败,请刷新重试")
          },
        })
      } catch (e) {
        console.error("Turnstile render error:", e)
        setError("验证码初始化失败,请刷新重试")
      }
    }

    // @ts-expect-error — Turnstile global
    if (window.turnstile) {
      renderWidget()
      return
    }

    const script = document.createElement("script")
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"
    script.async = true
    script.onload = () => renderWidget()
    script.onerror = () => setError("验证码加载失败,请刷新重试")
    document.head.appendChild(script)

    return () => {
      // Cleanup widget on unmount
      if (widgetIdRef.current) {
        try {
          // @ts-expect-error — Turnstile global
          window.turnstile?.remove(widgetIdRef.current)
        } catch { /* ignore */ }
        widgetIdRef.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (error) {
    return (
      <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
        {error}
      </div>
    )
  }

  return (
    <div className="w-full space-y-2">
      <div
        ref={containerRef}
        className={cn(
          "flex w-full items-center justify-center rounded-lg border bg-card px-4 py-3 min-h-[65px]",
          verified ? "border-primary/50" : "border-border",
          disabled && "opacity-70 pointer-events-none",
        )}
      />
      {verified && (
        <div className="flex items-center gap-1.5 text-sm text-primary">
          <Check className="h-3.5 w-3.5" />
          已完成人机验证
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

export function CaptchaGate(props: CaptchaGateProps) {
  if (CAPTCHA_PROVIDER === "turnstile") {
    return <TurnstileCaptchaGate {...props} />
  }
  if (CAPTCHA_PROVIDER === "aliyun") {
    // Legacy — kept for reference but no longer used in production
    return <FakeCaptchaGate {...props} />
  }
  return <FakeCaptchaGate {...props} />
}
