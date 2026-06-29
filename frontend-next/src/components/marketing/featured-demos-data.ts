import type { Demo } from "./featured-demo-card"
import demosJson from "../../../public/marketing/demos/featured-demos.json"

/**
 * Shared loader for the marketing sample-demo catalog.
 *
 * Both the homepage carousel (`FeaturedDemos`) and the hero sample player
 * (`Hero` → `HeroSamplePlayer`) read the SAME `featured-demos.json`, so the
 * parse + validation lives here once instead of being duplicated per consumer.
 *
 * Next.js statically inlines this small JSON at build time:
 *   - File missing → build-time module-resolution error (fails the build)
 *   - File malformed JSON → build-time parse error (fails the build)
 *   - File parses but `demos` is empty / not an array → returns [] at runtime;
 *     consumers decide how to degrade (carousel + hero player both hide).
 *
 * Phase 2 swap: replace the static import with a server-side fetch from
 * GET /api/featured-demos returning the same { version, demos } shape.
 *
 * Content policy (UI page locale / uiloc): each demo's `display_name`,
 * `source_label`, and `segment_label` are REAL sample-video metadata — content,
 * not UI chrome. They are intentionally NOT localized and render as authored on
 * `/en` too (a Chinese demo title is that real video's actual title). They live in
 * public/marketing/demos/featured-demos.json, outside the cjk-guard `src/` scan, so
 * no allowlist entry is needed. If per-locale demo titles are ever wanted, add a
 * `display_name_en` field to the JSON + Demo type rather than translating in place.
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

export function loadFeaturedDemos(): Demo[] {
  const config = demosJson as DemosConfig
  return Array.isArray(config?.demos) ? config.demos.filter(isValidDemo) : []
}
