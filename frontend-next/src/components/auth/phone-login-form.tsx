"use client"

import { useEffect, useState } from "react"
import { useSearchParams } from "next/navigation"
import { useLocale, useTranslations } from "next-intl"
import { toast } from "sonner"
import { Phone, KeyRound, Lock } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { CaptchaGate, type CaptchaChallenge } from "./captcha-gate"
import { goToPostAuthRedirect, resolvePostAuthRedirect } from "@/lib/auth/post-auth-redirect"

/**
 * Phone verification login / registration form (A1 rewrite).
 *
 * Three steps:
 * 1. Phone + captcha → send code
 * 2. Code input → verify code
 * 3. (NEW users only) Set password → complete registration
 *
 * For existing users, step 2 directly logs in.
 * For new users, step 2 returns a registration_token, and step 3 collects
 * a password before creating the account, granting trial, and logging in.
 */
type Step = "phone" | "code" | "set-password"

const MIN_PASSWORD_LENGTH = 12

type PhoneLoginFormProps = {
  captchaScenario?: "register" | "login"
}

export function PhoneLoginForm({ captchaScenario = "register" }: PhoneLoginFormProps) {
  const t = useTranslations("auth.phoneForm")
  const locale = useLocale()
  const searchParams = useSearchParams()
  const redirectTo = resolvePostAuthRedirect(searchParams, locale)

  const [step, setStep] = useState<Step>("phone")
  const [phone, setPhone] = useState("")
  const [captchaToken, setCaptchaToken] = useState<string | null>(null)
  const [captchaChallenge, setCaptchaChallenge] = useState<CaptchaChallenge | null>(null)
  const [code, setCode] = useState("")
  const [registrationToken, setRegistrationToken] = useState<string | null>(null)
  const [password, setPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [captchaExecuting, setCaptchaExecuting] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [resendCountdown, setResendCountdown] = useState(0)

  useEffect(() => {
    if (resendCountdown <= 0) return
    const timer = window.setInterval(() => {
      setResendCountdown((n) => Math.max(0, n - 1))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [resendCountdown])

  const phoneLooksValid = /^(\+?86)?\s*1[3-9]\d{9}$/.test(phone.replace(/[\s\-]/g, ""))

  const resolveCaptchaToken = async () => {
    if (captchaToken) return captchaToken
    if (!captchaChallenge) {
      toast.error(t("toastCaptchaLoading"))
      return null
    }
    setCaptchaExecuting(true)
    try {
      const token = await captchaChallenge.execute()
      setCaptchaToken(token)
      return token
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("toastCaptchaFailed"))
      captchaChallenge.reset()
      return null
    } finally {
      setCaptchaExecuting(false)
    }
  }

  const handleSendCode = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!phoneLooksValid) {
      toast.error(t("toastInvalidPhone"))
      return
    }
    const token = await resolveCaptchaToken()
    if (!token) {
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch("/auth/phone/send-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phone_number: phone.trim(),
          captcha_token: token,
        }),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        setCaptchaToken(null)
        captchaChallenge?.reset()
        toast.error(data?.detail || t("toastSendCodeFailed"))
        return
      }
      toast.success(t("toastCodeSent"))
      setStep("code")
      setResendCountdown(60)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("toastNetworkError"))
    } finally {
      setSubmitting(false)
    }
  }

  const handleVerifyCode = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!code.trim()) {
      toast.error(t("toastEnterCode"))
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch("/auth/phone/verify-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phone_number: phone.trim(),
          code: code.trim(),
        }),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || t("toastVerifyFailed"))
        return
      }

      if (data?.needs_password) {
        // NEW user — need to set password before registration is complete.
        setRegistrationToken(data.registration_token)
        setStep("set-password")
        return
      }

      // EXISTING user — already logged in.
      toast.success(t("toastLoginSuccess"))
      await goToPostAuthRedirect(redirectTo)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("toastNetworkError"))
    } finally {
      setSubmitting(false)
    }
  }

  const handleSetPassword = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!password || password.length < MIN_PASSWORD_LENGTH) {
      toast.error(t("toastPasswordMinLength", { min: MIN_PASSWORD_LENGTH }))
      return
    }
    if (password !== confirmPassword) {
      toast.error(t("toastPasswordMismatch"))
      return
    }
    if (!registrationToken) {
      toast.error(t("toastInvalidRegToken"))
      setStep("phone")
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch("/auth/phone/complete-registration", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          registration_token: registrationToken,
          password,
        }),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || t("toastRegisterFailed"))
        return
      }
      toast.success(t("toastRegisterSuccess"))
      await goToPostAuthRedirect(redirectTo)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("toastNetworkError"))
    } finally {
      setSubmitting(false)
    }
  }

  // === Step 1: Phone + captcha ===
  if (step === "phone") {
    return (
      <form onSubmit={handleSendCode} className="space-y-5">
        <div className="space-y-2">
          <Label htmlFor="phone" className="text-sm font-medium text-foreground">
            {t("phoneLabel")}
          </Label>
          <div className="relative">
            <Phone
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="phone"
              type="tel"
              inputMode="numeric"
              autoComplete="tel"
              placeholder={t("phonePlaceholder")}
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              className="h-11 pl-10"
              required
            />
          </div>
        </div>

        <CaptchaGate
          scenario={captchaScenario}
          verified={Boolean(captchaToken)}
          onVerify={(token) => setCaptchaToken(token)}
          onReady={setCaptchaChallenge}
          disabled={submitting}
        />

        <Button
          type="submit"
          className="h-11 w-full"
          disabled={
            submitting ||
            captchaExecuting ||
            !phoneLooksValid ||
            (!captchaToken && !captchaChallenge)
          }
        >
          {captchaExecuting ? t("verifying") : submitting ? t("sending") : t("sendCode")}
        </Button>
      </form>
    )
  }

  // === Step 2: Code input ===
  if (step === "code") {
    return (
      <form onSubmit={handleVerifyCode} className="space-y-5">
        <div className="space-y-2">
          <Label htmlFor="code" className="text-sm font-medium text-foreground">
            {t("codeLabel")}
          </Label>
          <div className="relative">
            <KeyRound
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder={t("codePlaceholder")}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 8))}
              className="h-11 pl-10 tracking-widest"
              required
            />
          </div>
          <p className="text-xs text-muted-foreground">
            {t.rich("codeSentTo", {
              phone,
              highlight: (chunks) => (
                <span className="font-medium text-foreground">{chunks}</span>
              ),
            })}
          </p>
        </div>

        <Button type="submit" className="h-11 w-full" disabled={submitting}>
          {submitting ? t("verifying") : t("verifyAndContinue")}
        </Button>

        <div className="flex items-center justify-between text-xs">
          <button
            type="button"
            onClick={() => { setStep("phone"); setCode("") }}
            className="text-muted-foreground hover:text-foreground transition-colors"
            disabled={submitting}
          >
            {t("useAnotherPhone")}
          </button>
          <button
            type="button"
            onClick={async () => {
              if (resendCountdown > 0 || submitting || !captchaToken) return
              setSubmitting(true)
              try {
                const res = await fetch("/auth/phone/send-code", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    phone_number: phone.trim(),
                    captcha_token: captchaToken,
                  }),
                  credentials: "include",
                })
                if (res.ok) {
                  toast.success(t("toastResent"))
                  setResendCountdown(60)
                }
              } finally {
                setSubmitting(false)
              }
            }}
            className="text-primary hover:text-primary/80 transition-colors disabled:text-muted-foreground"
            disabled={resendCountdown > 0 || submitting}
          >
            {resendCountdown > 0
              ? t("resendCountdown", { remaining: resendCountdown })
              : t("resend")}
          </button>
        </div>
      </form>
    )
  }

  // === Step 3: Set password (NEW users only) ===
  return (
    <form onSubmit={handleSetPassword} className="space-y-5">
      <div className="rounded-md border border-primary/20 bg-primary/5 p-3 text-sm text-foreground">
        <p className="font-medium">{t("setPasswordTitle")}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {t("setPasswordHint")}
        </p>
      </div>

      <div className="space-y-2">
        <Label htmlFor="new-password" className="text-sm font-medium text-foreground">
          {t("passwordLabel")}
        </Label>
        <div className="relative">
          <Lock
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="new-password"
            type="password"
            autoComplete="new-password"
            placeholder={t("passwordPlaceholder", { min: MIN_PASSWORD_LENGTH })}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="h-11 pl-10"
            required
          />
        </div>
      </div>
      <div className="space-y-2">
        <Label htmlFor="confirm-password" className="text-sm font-medium text-foreground">
          {t("confirmPasswordLabel")}
        </Label>
        <div className="relative">
          <Lock
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="confirm-password"
            type="password"
            autoComplete="new-password"
            placeholder={t("confirmPasswordPlaceholder")}
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            className="h-11 pl-10"
            required
          />
        </div>
      </div>

      <Button type="submit" className="h-11 w-full" disabled={submitting}>
        {submitting ? t("registering") : t("completeRegister")}
      </Button>
    </form>
  )
}
