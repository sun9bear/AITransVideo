/**
 * Inline JSON-LD `<script>` for schema.org structured data.
 *
 * Server-rendered so the schema lands in the initial HTML, not after client
 * hydration — Google's documented expectation. The `id` attribute lets
 * search engines dedupe when multiple schema blocks coexist on a page;
 * pass a stable string per schema type per page.
 *
 * `dangerouslySetInnerHTML` is safe here because:
 *   - Callers pass plain JS objects built in trusted server code
 *   - JSON.stringify produces well-formed JSON, which is also valid JS
 *     inside a `<script type="application/ld+json">` tag
 *   - schema.org field values (URLs, names) don't contain `</script` byte
 *     sequences in our usage
 */
type Props = {
  id: string
  data: object
}

export function JsonLd({ id, data }: Props) {
  return (
    <script
      type="application/ld+json"
      id={id}
      dangerouslySetInnerHTML={{ __html: JSON.stringify(data) }}
    />
  )
}
