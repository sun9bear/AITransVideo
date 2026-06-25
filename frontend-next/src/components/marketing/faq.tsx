import { getTranslations } from "next-intl/server"
import { FaqJsonLd, type FaqItem } from "@/components/seo/faq-json-ld"
import { Link } from "@/i18n/navigation"

/**
 * FAQ section. DESIGN.md §3.5 — pricing / FAQ surfaces should stay on neutral,
 * high-contrast reading backgrounds for dense Chinese text.
 *
 * Question order follows plan §5.2 第三幕「信任」: pre-purchase doubts come first
 * (视频来源 → Express vs Studio), then post-purchase mechanics (增量重生成 →
 * 导出格式). The reordering matches the conversion funnel — visitors hesitate on
 * "can I even use this?" before "how do I get my output back?".
 *
 * Answers intentionally avoid any numeric trial facts (days / minutes / Studio
 * inclusion) while those values remain unfrozen in the gateway catalog.
 * Answers that reference plan-specific numbers should be added after Task 0's
 * trial config is flipped to `frozen = true`.
 *
 * SEO: FAQPage JSON-LD schema is emitted inline next to the visible Q&A
 * markup so the schema and DOM stay byte-aligned — Google's FAQ guideline
 * requires the structured-data Q&A to mirror what users actually see.
 * AI search engines (Perplexity, ChatGPT, Claude, Bing Copilot) lean on
 * FAQPage for direct Q&A citation in answers; even after Google's 2023
 * rich-snippet pullback the AI-side upside still beats the few hundred
 * bytes of inline JSON.
 *
 * i18n (UI-03a): Q&A copy lives in messages/{zh,en}/marketing.json under
 * `marketing.faq`. The visible DOM and the FaqJsonLd `items` read the SAME
 * `items` array (built once below) so the schema and DOM stay byte-aligned
 * per-locale (不变量 5). `general` is the 8-item home set; `pricing` appends
 * `pricingExtra` (mirrors the old `PRICING_FAQ = [...GENERAL_FAQ, ...3]`).
 */
export async function Faq({ variant = "home" }: { variant?: "home" | "pricing" }) {
  const t = await getTranslations("marketing.faq")
  const general = t.raw("general") as FaqItem[]
  const pricingExtra = t.raw("pricingExtra") as FaqItem[]
  const items: FaqItem[] = variant === "pricing" ? [...general, ...pricingExtra] : general

  // On the homepage, auto-scroll the Q&A cards upward in a vertical
  // marquee with hover-pause — keeps the section compact (~420px tall)
  // even with 8+ Q&A items. On /pricing the FAQ is the user's primary
  // reading surface for purchase decisions, so we keep it as a static
  // stack there (auto-scrolling forced reading pace conflicts with
  // deliberate research). 2026-05-02 user request.
  const useMarquee = variant === "home"

  // For the marquee variant we render the items twice in DOM so the
  // keyframe loop seam lands on identical content.
  const renderItems = useMarquee ? [...items, ...items] : items

  return (
    <section
      id="faq"
      className="marketing-reading-surface py-14 sm:py-16"
    >
      <FaqJsonLd items={items} id={`ld-faq-${variant}`} />
      <div className="mx-auto max-w-3xl px-4 sm:px-6 lg:px-8">
        <div className="text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            {t("eyebrow")}
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            {t("heading")}
          </h2>
          {useMarquee && (
            <p className="mt-3 text-xs text-muted-foreground">
              {t("marqueeHint")}
              <Link
                href="/pricing#faq"
                className="ml-1 underline-offset-2 hover:underline"
                style={{ color: "var(--cinnabar)" }}
              >
                {t("marqueeHintLink")}
              </Link>
            </p>
          )}
        </div>

        {useMarquee ? (
          <div className="vmarquee-container relative mt-8 h-[420px] overflow-hidden sm:h-[460px]">
            <div
              className="vmarquee-track flex flex-col gap-4"
              role="list"
              aria-label={t("ariaList")}
            >
              {renderItems.map((item, i) => {
                const isDuplicate = i >= items.length
                return (
                  <article
                    key={`${item.q}-${isDuplicate ? "duplicate" : "primary"}`}
                    role="listitem"
                    aria-hidden={isDuplicate ? true : undefined}
                    className="rounded-xl border border-border bg-card p-6"
                  >
                    <h3 className="ink-heading text-base font-semibold text-foreground">{item.q}</h3>
                    <p className="mt-2 zh-body text-muted-foreground">{item.a}</p>
                  </article>
                )
              })}
            </div>
          </div>
        ) : (
          <dl className="mt-8 space-y-4">
            {items.map((item) => (
              <div
                key={item.q}
                className="rounded-xl border border-border bg-card p-6 transition-shadow hover:shadow-md"
              >
                <dt className="ink-heading text-base font-semibold text-foreground">{item.q}</dt>
                <dd className="mt-2 zh-body text-muted-foreground">{item.a}</dd>
              </div>
            ))}
          </dl>
        )}

        {/* "Still have questions?" support entry — plan 2026-05-08 §8.3.
            The actual SupportWidget mounts in (marketing)/layout.tsx; this
            paragraph just nudges users toward it. */}
        <p className="mt-8 text-center text-sm text-muted-foreground">
          {t("supportNudge")}
        </p>
      </div>
    </section>
  )
}
