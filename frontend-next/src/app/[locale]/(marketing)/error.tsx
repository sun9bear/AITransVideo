"use client"

import { useEffect } from "react"
import { useTranslations } from "next-intl"
import { Link } from "@/i18n/navigation"
import { AlertTriangle, RefreshCw } from "lucide-react"

export default function MarketingError({
  error,
  reset,
}: {
  error: Error & { digest?: string }
  reset: () => void
}) {
  const t = useTranslations("common")

  useEffect(() => {
    console.error("Unhandled marketing page error", error)
  }, [error])

  return (
    <section className="mx-auto flex min-h-[56vh] w-full max-w-3xl flex-col justify-center px-4 py-16">
      <div className="rounded-lg border border-[color:var(--ink-line,#D8CBBB)] bg-[color:var(--ink-paper,#F7F1E6)] p-6 shadow-sm">
        <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-full bg-[color:var(--cinnabar-soft,rgba(199,62,58,0.08))] text-[color:var(--cinnabar,#C73E3A)]">
          <AlertTriangle className="h-5 w-5" aria-hidden="true" />
        </div>
        <p className="text-sm font-medium text-[color:var(--muted-foreground)]">{t("error.marketing.eyebrow")}</p>
        <h1 className="mt-2 text-2xl font-semibold text-[color:var(--ink-black,#1A1A1A)]">
          {t("error.marketing.title")}
        </h1>
        <p className="mt-3 text-sm leading-6 text-[color:var(--ink-gray,#5B5B5B)]">
          {t("error.marketing.description")}
        </p>
        <div className="mt-6 flex flex-wrap gap-3">
          <button
            type="button"
            onClick={reset}
            className="inline-flex h-9 items-center justify-center gap-2 rounded-md bg-[color:var(--cinnabar,#C73E3A)] px-4 text-sm font-medium text-white transition hover:bg-[color:var(--cinnabar-dark,#A9312E)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--cinnabar,#C73E3A)]"
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            {t("error.retry")}
          </button>
          <Link
            href="/"
            className="inline-flex h-9 items-center justify-center rounded-md border border-[color:var(--ink-line,#D8CBBB)] bg-[color:var(--ink-paper-2,#EDE6D6)] px-4 text-sm font-medium text-[color:var(--ink-black,#1A1A1A)] transition hover:text-[color:var(--cinnabar,#C73E3A)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--cinnabar,#C73E3A)]"
          >
            {t("error.marketing.actionLabel")}
          </Link>
        </div>
      </div>
    </section>
  )
}
