/**
 * Fetch helper for the public `GET /api/plans` endpoint.
 *
 * This is the only supported way for the frontend to learn plan / pricing / trial
 * facts at runtime. No business-truth values should be hardcoded on the client.
 *
 * Contract reference: docs/specs/2026-04-04-pricing-and-plans-api-contract.md
 */

import { EMPTY_PLANS_RESPONSE, type PlansResponse } from "./types"

/**
 * Fetch the plan catalog from the gateway.
 *
 * - No credentials: the endpoint is public. Sending the session cookie would be
 *   harmless but unnecessary for a marketing-facing call.
 * - On failure, throws — callers should fall back to `EMPTY_PLANS_RESPONSE` or a
 *   loading / error UI, rather than hardcoding default numbers.
 */
export async function getPlans(): Promise<PlansResponse> {
  const res = await fetch("/api/plans", {
    method: "GET",
    credentials: "omit",
    headers: { Accept: "application/json" },
  })
  if (!res.ok) {
    throw new Error(`Failed to fetch plans: ${res.status} ${res.statusText}`)
  }
  const data = (await res.json()) as PlansResponse
  return data
}

/**
 * Safe variant that swallows errors and returns the empty fallback shape.
 * Useful for SSR / marketing pages that must render even when the API is down.
 */
export async function getPlansSafe(): Promise<PlansResponse> {
  try {
    return await getPlans()
  } catch {
    return EMPTY_PLANS_RESPONSE
  }
}

/**
 * Server-side variant for use inside Next.js Server Components / Route Handlers.
 *
 * Why this exists: `getPlans()` calls `fetch("/api/plans")` with a relative URL.
 * That works in browsers (relative to current page), but fails in Node-side
 * fetch which requires an absolute URL. This helper reconstructs the origin
 * from the inbound request headers (`x-forwarded-proto` + `host`) so SSR
 * pricing data lands in the initial HTML.
 *
 * Falls back to `EMPTY_PLANS_RESPONSE` on any error — pricing pages must never
 * blank out because the upstream is briefly unavailable.
 *
 * Cache strategy: reuses Next's request-scoped fetch dedup. We add a 60s
 * `revalidate` so pricing changes propagate quickly without hammering the
 * gateway on every page render.
 *
 * MUST be called from a Server Component or Route Handler — `headers()` throws
 * in client code paths.
 */
export async function getPlansSafeServer(): Promise<PlansResponse> {
  // Lazy-import to keep the client bundle clean. `next/headers` is server-only
  // and would break the build if leaked into a client component.
  const { headers } = await import("next/headers")
  try {
    const h = await headers()
    const host = h.get("host")
    if (!host) return EMPTY_PLANS_RESPONSE
    const proto = h.get("x-forwarded-proto") ?? (host.includes("localhost") ? "http" : "https")
    const url = `${proto}://${host}/api/plans`
    const res = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
      next: { revalidate: 60 },
    })
    if (!res.ok) return EMPTY_PLANS_RESPONSE
    return (await res.json()) as PlansResponse
  } catch {
    return EMPTY_PLANS_RESPONSE
  }
}
