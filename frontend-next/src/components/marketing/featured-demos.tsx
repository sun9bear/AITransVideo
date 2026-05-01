import { FeaturedDemosClient } from "./featured-demos-client"
import type { Demo } from "./featured-demo-card"
import demosJson from "../../../public/marketing/demos/featured-demos.json"

/**
 * FeaturedDemos — homepage carousel of real dubbed clips.
 *
 * Server Component. Statically imports the JSON config (Next.js inlines
 * small JSON files at build time). Validates the parse result is a list of
 * objects with the required shape, and:
 *
 *   - File missing → build-time module-resolution error (fails the build)
 *   - File malformed JSON → build-time parse error (fails the build)
 *   - File parses but `demos` is empty / not an array → returns null at
 *     runtime; section disappears silently from the page
 *
 * Phase 2 swap: replace the static import with a server-side fetch from
 * GET /api/featured-demos returning the same { version, demos } shape.
 *
 * See: docs/specs/2026-05-01-marketing-featured-demos-design.md
 */

type DemosConfig = {
  version: number
  demos: Demo[]
}

function isValidDemo(d: unknown): d is Demo {
  if (typeof d !== "object" || d === null) return false
  const o = d as Record<string, unknown>
  return (
    typeof o.id === "string" &&
    typeof o.display_name === "string" &&
    typeof o.source_label === "string" &&
    typeof o.segment_label === "string" &&
    typeof o.original_src === "string" &&
    typeof o.dubbed_src === "string" &&
    typeof o.poster_src === "string" &&
    typeof o.natural_width === "number" &&
    typeof o.natural_height === "number"
  )
}

export function FeaturedDemos() {
  const config = demosJson as DemosConfig
  const demos = Array.isArray(config?.demos) ? config.demos.filter(isValidDemo) : []
  if (demos.length === 0) return null
  return <FeaturedDemosClient demos={demos} />
}
