export interface EntitlementsLimits {
  max_duration_minutes: number | null
  max_concurrent_jobs: number | null
  allowed_service_modes: ('express' | 'studio' | 'smart' | 'free')[]
  free_jobs_quota_total: number | null
  free_jobs_quota_used: number | null
  free_jobs_quota_remaining: number | null
}

export interface EntitlementsUI {
  show_admin_badge: boolean
  allow_upgrade: boolean
}

export interface UserEntitlements {
  role: string
  plan_code: string
  limits: EntitlementsLimits
  ui: EntitlementsUI
}

/**
 * Fetch user entitlements from Gateway `/api/me/entitlements`.
 * This endpoint lives on the Gateway (not Job API), so we use a direct fetch.
 */
export async function getEntitlements(): Promise<UserEntitlements> {
  const resp = await fetch('/api/me/entitlements', { credentials: 'include' })
  if (!resp.ok) {
    throw new Error(`Failed to fetch entitlements: ${resp.status}`)
  }
  return resp.json() as Promise<UserEntitlements>
}

/**
 * Phase 4.3a PR3 — Express auto-clone availability (admin flag + allowlist
 * gating, computed server-side by Gateway `/api/me/express-auto-clone-availability`).
 *
 * `available=true` is the ONLY signal the UI uses to render the Express
 * auto-clone consent checkbox. `reason` is for telemetry/debug only and is
 * never shown to the user (privacy + don't leak allowlist/canary state).
 */
export interface ExpressAutoCloneAvailability {
  available: boolean
  reason: string
}

/**
 * Fetch Express auto-clone availability. **Fail-closed**: any non-2xx /
 * network error resolves to `{available:false}` so the checkbox is simply
 * not rendered (never throws, never blocks the form). Mirrors the
 * endpoint's own fail-closed semantics (PR1-D).
 */
export async function getExpressAutoCloneAvailability(): Promise<ExpressAutoCloneAvailability> {
  try {
    const resp = await fetch('/api/me/express-auto-clone-availability', {
      credentials: 'include',
    })
    if (!resp.ok) {
      return { available: false, reason: 'fetch_failed' }
    }
    const data = (await resp.json()) as Partial<ExpressAutoCloneAvailability>
    return {
      available: data?.available === true,
      reason: typeof data?.reason === 'string' ? data.reason : 'unknown',
    }
  } catch {
    return { available: false, reason: 'fetch_failed' }
  }
}
