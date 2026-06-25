"use client"

import { useState } from "react"
import { useLocale, useTranslations } from "next-intl"
import { Link } from "@/i18n/navigation"
import { toast } from "sonner"
import { KeyRound, Lock, Mail, Phone } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { BrandMark } from "@/components/marketing/brand-mark"
import { CaptchaGate } from "@/components/auth/captcha-gate"
import { cn } from "@/lib/utils"

type Method = "phone" | "email"
type Step = "identity" | "reset"

const MIN_PASSWORD_LENGTH = 12

export default function ForgotPasswordPage() {
  const t = useTranslations("auth.forgot")
  const locale = useLocale()
  const [method, setMethod] = useState<Method>("phone")
  const [step, setStep] = useState<Step>("identity")
  const [phone, setPhone] = useState("")
  const [email, setEmail] = useState("")
  const [captchaToken, setCaptchaToken] = useState<string | null>(null)
  const [code, setCode] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [submitting, setSubmitting] = useState(false)

  const normalizedPhone = phone.trim()
  const normalizedEmail = email.trim()
  const phoneLooksValid = /^(\+?86)?\s*1[3-9]\d{9}$/.test(phone.replace(/[\s\-]/g, ""))
  const emailLooksValid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalizedEmail)
  const identityLooksValid = method === "phone" ? phoneLooksValid : emailLooksValid

  const switchMethod = (next: Method) => {
    if (method === next || submitting) return
    setMethod(next)
    setStep("identity")
    setCaptchaToken(null)
    setCode("")
    setNewPassword("")
    setConfirmPassword("")
  }

  const handleSendCode = async () => {
    if (!identityLooksValid || !captchaToken) return
    setSubmitting(true)
    try {
      const endpoint = method === "phone" ? "/auth/phone/send-code" : "/auth/email/send-reset-code"
      const payload =
        method === "phone"
          ? { phone_number: normalizedPhone, captcha_token: captchaToken }
          : { email: normalizedEmail, captcha_token: captchaToken }
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || t("toastSendCodeFailed"))
        return
      }
      toast.success(method === "phone" ? t("toastSmsCodeSent") : t("toastEmailCodeSent"))
      setStep("reset")
    } catch {
      toast.error(t("toastNetworkError"))
    } finally {
      setSubmitting(false)
    }
  }

  const handleReset = async (e: React.FormEvent) => {
    e.preventDefault()
    const cleanCode = code.trim()
    if (!cleanCode) {
      toast.error(t("toastEnterCode"))
      return
    }
    if (newPassword.length < MIN_PASSWORD_LENGTH) {
      toast.error(t("toastPasswordMinLength", { min: MIN_PASSWORD_LENGTH }))
      return
    }
    if (newPassword !== confirmPassword) {
      toast.error(t("toastPasswordMismatch"))
      return
    }
    setSubmitting(true)
    try {
      const endpoint = method === "phone" ? "/auth/phone/reset-password" : "/auth/email/reset-password"
      const payload =
        method === "phone"
          ? {
              phone_number: normalizedPhone,
              code: cleanCode,
              new_password: newPassword,
            }
          : {
              email: normalizedEmail,
              code: cleanCode,
              new_password: newPassword,
            }
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || t("toastResetFailed"))
        return
      }
      toast.success(t("toastResetSuccess"))
      await new Promise((r) => setTimeout(r, 300))
      // Locale-aware default target (UI-04 Step 5.6): keep the /en funnel on /en
      // after a hard reload. zh stays bare per localePrefix:"as-needed".
      window.location.replace(
        locale && locale !== "zh" ? `/${locale}/translations/new` : "/translations/new",
      )
    } catch {
      toast.error(t("toastNetworkError"))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="w-full">
      <div className="mb-6 text-center sm:mb-8">
        <div className="mb-4 flex justify-center">
          <BrandMark size={44} />
        </div>
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">
          {t("title")}
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {t("subtitle")}
        </p>
      </div>

      <div className="rounded-2xl border border-border bg-card p-4 shadow-sm sm:p-8">
        {step === "identity" && (
          <div className="space-y-5">
            <div className="grid grid-cols-2 rounded-lg border border-border bg-muted/40 p-1">
              <button
                type="button"
                onClick={() => switchMethod("phone")}
                className={cn(
                  "flex h-9 items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors",
                  method === "phone"
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                <Phone className="h-4 w-4" aria-hidden="true" />
                {t("phoneLabel")}
              </button>
              <button
                type="button"
                onClick={() => switchMethod("email")}
                className={cn(
                  "flex h-9 items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors",
                  method === "email"
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                <Mail className="h-4 w-4" aria-hidden="true" />
                {t("emailLabel")}
              </button>
            </div>

            {method === "phone" ? (
              <div className="space-y-2">
                <Label htmlFor="reset-phone" className="text-sm font-medium text-foreground">
                  {t("phoneLabel")}
                </Label>
                <div className="relative">
                  <Phone
                    className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                    aria-hidden="true"
                  />
                  <Input
                    id="reset-phone"
                    type="tel"
                    inputMode="numeric"
                    placeholder={t("phonePlaceholder")}
                    value={phone}
                    onChange={(e) => setPhone(e.target.value)}
                    className="h-11 pl-10"
                    required
                  />
                </div>
              </div>
            ) : (
              <div className="space-y-2">
                <Label htmlFor="reset-email" className="text-sm font-medium text-foreground">
                  {t("emailLabel")}
                </Label>
                <div className="relative">
                  <Mail
                    className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                    aria-hidden="true"
                  />
                  <Input
                    id="reset-email"
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
            )}

            <CaptchaGate
              key={method}
              scenario="login"
              verified={Boolean(captchaToken)}
              onVerify={(token) => setCaptchaToken(token)}
              disabled={submitting}
            />
            <Button
              type="button"
              className="h-11 w-full"
              disabled={submitting || !identityLooksValid || !captchaToken}
              onClick={handleSendCode}
            >
              {submitting ? t("sending") : t("sendCode")}
            </Button>
          </div>
        )}

        {step === "reset" && (
          <form onSubmit={handleReset} className="space-y-5">
            <div className="rounded-lg border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
              {t.rich("codeSentTo", {
                identity: method === "phone" ? normalizedPhone : normalizedEmail,
                highlight: (chunks) => (
                  <span className="font-medium text-foreground">{chunks}</span>
                ),
              })}
            </div>
            <div className="space-y-2">
              <Label htmlFor="reset-code" className="text-sm font-medium text-foreground">
                {t("codeLabel")}
              </Label>
              <div className="relative">
                <KeyRound
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                  aria-hidden="true"
                />
                <Input
                  id="reset-code"
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder={method === "phone" ? t("codePlaceholderPhone") : t("codePlaceholderEmail")}
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\s/g, "").slice(0, 16))}
                  className="h-11 pl-10 tracking-widest"
                  required
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="new-pw" className="text-sm font-medium text-foreground">
                {t("newPasswordLabel")}
              </Label>
              <div className="relative">
                <Lock
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                  aria-hidden="true"
                />
                <Input
                  id="new-pw"
                  type="password"
                  autoComplete="new-password"
                  placeholder={t("newPasswordPlaceholder", { min: MIN_PASSWORD_LENGTH })}
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  className="h-11 pl-10"
                  required
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="confirm-pw" className="text-sm font-medium text-foreground">
                {t("confirmPasswordLabel")}
              </Label>
              <div className="relative">
                <Lock
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                  aria-hidden="true"
                />
                <Input
                  id="confirm-pw"
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
              {submitting ? t("resetting") : t("resetPassword")}
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="h-10 w-full"
              disabled={submitting}
              onClick={() => {
                setStep("identity")
                setCaptchaToken(null)
                setCode("")
              }}
            >
              {t("resendCode")}
            </Button>
          </form>
        )}

        <div className="mt-6 border-t border-border pt-5 text-center text-xs text-muted-foreground">
          {t("footnote")}
        </div>
      </div>

      <div className="mt-6 text-center text-sm text-muted-foreground">
        <Link
          href="/auth"
          className="text-primary transition-colors hover:text-primary/80"
        >
          {t("backToLogin")}
        </Link>
      </div>
    </div>
  )
}
