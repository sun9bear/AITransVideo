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
