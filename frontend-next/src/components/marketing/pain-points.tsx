import { getTranslations } from "next-intl/server"
import { InkDivider } from "./ink-divider"

/**
 * Pain points section — sits between Hero and ProductProof.
 *
 * Recasts the conversion narrative around what users actually struggle with
 * when localising long-form videos: length, fragility of edits, narrow
 * output formats, and runaway cost. Each bullet leads with a "用户在哪卡住"
 * line and lands on the implicit "我们正好把这件事做了" close.
 *
 * Replaces the previous in-Hero 钩子 ("口型对不上 / 改一句要重跑全片 /
 * 剪辑师拿到的是死字幕") which over-promised lip-sync and over-emphasised
 * the 剪映 export angle. New copy is honest about what AI翻译配音 actually
 * fixes and what users still control.
 */
export async function PainPoints() {
  const t = await getTranslations("marketing.painPoints")
  const points = t.raw("points") as Array<{ title: string; body: string }>

  return (
    <section className="marketing-reading-surface py-14 sm:py-16">
      <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-3xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar)]">
            {t("eyebrow")}
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            {t("heading")}
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            {t("lead")}
          </p>
        </div>

        <div className="mt-10 mx-auto max-w-3xl">
          <InkDivider variant={1} className="text-foreground/35" />
        </div>

        <ol className="mt-12 grid gap-5 md:grid-cols-2" role="list">
          {points.map((p, i) => (
            <li
              key={i}
              className="rounded-xl border border-border bg-card p-6 transition-shadow hover:shadow-md"
            >
              <div className="flex items-start gap-3">
                <span
                  className="ink-num shrink-0 text-base font-bold"
                  style={{ color: "var(--cinnabar)" }}
                >
                  {String(i + 1).padStart(2, "0")}
                </span>
                <div>
                  <h3 className="ink-heading text-lg font-semibold text-foreground">
                    {p.title}
                  </h3>
                  <p className="mt-2 zh-body text-sm leading-relaxed text-muted-foreground">
                    {p.body}
                  </p>
                </div>
              </div>
            </li>
          ))}
        </ol>

        <p className="mx-auto mt-12 max-w-3xl text-center zh-body text-foreground/85">
          {t.rich("closing", {
            strong: (chunks) => (
              <strong className="text-foreground">{chunks}</strong>
            ),
          })}
        </p>
      </div>
    </section>
  )
}
