/**
 * Billing / pricing types — read-only mirror of gateway `plan_catalog`.
 *
 * The gateway is the authoritative source of truth. This file only defines the
 * client-side shape used to consume `GET /api/plans`. Do NOT hardcode prices,
 * plan tiers, or trial numbers here — fetch them via `getPlans()` at runtime.
 *
 * Contract reference: docs/specs/2026-04-04-pricing-and-plans-api-contract.md
 */

export type PlanCode = "free" | "plus" | "pro"

export type BillingPeriod = "monthly" | "quarterly" | "annual"

/** CNY fen (分) prices keyed by billing period. `null` means unavailable. */
export type PlanPriceMap = {
  monthly: number | null
  quarterly: number | null
  annual: number | null
}

export type Plan = {
  code: string
  display_name: string
  max_duration_minutes: number
  max_concurrent_jobs: number
  allowed_service_modes: string[]
  self_serve: boolean
  /** `null` for free-tier plans that have no checkout path. */
  price_cny_fen: PlanPriceMap | null
  /** Present only on free-tier plans. */
  free_quota_total?: number
}

/**
 * Trial configuration.
 *
 * When `frozen` is true, the numeric fields are present and authoritative.
 * When `frozen` is false, they are absent and MUST NOT be assumed.
 */
export type TrialConfig = {
  frozen: boolean
  notes?: string
  days?: number
  source_minutes?: number
  includes_studio?: boolean
  phone_required?: boolean
  auto_charge?: boolean
  fallback_plan?: string
}

export type PlansResponse = {
  plans: Plan[]
  trial: TrialConfig
}

/**
 * Minimal empty-shape fallback used when the API call fails or is still loading.
 * Consumers should prefer showing a loading skeleton over rendering these values.
 */
export const EMPTY_PLANS_RESPONSE: PlansResponse = {
  plans: [],
  trial: { frozen: false },
}
