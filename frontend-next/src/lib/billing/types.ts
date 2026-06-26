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

/**
 * Integer prices keyed by billing period. `null` means unavailable.
 * `price_cny_fen` is CNY fen (分); `price_usd_cents` is USD cents (PayPal lane).
 */
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
  /**
   * PayPal-lane USD list price (USD cents), set independently of CNY in the
   * admin pricing page (plan 2026-06-26). `null`/absent when not published;
   * used only to show "≈ $X" when PayPal is the picked rail.
   */
  price_usd_cents?: PlanPriceMap | null
  /** Present only on free-tier plans. */
  free_quota_total?: number
  /**
   * Recurring monthly credit grant for this tier (Free / Plus / Pro).
   * Combined with `PlansResponse.credits_per_minute` to derive an
   * approximate "约 N 分钟 Express / N 分钟 Studio" display on the
   * pricing card. Optional because pre-2026-05-02 backend versions
   * don't include it.
   */
  monthly_grant_credits?: number
}

/**
 * System-wide credit-to-minute conversion rates, keyed by
 * `${service_mode}_${quality_tier}` (flat string keys for JSON-friendly
 * shape; the gateway flattens its internal tuple keys before sending).
 * The marketing pricing card reads `express_standard` and `studio_standard`
 * to compute the headline "约 N 分钟" display per paid tier.
 */
export type CreditsPerMinute = {
  express_standard?: number
  studio_standard?: number
  studio_high?: number
  studio_flagship?: number
  /** Forward-compat for new mode/tier combinations introduced post-V3. */
  [key: string]: number | undefined
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
  /**
   * Optional — present in 2026-05-02+ gateway. Older backends omit this
   * key, in which case the pricing card falls back to qualitative
   * "包含月度处理额度" wording instead of computed minute counts.
   */
  credits_per_minute?: CreditsPerMinute
}

/**
 * Minimal empty-shape fallback used when the API call fails or is still loading.
 * Consumers should prefer showing a loading skeleton over rendering these values.
 */
export const EMPTY_PLANS_RESPONSE: PlansResponse = {
  plans: [],
  trial: { frozen: false },
}
