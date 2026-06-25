"use client"

import { useState } from "react"
import { useSearchParams } from "next/navigation"
import { useLocale, useTranslations } from "next-intl"
import { Link } from "@/i18n/navigation"
import { toast } from "sonner"
import { User, Lock } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { goToPostAuthRedirect, resolvePostAuthRedirect } from "@/lib/auth/post-auth-redirect"

// Brand origin echoed verbatim in the CSRF-origin error (content, not chrome).
const APP_ORIGIN = "https://aitrans.video"

/**
 * Server-emitted login error codes we can localize. Today the backend only
 * emits `csrf_origin_rejected` as a stable code; every other auth failure
 * arrives as bare Chinese prose in `detail` and is shown verbatim until the
 * Phase 4 backend error-code/Accept-Language track lands (UI-04 known gap,
 * plan §1.9). Add a code here + a matching `auth.passwordLogin.error*` key when
 * the backend starts emitting it.
 */
const KNOWN_LOGIN_ERROR_CODES = new Set(["csrf_origin_rejected"])

/**
 * Account + password login form (A1 unified login).
 *
 * The "account" field accepts phone number OR email. Backend resolves both.
 */
export function PasswordLoginForm() {
  const t = useTranslations("auth.passwordLogin")
  const locale = useLocale()
  const searchParams = useSearchParams()
  const redirectTo = resolvePostAuthRedirect(searchParams, locale)
  const [account, setAccount] = useState("")
  const [password, setPassword] = useState("")
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!account || !password) {
      toast.error(t("toastEnterAccountPassword"))
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: account, password }),
        credentials: "include",
      })
      const data = await res.json()
      if (!res.ok) {
        const detail = typeof data.detail === "string" ? data.detail : ""
        // Known error code → localized; else server `detail` verbatim (still
        // Chinese, known gap); else localized generic fallback.
        const message = KNOWN_LOGIN_ERROR_CODES.has(detail)
          ? t("errorCsrfOriginRejected", { url: APP_ORIGIN })
          : detail || t("toastLoginFailed")
        toast.error(message)
        return
      }
      toast.success(t("toastLoginSuccess"))
      await goToPostAuthRedirect(redirectTo)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("toastNetworkError"))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      <div className="space-y-2">
        <Label htmlFor="account" className="text-sm font-medium text-foreground">
          {t("accountLabel")}
        </Label>
        <div className="relative">
          <User
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="account"
            type="text"
            inputMode="text"
            autoComplete="username"
            placeholder={t("accountPlaceholder")}
            value={account}
            onChange={(e) => setAccount(e.target.value)}
            className="h-11 pl-10"
            required
          />
        </div>
      </div>
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label htmlFor="password" className="text-sm font-medium text-foreground">
            {t("passwordLabel")}
          </Label>
          <Link
            href="/auth/forgot-password"
            className="text-xs text-muted-foreground hover:text-primary transition-colors"
          >
            {t("forgotPassword")}
          </Link>
        </div>
        <div className="relative">
          <Lock
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            placeholder={t("passwordPlaceholder")}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="h-11 pl-10"
            required
          />
        </div>
      </div>
      <Button type="submit" className="h-11 w-full" disabled={submitting}>
        {submitting ? t("loggingIn") : t("login")}
      </Button>
    </form>
  )
}
