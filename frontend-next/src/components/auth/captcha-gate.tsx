"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { useLocale, useTranslations } from "next-intl"
import { ShieldCheck, Check } from "lucide-react"
import { cn } from "@/lib/utils"

// GeeTest v4 / Cloudflare Turnstile language codes, driven by the active UI
// locale (UI-04 Step 5.6) so an English form never embeds a Chinese widget.
// locale is *read* from useLocale() — never a second <html lang> source (R5).
function geetestLanguage(locale: string): string {
  return locale === "en" ? "eng" : "zho"
}
function turnstileLanguage(locale: string): string {
  return locale === "en" ? "en" : "zh-cn"
}

/**
 * Captcha gate component — supports GeeTest, Cloudflare Turnstile, and fake modes.
 *
 * - `turnstile`: Loads Cloudflare Turnstile JS, renders a managed widget.
 *   On success the widget returns a token that the backend validates via
 *   a single POST to https://challenges.cloudflare.com/turnstile/v0/siteverify.
 *
 * - `geetest`: Loads GeeTest CAPTCHA v4, returns the validation payload as a
 *   JSON token that the backend verifies with the scene-specific private key.
 *
 * - `fake`: Click-to-verify button for local development / tests.
 */
type CaptchaGateProps = {
  onVerify: (token: string) => void
  verified: boolean
  disabled?: boolean
  onReady?: (challenge: CaptchaChallenge | null) => void
  scenario?: "register" | "login"
}

export type CaptchaChallenge = {
  execute: () => Promise<string>
  reset: () => void
}

const CAPTCHA_PROVIDER =
  (process.env.NEXT_PUBLIC_CAPTCHA_PROVIDER || "").trim().toLowerCase() || "fake"

const TURNSTILE_SITE_KEY = process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY || ""
const GEETEST_REGISTER_CAPTCHA_ID = process.env.NEXT_PUBLIC_GEETEST_REGISTER_CAPTCHA_ID || ""
const GEETEST_LOGIN_CAPTCHA_ID = process.env.NEXT_PUBLIC_GEETEST_LOGIN_CAPTCHA_ID || ""

type GeeTestValidateResult = {
  lot_number?: string
  captcha_output?: string
  pass_token?: string
  gen_time?: string
}

type GeeTestCaptcha = {
  showCaptcha: () => void
  getValidate: () => GeeTestValidateResult | false
  reset?: () => void
  destroy?: () => void
  onReady: (callback: () => void) => GeeTestCaptcha
  onSuccess: (callback: () => void) => GeeTestCaptcha
  onError: (callback: (error: { code?: string; msg?: string; desc?: unknown }) => void) => GeeTestCaptcha
  onClose: (callback: () => void) => GeeTestCaptcha
}

declare global {
  interface Window {
    initGeetest4?: (
      config: Record<string, unknown>,
      callback: (captcha: GeeTestCaptcha) => void,
    ) => void
  }
}

function getGeetestCaptchaId(scenario: CaptchaGateProps["scenario"]) {
  return scenario === "login"
    ? GEETEST_LOGIN_CAPTCHA_ID || GEETEST_REGISTER_CAPTCHA_ID
    : GEETEST_REGISTER_CAPTCHA_ID || GEETEST_LOGIN_CAPTCHA_ID
}

// ---------------------------------------------------------------------------
// Fake (dev) captcha
// ---------------------------------------------------------------------------

function FakeCaptchaGate({ onVerify, verified, disabled, onReady }: CaptchaGateProps) {
  const t = useTranslations("auth.captcha")
  const [loading, setLoading] = useState(false)

  const complete = useCallback(async () => {
    if (verified || loading || disabled) return
    setLoading(true)
    await new Promise((r) => setTimeout(r, 250))
    onVerify("fake-ok")
    setLoading(false)
    return "fake-ok"
  }, [disabled, loading, onVerify, verified])

  useEffect(() => {
    onReady?.({
      execute: async () => (await complete()) || "fake-ok",
      reset: () => undefined,
    })
    return () => onReady?.(null)
  }, [complete, onReady])

  return (
    <button
      type="button"
      onClick={complete}
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
          ? t("loadingVerify")
          : verified
            ? t("verified")
            : t("clickToComplete")}
      </span>
    </button>
  )
}

// ---------------------------------------------------------------------------
// GeeTest CAPTCHA v4
// ---------------------------------------------------------------------------

