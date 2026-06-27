import { getTranslations } from "next-intl/server"
import { InkDivider } from "./ink-divider"

/**
 * Suited scenarios — "is this product for me?".
 *
 * Sits between Features and ToolComparison. Goal: high-代入感. After visitors
 * see the workflow + features, they need a beat to recognise themselves in
 * the use cases. Keeping this surface compact (5 short tag+body pairs) so it
 * reads as a sanity-check, not a sales pitch.
 *
 * Copy adapted from ChatGPT marketing consult (2026-05-01). Five buckets
 * intentionally chosen to span solo creators → MCN → B2B content teams.
 *
 * No icons — the typographic tag style keeps the section airy and aligned
 * with the ink-aesthetic. Each card is plain border + cinnabar tag + body.
 */
export async function SuitedScenarios() {
  const t = await getTranslations("marketing.suitedScenarios")
  const scenarios = t.raw("scenarios") as Array<{ tag: string; body: string }>

  return (
    <section className="marketing-reading-surface py-14 sm:py-16">
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-3xl text-center">
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

        <div className="mt-10">
          <InkDivider variant={2} className="text-foreground/35" />
        </div>

        <ul
          className="mt-12 grid gap-5 md:grid-cols-2 lg:grid-cols-3"
          role="list"
        >
          {scenarios.map((s, i) => (
            <li
              key={i}
              className="rounded-xl border border-border bg-card p-6 transition-shadow hover:shadow-md"
            >
              <span
                className="ink-heading inline-flex items-center rounded-md px-2.5 py-1 text-xs font-semibold uppercase tracking-wider"
                style={{
                  backgroundColor: "var(--cinnabar-soft)",
                  color: "var(--cinnabar)",
                }}
              >
                {s.tag}
              </span>
              <p className="mt-4 zh-body text-sm leading-relaxed text-muted-foreground">
                {s.body}
              </p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
