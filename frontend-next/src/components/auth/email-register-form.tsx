"use client"

import { useState } from "react"
import { useSearchParams } from "next/navigation"
import { useLocale, useTranslations } from "next-intl"
import { toast } from "sonner"
import { KeyRound, Lock, Mail, User } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { CaptchaGate, type CaptchaChallenge } from "./captcha-gate"
import { goToPostAuthRedirect, resolvePostAuthRedirect } from "@/lib/auth/post-auth-redirect"

type Step = "email" | "code" | "password"

const MIN_PASSWORD_LENGTH = 12

export function EmailRegisterForm() {
  const t = useTranslations("auth.emailForm")
  const locale = useLocale()
  const searchParams = useSearchParams()
  const redirectTo = resolvePostAuthRedirect(searchParams, locale)
  const [step, setStep] = useState<Step>("email")
  const [email, setEmail] = useState("")
  const [code, setCode] = useState("")
  const [registrationToken, setRegistrationToken] = useState("")
  const [displayName, setDisplayName] = useState("")
  const [password, setPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [captchaToken, setCaptchaToken] = useState<string | null>(null)
  const [captchaChallenge, setCaptchaChallenge] = useState<CaptchaChallenge | null>(null)
  const [captchaExecuting, setCaptchaExecuting] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  const normalizedEmail = email.trim()
  const emailLooksValid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalizedEmail)

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
    if (!emailLooksValid) {
      toast.error(t("toastInvalidEmail"))
      return
    }
    const token = await resolveCaptchaToken()
    if (!token) return

    setSubmitting(true)
    try {
      const res = await fetch("/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: normalizedEmail,
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
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("toastNetworkError"))
    } finally {
      setSubmitting(false)
    }
  }

  const handleVerifyCode = async (e: React.FormEvent) => {
    e.preventDefault()
    const cleanCode = code.trim()
    if (!cleanCode) {
      toast.error(t("toastEnterCode"))
      return
    }

    setSubmitting(true)
    try {
      const res = await fetch("/auth/email/verify-registration-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: normalizedEmail,
          code: cleanCode,
        }),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || t("toastVerifyFailed"))
        return
      }
      setRegistrationToken(data.registration_token || "")
      toast.success(t("toastEmailVerified"))
      setStep("password")
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("toastNetworkError"))
    } finally {
      setSubmitting(false)
    }
  }

  const handleCompleteRegistration = async (e: React.FormEvent) => {
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
      toast.error(t("toastRegTokenExpired"))
      setStep("email")
      return
    }

    setSubmitting(true)
    try {
      const res = await fetch("/auth/email/complete-registration", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: normalizedEmail,
          registration_token: registrationToken,
          display_name: displayName.trim(),
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

  if (step === "code") {
    return (
      <form onSubmit={handleVerifyCode} className="space-y-5">
        <div className="rounded-lg border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
          {t.rich("codeSentTo", {
            normalizedEmail,
            highlight: (chunks) => (
              <span className="font-medium text-foreground">{chunks}</span>
            ),
          })}
        </div>

        <div className="space-y-2">
          <Label htmlFor="register-email-code" className="text-sm font-medium text-foreground">
            {t("codeLabel")}
          </Label>
          <div className="relative">
            <KeyRound
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="register-email-code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder={t("codePlaceholder")}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\s/g, "").slice(0, 16))}
              className="h-11 pl-10 tracking-widest"
              required
            />
          </div>
        </div>

        <Button type="submit" className="h-11 w-full" disabled={submitting || !code.trim()}>
          {submitting ? t("verifying") : t("verifyEmail")}
        </Button>

        <Button
          type="button"
          variant="ghost"
          className="h-10 w-full"
          disabled={submitting}
          onClick={() => {
            setStep("email")
            setCode("")
            setRegistrationToken("")
            setCaptchaToken(null)
            captchaChallenge?.reset()
          }}
        >
          {t("editEmailOrResend")}
        </Button>
      </form>
    )
  }

  if (step === "password") {
    return (
      <form onSubmit={handleCompleteRegistration} className="space-y-5">
        <div className="rounded-lg border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
          {t.rich("emailVerified", {
            normalizedEmail,
            highlight: (chunks) => (
              <span className="font-medium text-foreground">{chunks}</span>
            ),
          })}
        </div>

        <div className="space-y-2">
          <Label htmlFor="register-name" className="text-sm font-medium text-foreground">
            {t("nicknameLabel")}
          </Label>
          <div className="relative">
            <User
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="register-name"
              type="text"
              autoComplete="nickname"
              placeholder={t("nicknamePlaceholder")}
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="h-11 pl-10"
            />
          </div>
        </div>

        <div className="space-y-2">
          <Label htmlFor="register-password" className="text-sm font-medium text-foreground">
            {t("passwordLabel")}
          </Label>
          <div className="relative">
            <Lock
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="register-password"
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
          <Label htmlFor="register-confirm-password" className="text-sm font-medium text-foreground">
            {t("confirmPasswordLabel")}
          </Label>
          <div className="relative">
            <Lock
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="register-confirm-password"
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

        <p className="text-xs leading-relaxed text-muted-foreground">
          {t("registerNote")}
        </p>

        <Button
          type="submit"
          className="h-11 w-full"
          disabled={submitting || !password || !confirmPassword}
        >
          {submitting ? t("registering") : t("completeRegister")}
        </Button>
      </form>
    )
  }

  return (
    <form onSubmit={handleSendCode} className="space-y-5">
      <div className="space-y-2">
        <Label htmlFor="register-email" className="text-sm font-medium text-foreground">
          {t("emailLabel")}
        </Label>
        <div className="relative">
          <Mail
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="register-email"
            type="email"
            inputMode="email"
            autoComplete="email"
            placeholder={t("emailPlaceholder")}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="h-11 pl-10"
            required
          />
        </div>
      </div>

      <CaptchaGate
        scenario="register"
        verified={Boolean(captchaToken)}
        onVerify={(token) => setCaptchaToken(token)}
        onReady={setCaptchaChallenge}
        disabled={submitting}
      />

      <p className="text-xs leading-relaxed text-muted-foreground">
        {t("sendCodeHint")}
      </p>

      <Button
        type="submit"
        className="h-11 w-full"
        disabled={
          submitting ||
          captchaExecuting ||
          !emailLooksValid ||
          (!captchaToken && !captchaChallenge)
        }
      >
        {captchaExecuting ? t("verifying") : submitting ? t("sending") : t("sendCode")}
      </Button>
    </form>
  )
}
