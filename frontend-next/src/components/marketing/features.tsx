import { Clock, Package2, Pencil, RefreshCw, Coins, Film } from "lucide-react"
import { getTranslations } from "next-intl/server"

/**
 * Six core value proofs — re-prioritised 2026-04-30 per ChatGPT consult,
 * with the 剪映草稿 card de-hedged 2026-05-02 after the export feature
 * formally launched.
 *
 * Long-video support is the lead because it's the most differentiated
 * thing the product does (most competitors cap at 30–60 min). 剪映草稿
 * sits at position #6 as a workflow assist — it's a real shipped feature
 * now, but doesn't carry the "you absolutely need this product" weight
 * that the first 5 cards do (length, editability, incremental re-gen,
 * price, multi-format).
 *
 * Tone is benefit-first, not feature-first. No "DSP 级帧级对齐" or other
 * technical claims that a) require validation we can't yet ship and b) read
 * as marketing aggrandisement to the actual user base (Chinese knowledge
 * creators). Concrete use-cases (访谈/课程/播客) replace abstract adjectives.
 */
const FEATURE_ICONS = [Clock, Package2, Pencil, RefreshCw, Coins, Film]

export async function Features() {
  const t = await getTranslations("marketing.features")
  const items = t.raw("items") as Array<{ title: string; body: string }>

  return (
    <section className="marketing-reading-surface py-14 sm:py-16">
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

        <div className="mt-14 grid gap-6 md:grid-cols-2 lg:grid-cols-3">
          {items.map((f, i) => {
            const Icon = FEATURE_ICONS[i]
            return (
              <article
                key={i}
                className="rounded-xl border border-border bg-card p-6 transition-shadow hover:shadow-md"
              >
                <div className="flex items-center gap-3">
                  <div
                    className="flex h-10 w-10 items-center justify-center rounded-lg"
                    style={{
                      backgroundColor: "var(--cinnabar-soft)",
                      color: "var(--cinnabar)",
                    }}
                  >
                    <Icon className="h-5 w-5" aria-hidden="true" />
                  </div>
                  <span
                    className="ink-num text-xs font-bold"
                    style={{ color: "var(--cinnabar)" }}
                  >
                    {String(i + 1).padStart(2, "0")}
                  </span>
                </div>
                <h3 className="ink-heading mt-5 text-lg font-semibold text-foreground">
                  {f.title}
                </h3>
                <p className="mt-2 zh-body text-muted-foreground text-[0.95rem]">
                  {f.body}
                </p>
              </article>
            )
          })}
        </div>
      </div>
    </section>
  )
}
