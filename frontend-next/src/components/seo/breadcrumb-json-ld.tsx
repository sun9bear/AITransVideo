import { absoluteUrl } from "@/lib/seo/site"
import { JsonLd } from "./json-ld"

export type BreadcrumbItem = {
  /** Display label, matches the visible nav text on the page. */
  name: string
  /** Path relative to siteUrl (e.g. `/pricing`). The last item is usually the current page. */
  path: string
}

/**
 * BreadcrumbList structured data — emit on pricing / trial / legal pages
 * and Phase 2 guide / compare / use-cases pages, anywhere a visible
 * breadcrumb or hierarchy is implied.
 *
 * `items` order is root → leaf. Schema rejects single-item lists, so
 * callers must pass at least 2 entries (typically Home + current page).
 */
export function BreadcrumbJsonLd({ items, id }: { items: BreadcrumbItem[]; id?: string }) {
  if (items.length < 2) return null

  const data = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: items.map((item, idx) => ({
      "@type": "ListItem",
      position: idx + 1,
      name: item.name,
      item: absoluteUrl(item.path),
    })),
  }

  return <JsonLd id={id ?? "ld-breadcrumb"} data={data} />
}
