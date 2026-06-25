import { Check, Minus } from "lucide-react"
import { getTranslations } from "next-intl/server"
import { InkDivider } from "./ink-divider"

type ComparisonRow = {
  dimension: string
  oneClick: string
  workbench: string
}

/**
 * Tool comparison — "为什么比一键生成工具更适合长视频".
 *
 * Sits after SuitedScenarios. Goal: clarify positioning vs the broader class
 * of "AI video translation" tools without naming any competitor by brand
 * (FAQ already references Rask/HeyGen/ElevenLabs by name; this surface
 * intentionally stays category-level so it's not read as an attack).
 *
 * Four-row table is rendered as a 2-column card grid on desktop + stacked on
 * mobile — keeps both columns equally readable at any width. Each row is the
 * same dimension shown on both sides so the contrast stays clean.
 *
 * Copy from ChatGPT marketing consult (2026-05-01).
 *
 * i18n (UI-03a): rows + section chrome live in messages/{zh,en}/marketing.json
 * under `marketing.comparison`. Server component → getTranslations.
 */
export async function ToolComparison() {
  const t = await getTranslations("marketing.comparison")
  const rows = t.raw("rows") as ComparisonRow[]

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
          <InkDivider variant={0} className="text-foreground/35" />
        </div>

        {/* Header row — only visible at md+ to anchor the columns. On mobile
            each row already labels its two halves, so the header would be
            redundant and crowd the viewport. */}
        <div className="mt-12 hidden grid-cols-[1fr_1.4fr_1.4fr] gap-4 px-2 text-xs font-semibold uppercase tracking-widest text-muted-foreground md:grid">
          <div>{t("headerDimension")}</div>
          <div className="text-foreground/85">{t("headerOneClick")}</div>
          <div
            className="ink-heading"
            style={{ color: "var(--cinnabar)" }}
          >
            {t("headerWorkbench")}
          </div>
        </div>

        <ul className="mt-4 space-y-3" role="list">
          {rows.map((row) => (
            <li
              key={row.dimension}
              className="grid gap-4 rounded-xl border border-border bg-card p-5 md:grid-cols-[1fr_1.4fr_1.4fr] md:items-start md:p-6"
            >
              <div className="ink-heading text-sm font-semibold text-foreground md:text-base">
                {row.dimension}
              </div>
              <div className="flex items-start gap-2 text-sm text-muted-foreground">
                <Minus
                  className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground/60"
                  aria-hidden="true"
                />
                <div>
                  <span className="md:hidden text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    {t("labelOneClick")}
                  </span>
                  <span className="zh-body leading-relaxed">{row.oneClick}</span>
                </div>
              </div>
              <div
                className="flex items-start gap-2 text-sm"
                style={{ color: "var(--ink-black, #1A1A1A)" }}
              >
                <Check
                  className="mt-0.5 h-4 w-4 shrink-0"
                  style={{ color: "var(--cinnabar)" }}
                  aria-hidden="true"
                />
                <div>
                  <span
                    className="md:hidden text-xs font-semibold uppercase tracking-wider"
                    style={{ color: "var(--cinnabar)" }}
                  >
                    {t("labelWorkbench")}
                  </span>
                  <span className="zh-body leading-relaxed text-foreground/90">
                    {row.workbench}
                  </span>
                </div>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
