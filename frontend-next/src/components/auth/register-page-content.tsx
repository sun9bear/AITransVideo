"use client"

import { Suspense } from "react"
import { useTranslations } from "next-intl"
import { Link } from "@/i18n/navigation"
import { BrandMark } from "@/components/marketing/brand-mark"
import { RegisterMethodForm } from "@/components/auth/register-method-form"

/**
 * Shared registration page body, rendered by both /auth (primary "免费开始试用"
 * entry) and /auth/register. Deduped in UI-04 so the registration chrome lives
 * in one place before strings were localized — otherwise the same copy would be
 * mirrored into the catalog twice and drift.
 *
 * Phone verification + set password for new users. Existing users who verify
 * phone are auto-logged in.
 */
export function RegisterPageContent() {
  const t = useTranslations("auth")

  return (
    <div className="w-full">
      <div className="mb-6 flex items-center justify-between">
        <Link href="/" className="inline-flex items-center" aria-label={t("brandHomeAria")}>
          <BrandMark size={44} />
        </Link>
        <Link
          href="/auth/login"
          className="inline-flex h-9 items-center justify-center rounded-lg border border-border bg-card px-3 text-sm font-medium text-foreground shadow-sm transition-colors hover:border-primary/50 hover:text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
        >
          {t("register.loginLink")}
        </Link>
      </div>

      <div className="mb-6 text-center sm:mb-8">
        <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
          {t("register.eyebrow")}
        </p>
        <h1 className="ink-display mt-2 text-3xl tracking-tight text-foreground sm:text-4xl">
          {t("register.title")}
        </h1>
        <p className="mt-3 zh-body text-sm text-muted-foreground">
          {t("register.subtitle")}
        </p>
      </div>

      <div className="rounded-2xl border border-border bg-card p-4 shadow-sm sm:p-8">
        <Suspense fallback={null}>
          <RegisterMethodForm />
        </Suspense>

        <div className="mt-6 border-t border-border pt-5 text-center text-sm text-muted-foreground">
          {t("register.haveAccount")}
          <Link href="/auth/login" className="ml-1 text-primary hover:underline">
            {t("register.backToLogin")}
          </Link>
        </div>
      </div>
    </div>
  )
}
