/**
 * Inline JSON-LD `<script>` for schema.org structured data.
 *
 * Server-rendered so the schema lands in the initial HTML, not after client
 * hydration — Google's documented expectation. The `id` attribute lets
 * search engines dedupe when multiple schema blocks coexist on a page;
 * pass a stable string per schema type per page.
 *
 * Defense-in-depth escape: `<` is replaced with `<` in the serialized
 * payload. Today's callers pass trusted server-side constants, but if a
 * future schema embeds user-derived content (e.g. a Review snippet pulled
 * from a DB), an unescaped `</script>` substring could break out of the
 * script tag. The escape costs nothing and removes a class of footgun.
 */
type Props = {
  id: string
  data: object
}

function serialize(data: object): string {
  return JSON.stringify(data).replace(/</g, "\\u003c")
}

export function JsonLd({ id, data }: Props) {
  return (
    <script
      type="application/ld+json"
      id={id}
      dangerouslySetInnerHTML={{ __html: serialize(data) }}
    />
  )
}
