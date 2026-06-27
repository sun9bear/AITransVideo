/**
 * "How it works" section — a short four-step workflow explanation.
 * DESIGN.md §3.2 — between features and pricing.
 *
 * No mockup image yet; when Task 2 gets a real demo asset, drop it into the
 * right column. The placeholder below stays intentionally quiet (no dramatic
 * animation, no hero-style oversized type).
 */
import { getTranslations } from "next-intl/server"

export async function WorkflowShowcase() {
  const t = await getTranslations("marketing.workflowShowcase")
  const steps = t.raw("steps") as Array<{ title: string; body: string }>

  return (
    <section className="bg-muted/40 py-14 sm:py-16">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-2xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            {t("eyebrow")}
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            {t("heading")}
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            {t("lead")}
          </p>
        </div>

        <ol className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-4" role="list">
          {steps.map((step, i) => (
            <li
              key={i}
              className="rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-md"
            >
              <span className="ink-num text-base font-bold text-[color:var(--cinnabar,#C73E3A)]">
                {String(i + 1).padStart(2, "0")}
              </span>
              <h3 className="ink-heading mt-2 text-base font-semibold text-foreground">
                {step.title}
              </h3>
              <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                {step.body}
              </p>
            </li>
          ))}
        </ol>
      </div>
    </section>
  )
}
