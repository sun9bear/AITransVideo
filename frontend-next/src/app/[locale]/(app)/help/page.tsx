/**
 * Help center landing page.
 *
 * Plan 2026-05-08 §13 P1 — replace the "正在开发中" placeholder with a
 * real entry that points users at:
 *
 *   1. The most-asked FAQ topics (links to the marketing FAQ section).
 *   2. The on-page support widget (rendered by AppShell — we just nudge
 *      the user toward it; the floating launcher remains the canonical
 *      entry point).
 *   3. Direct contact channels for billing / privacy / copyright issues.
 *
 * The widget itself is mounted globally by AppShell, so this page does
 * not import SupportWidget directly.
 */

import { getTranslations } from "next-intl/server"

import { Link } from "@/i18n/navigation"

const HELP_TOPICS: Array<{
  key:
    | "billing"
    | "uploadAuth"
    | "expressStudio"
    | "jianyingDraft"
    | "failureTroubleshooting"
    | "refundPrivacy"
  href?: string
}> = [
  { key: "billing", href: "/pricing#faq" },
  { key: "uploadAuth", href: "/#faq" },
  { key: "expressStudio", href: "/#faq" },
  { key: "jianyingDraft", href: "/#faq" },
  { key: "failureTroubleshooting" },
  { key: "refundPrivacy", href: "/contact" },
]

export default async function HelpPage() {
  const t = await getTranslations("appHelp")
  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <header className="space-y-2">
        <h1 className="ink-display text-2xl text-foreground sm:text-3xl">
          {t("header.title")}
        </h1>
        <p className="text-sm text-muted-foreground">
          {t("header.intro")}
        </p>
      </header>

      <ul className="grid gap-4 sm:grid-cols-2">
        {HELP_TOPICS.map((topic) => {
          const card = (
            <article className="h-full rounded-xl border border-border bg-card p-4 transition-shadow hover:shadow-md">
              <h2 className="text-base font-semibold text-foreground">
                {t(`topics.${topic.key}.title`)}
              </h2>
              <p className="mt-2 text-sm text-muted-foreground">
                {t(`topics.${topic.key}.body`)}
              </p>
            </article>
          )
          return (
            <li key={topic.key}>
              {topic.href ? (
                <Link href={topic.href} className="block h-full">
                  {card}
                </Link>
              ) : (
                card
              )}
            </li>
          )
        })}
      </ul>

      <section className="rounded-xl border border-border bg-card p-4 text-sm">
        <h2 className="text-base font-semibold text-foreground">
          {t("contact.title")}
        </h2>
        <p className="mt-2 text-muted-foreground">
          {t("contact.intro")}
        </p>
        <p className="mt-2 text-muted-foreground">
          {t("contact.emailPrefix")}{" "}
          <a
            className="underline-offset-2 hover:underline"
            href="mailto:sxz999@proton.me"
          >
            sxz999@proton.me
          </a>
          {t("contact.emailSuffix")}
        </p>
      </section>
    </div>
  )
}
