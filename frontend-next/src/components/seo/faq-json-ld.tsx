import { JsonLd } from "./json-ld"

export type FaqItem = { q: string; a: string }

/**
 * FAQPage structured data — emits one schema.org FAQPage block summarizing
 * a Q&A list. Mounted alongside the visible FAQ section so the schema and
 * DOM stay byte-aligned (Google's FAQ guideline: "all FAQ content must be
 * visible on the page from which the schema is referenced"; AI search
 * engines like Perplexity / ChatGPT lean on this structure for direct
 * Q&A citation in answers).
 *
 * Pass `id` per page when the same component is rendered on more than one
 * URL (e.g. home + pricing) — distinct IDs let search engines dedupe
 * cleanly. Returns `null` for empty input rather than emitting an empty
 * schema, which Google rejects as malformed.
 *
 * Note on Google policy (2023 update): regular sites lost FAQ rich-snippet
 * eligibility in standard Google search results, but the schema still
 * carries weight for AI Overviews / AI Mode and for non-Google AI engines
 * (Bing Copilot, Perplexity, Claude, Baidu 文心一言 reach-back). Cost is a
 * few hundred bytes of inline JSON; expected upside is non-zero. Net
 * positive even with the rich-snippet pullback.
 */
export function FaqJsonLd({
  items,
  id = "ld-faq",
}: {
  items: readonly FaqItem[]
  id?: string
}) {
  if (items.length === 0) return null

  const data = {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: items.map((item) => ({
      "@type": "Question",
      name: item.q,
      acceptedAnswer: {
        "@type": "Answer",
        text: item.a,
      },
    })),
  }

  return <JsonLd id={id} data={data} />
}
