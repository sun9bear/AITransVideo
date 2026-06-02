import { FeaturedDemosClient } from "./featured-demos-client"
import { loadFeaturedDemos } from "./featured-demos-data"

/**
 * FeaturedDemos — homepage carousel of real dubbed clips.
 *
 * Server Component. Reads the validated demo catalog via the shared
 * `loadFeaturedDemos()` loader (same source the hero sample player uses) and
 * hands it to the "use client" carousel shell.
 *
 *   - empty / invalid catalog → section disappears silently from the page
 *
 * See: docs/specs/2026-05-01-marketing-featured-demos-design.md
 */
export function FeaturedDemos() {
  const demos = loadFeaturedDemos()
  if (demos.length === 0) return null
  return <FeaturedDemosClient demos={demos} />
}
