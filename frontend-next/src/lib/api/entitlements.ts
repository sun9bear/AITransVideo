export interface EntitlementsLimits {
  max_duration_minutes: number | null
  max_concurrent_jobs: number | null
  allowed_service_modes: ('express' | 'studio')[]
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