function GeeTestCaptchaGate({
  onVerify,
  verified,
  disabled,
  onReady,
  scenario = "register",
}: CaptchaGateProps) {
  const t = useTranslations("auth.captcha")
  const locale = useLocale()
  // Callbacks below live in stable useCallback closures; read the latest
  // translator through a ref so a locale switch is reflected without
  // re-creating the GeeTest widget (mirrors the existing onVerifyRef pattern).
  const tRef = useRef(t)
  // Read locale through a ref so the widget-init effect's dependency-driven
  // lifecycle is unchanged (R5: locale is read, never a new lang source; the
  // captcha render/destroy control flow stays byte-for-byte). A locale switch
  // remounts the form (full-page URL nav), re-reading this at mount.
  const localeRef = useRef(locale)
  const captchaRef = useRef<GeeTestCaptcha | null>(null)
  const onVerifyRef = useRef(onVerify)
  const pendingRef = useRef<{
    resolve: (token: string) => void
    reject: (error: Error) => void
    timeoutId: number
  } | null>(null)
  const captchaId = getGeetestCaptchaId(scenario)
  const [ready, setReady] = useState(false)
  const [executing, setExecuting] = useState(false)
  const [error, setError] = useState<string | null>(
    !captchaId
      ? t("configMissing", { var: "NEXT_PUBLIC_GEETEST_*_CAPTCHA_ID" })
      : null,
  )

  useEffect(() => {
    onVerifyRef.current = onVerify
  }, [onVerify])

  useEffect(() => {
    tRef.current = t
  }, [t])

  useEffect(() => {
    localeRef.current = locale
  }, [locale])

  const clearPending = useCallback(() => {
    if (pendingRef.current) {
      window.clearTimeout(pendingRef.current.timeoutId)
      pendingRef.current = null
    }
  }, [])

  const rejectToken = useCallback((message: string) => {
    setExecuting(false)
    onVerifyRef.current("")
    setError(message)
    if (pendingRef.current) {
      pendingRef.current.reject(new Error(message))
      clearPending()
    }
  }, [clearPending])

  const resolveToken = useCallback((result: GeeTestValidateResult) => {
    if (!result || !result.lot_number || !result.captcha_output || !result.pass_token || !result.gen_time) {
      rejectToken(tRef.current("resultIncomplete"))
      return
    }

    const token = JSON.stringify({
      provider: "geetest",
      scenario,
      captcha_id: captchaId,
      lot_number: result.lot_number,
      captcha_output: result.captcha_output,
      pass_token: result.pass_token,
      gen_time: result.gen_time,
    })

    setExecuting(false)
    setError(null)
    onVerifyRef.current(token)
    if (pendingRef.current) {
      pendingRef.current.resolve(token)
      clearPending()
    }
  }, [captchaId, clearPending, rejectToken, scenario])

  useEffect(() => {
    if (typeof window === "undefined" || !captchaId) return

    const init = () => {
      if (captchaRef.current || !window.initGeetest4) return
      window.initGeetest4(
        {
          captchaId,
          product: "bind",
          language: geetestLanguage(localeRef.current),
          timeout: 30000,
        },
        (captcha) => {
          captchaRef.current = captcha
          captcha
            .onReady(() => {
              setReady(true)
              setError(null)
            })
            .onSuccess(() => {
              const result = captcha.getValidate()
              if (!result) {
                rejectToken(tRef.current("notPassed"))
                return
              }
              resolveToken(result)
            })
            .onError((captchaError) => {
              const message = captchaError?.msg || tRef.current("loadFailed")
              rejectToken(message)
            })
            .onClose(() => {
              if (pendingRef.current) {
                rejectToken(tRef.current("completeFirst"))
              }
            })
        },
      )
    }

    if (window.initGeetest4) {
      init()
      return
    }

    const script = document.createElement("script")
    script.src = "https://static.geetest.com/v4/gt4.js"
    script.async = true
    script.onload = init
    script.onerror = () => setError(tRef.current("componentLoadFailed"))
    document.head.appendChild(script)

    return () => {
      clearPending()
      try {
        captchaRef.current?.destroy?.()
      } catch {
        /* ignore */
      }
      captchaRef.current = null
    }
  }, [captchaId, clearPending, rejectToken, resolveToken])

  const resetChallenge = useCallback(() => {
    setError(null)
    try {
      captchaRef.current?.reset?.()
    } catch {
      /* ignore */
    }
  }, [])

  const executeChallenge = useCallback(() => {
    if (!captchaRef.current || !ready) {
      return Promise.reject(new Error(tRef.current("stillLoading")))
    }
    setExecuting(true)
    setError(null)
    return new Promise<string>((resolve, reject) => {
      clearPending()
      pendingRef.current = {
        resolve,
        reject,
        timeoutId: window.setTimeout(() => {
          rejectToken(tRef.current("timeout"))
        }, 60000),
      }
      try {
        captchaRef.current?.showCaptcha()
      } catch {
        rejectToken(tRef.current("startFailed"))
      }
    })
  }, [clearPending, ready, rejectToken])

  useEffect(() => {
    if (!ready) return
    onReady?.({
      execute: executeChallenge,
      reset: resetChallenge,
    })
    return () => onReady?.(null)
  }, [executeChallenge, onReady, ready, resetChallenge])

  return (
    <div
      className={cn(
        "w-full rounded-lg border bg-card p-3 transition-colors",
        verified ? "border-primary/50 bg-primary/5" : "border-border",
        disabled && "opacity-70",
      )}
    >
      <div className="flex items-center gap-2 text-sm text-foreground">
        <span
          className={cn(
            "flex h-5 w-5 shrink-0 items-center justify-center rounded border",
            verified
              ? "border-primary bg-primary text-primary-foreground"
              : "border-border bg-background text-muted-foreground",
          )}
          aria-hidden="true"
        >
          {verified ? <Check className="h-3.5 w-3.5" /> : <ShieldCheck className="h-3.5 w-3.5" />}
        </span>
        <span>{verified ? t("verified") : ready ? t("ready") : t("loading")}</span>
      </div>
      {!verified && (
        <button
          type="button"
          onClick={() => {
            executeChallenge().catch(() => undefined)
          }}
          disabled={!ready || disabled || executing}
          className="mt-3 inline-flex h-9 w-full items-center justify-center rounded-md border border-primary/30 bg-primary/5 px-3 text-sm font-medium text-primary transition-colors hover:bg-primary/10 disabled:cursor-not-allowed disabled:border-border disabled:bg-muted/40 disabled:text-muted-foreground"
        >
          {executing ? t("verifying") : ready ? t("clickToVerify") : t("loadingShort")}
        </button>
      )}
      {error && !verified && (
        <div className="mt-3 flex items-center justify-between gap-3 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          <span>{error}</span>
          <button
            type="button"
            onClick={resetChallenge}
            className="shrink-0 font-medium text-primary transition-colors hover:text-primary/80"
          >
            {t("retry")}
          </button>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Cloudflare Turnstile captcha
// ---------------------------------------------------------------------------

function TurnstileCaptchaGate({ onVerify, verified, disabled, onReady }: CaptchaGateProps) {
  const t = useTranslations("auth.captcha")
  const locale = useLocale()
  const tRef = useRef(t)
  // See GeeTest note: locale is read via a ref so the render effect's
  // dependency-driven lifecycle stays unchanged (R5).
  const localeRef = useRef(locale)
  const containerRef = useRef<HTMLDivElement>(null)
  const widgetIdRef = useRef<string | null>(null)
  const onVerifyRef = useRef(onVerify)
  const pendingRef = useRef<{
    resolve: (token: string) => void
    reject: (error: Error) => void
    timeoutId: number
  } | null>(null)
  const [error, setError] = useState<string | null>(
    !TURNSTILE_SITE_KEY
      ? t("configMissing", { var: "NEXT_PUBLIC_TURNSTILE_SITE_KEY" })
      : null,
  )
  const [ready, setReady] = useState(false)
  const [executing, setExecuting] = useState(false)

  useEffect(() => {
    onVerifyRef.current = onVerify
  }, [onVerify])

  useEffect(() => {
    tRef.current = t
  }, [t])

  useEffect(() => {
    localeRef.current = locale
  }, [locale])

  const clearPending = useCallback(() => {
    if (pendingRef.current) {
      window.clearTimeout(pendingRef.current.timeoutId)
      pendingRef.current = null
    }
  }, [])

  const resolveToken = useCallback((token: string) => {
    setExecuting(false)
    setError(null)
    onVerifyRef.current(token)
    if (pendingRef.current) {
      pendingRef.current.resolve(token)
      clearPending()
    }
  }, [clearPending])

  const rejectToken = useCallback((message: string) => {
    setExecuting(false)
    onVerifyRef.current("")
    setError(message)
    if (pendingRef.current) {
      pendingRef.current.reject(new Error(message))
      clearPending()
    }
  }, [clearPending])

  useEffect(() => {
    if (typeof window === "undefined") return
    if (!TURNSTILE_SITE_KEY || !containerRef.current) return

    const renderWidget = () => {
      if (!containerRef.current || widgetIdRef.current) return
      try {
        // @ts-expect-error — Turnstile global
        widgetIdRef.current = window.turnstile.render(containerRef.current, {
          sitekey: TURNSTILE_SITE_KEY,
          theme: "light",
          language: turnstileLanguage(localeRef.current),
          size: window.matchMedia("(max-width: 340px)").matches ? "compact" : "flexible",
          appearance: "always",
          execution: "execute",
          retry: "auto",
          "retry-interval": 8000,
          "refresh-expired": "auto",
          "refresh-timeout": "auto",
          callback: (token: string) => resolveToken(token),
          "error-callback": () => {
            rejectToken(tRef.current("turnstileError"))
          },
          "expired-callback": () => {
            rejectToken(tRef.current("turnstileExpired"))
          },
          "timeout-callback": () => {
            rejectToken(tRef.current("timeout"))
          },
          "unsupported-callback": () => {
            rejectToken(tRef.current("turnstileUnsupported"))
          },
        })
        setReady(true)
      } catch (e) {
        console.error("Turnstile render error:", e)
        setError(tRef.current("turnstileInitFailed"))
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
    script.onerror = () => setError(tRef.current("turnstileLoadFailed"))
    document.head.appendChild(script)

    return () => {
      clearPending()
      // Cleanup widget on unmount
      if (widgetIdRef.current) {
        try {
          // @ts-expect-error — Turnstile global
          window.turnstile?.remove(widgetIdRef.current)
        } catch { /* ignore */ }
        widgetIdRef.current = null
      }
    }
  }, [clearPending, rejectToken, resolveToken])

  const resetChallenge = useCallback(() => {
    setError(null)
    if (widgetIdRef.current) {
      try {
        // @ts-expect-error — Turnstile global
        window.turnstile?.reset(widgetIdRef.current)
      } catch {
        /* ignore */
      }
    }
  }, [])

  const executeChallenge = useCallback(() => {
    if (!widgetIdRef.current) {
      return Promise.reject(new Error(tRef.current("stillLoading")))
    }
    setExecuting(true)
    setError(null)
    return new Promise<string>((resolve, reject) => {
      clearPending()
      pendingRef.current = {
        resolve,
        reject,
        timeoutId: window.setTimeout(() => {
          rejectToken(tRef.current("timeout"))
        }, 30000),
      }
      try {
        // @ts-expect-error — Turnstile global
        window.turnstile?.execute(widgetIdRef.current)
      } catch {
        rejectToken(tRef.current("startFailed"))
      }
    })
  }, [clearPending, rejectToken])

  useEffect(() => {
    if (!ready) return
    onReady?.({
      execute: executeChallenge,
      reset: resetChallenge,
    })
    return () => onReady?.(null)
  }, [executeChallenge, onReady, ready, resetChallenge])

  return (
    <div
      className={cn(
        "w-full rounded-lg border bg-card p-3 transition-colors",
        verified ? "border-primary/50 bg-primary/5" : "border-border",
        disabled && "opacity-70",
      )}
    >
      <div className="flex items-center gap-2 text-sm text-foreground">
        <span
          className={cn(
            "flex h-5 w-5 shrink-0 items-center justify-center rounded border",
            verified
              ? "border-primary bg-primary text-primary-foreground"
              : "border-border bg-background text-muted-foreground",
          )}
          aria-hidden="true"
        >
          {verified ? <Check className="h-3.5 w-3.5" /> : <ShieldCheck className="h-3.5 w-3.5" />}
        </span>
        <span>{verified ? t("verified") : ready ? t("ready") : t("loading")}</span>
      </div>
      <div
        data-turnstile-container="true"
        ref={containerRef}
        className={cn(
          "mt-3 flex w-full justify-center overflow-hidden [line-height:0] [&_iframe]:max-w-full",
          verified && "hidden",
          disabled && "pointer-events-none",
        )}
      />
      {!verified && (
        <button
          type="button"
          onClick={() => {
            executeChallenge().catch(() => undefined)
          }}
          disabled={!ready || disabled || executing}
          className="mt-3 inline-flex h-9 w-full items-center justify-center rounded-md border border-primary/30 bg-primary/5 px-3 text-sm font-medium text-primary transition-colors hover:bg-primary/10 disabled:cursor-not-allowed disabled:border-border disabled:bg-muted/40 disabled:text-muted-foreground"
        >
          {executing ? t("verifying") : ready ? t("clickToVerify") : t("loadingShort")}
        </button>
      )}
      {error && !verified && (
        <div className="mt-3 flex items-center justify-between gap-3 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          <span>{error}</span>
          <button
            type="button"
            onClick={resetChallenge}
            className="shrink-0 font-medium text-primary transition-colors hover:text-primary/80"
          >
            {t("retry")}
          </button>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

export function CaptchaGate(props: CaptchaGateProps) {
  if (CAPTCHA_PROVIDER === "geetest") {
    return <GeeTestCaptchaGate {...props} />
  }
  if (CAPTCHA_PROVIDER === "turnstile") {
    return <TurnstileCaptchaGate {...props} />
  }
  if (CAPTCHA_PROVIDER === "aliyun") {
    // Legacy — kept for reference but no longer used in production
    return <FakeCaptchaGate {...props} />
  }
  return <FakeCaptchaGate {...props} />
}
