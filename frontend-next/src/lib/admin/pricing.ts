// Admin Pricing API client
// Uses raw fetch with /api/admin/ prefix + credentials: 'include'
// (same pattern as admin/settings/page.tsx)

export interface PricingPayload {
  version: number
  catalog_frozen: boolean
  plans: Record<string, PlanConfig>
  trial: TrialConfig
  credits: CreditsConfig
  topup: TopupConfig
  cost_model: CostModelConfig
}

export interface PlanConfig {
  display_name: string
  free_quota_total: number | null
  max_duration_minutes: number
  max_concurrent_jobs: number
  allowed_service_modes: string[]
  self_serve: boolean
  price_cny_fen: { monthly: number; quarterly: number; annual: number } | null
  monthly_grant_credits: number | null
}

export interface TrialConfig {
  frozen: boolean
  days: number
  source_minutes: number
  includes_studio: boolean
  phone_required: boolean
  auto_charge: boolean
  fallback_plan: string
  grant_credits: number
}

export interface CreditsConfig {
  free_grant_credits: number
  debit_rates: Record<string, number>
  bucket_priority: Record<string, string[]>
  voice_clone_cost_credits: number
}

export interface TopupPackage {
  code: string
  credits: number
  price_cny_fen: number
  active: boolean
  sort_order: number
}

export interface TopupConfig {
  enabled: boolean
  packages: TopupPackage[]
}

export interface CostModelConfig {
  point_cost_rmb: number
  point_price_rmb: number
  target_gross_margin: number
  k_cn_chars_per_src_min: number
  fx_usd_cny: number
  translate_cost_rmb_per_src_min: number
  s2_review_cost_rmb_per_src_min: number
  rewrite_cost_rmb_per_src_min: number
  server_cost_rmb_per_src_min: number
}

export interface PricingVersion {
  version: number
  status: string
  payload: PricingPayload
  change_note: string | null
  created_at: string
  activated_at: string | null
}

async function adminFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`/api/admin/pricing${path}`, {
    credentials: 'include',
    ...init,
  })
  if (!resp.ok) {
    if (resp.status === 403) throw new ForbiddenError()
    const body = await resp.json().catch(() => ({}))
    const detail = (body as Record<string, unknown>)?.detail
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${resp.status}`)
  }
  return resp.json() as Promise<T>
}

export class ForbiddenError extends Error {
  constructor() {
    super('Forbidden')
    this.name = 'ForbiddenError'
  }
}

export async function getAdminPricing(): Promise<{
  active: PricingVersion
  draft: PricingVersion | null
}> {
  return adminFetch('')
}

export async function savePricingDraft(
  payload: PricingPayload,
): Promise<PricingVersion> {
  const data = await adminFetch<{ version: PricingVersion }>('/draft', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ payload }),
  })
  return data.version
}

export async function publishPricing(
  payload: PricingPayload,
  changeNote: string,
): Promise<PricingVersion> {
  const data = await adminFetch<{ version: PricingVersion }>('/publish', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ payload, change_note: changeNote }),
  })
  return data.version
}

export async function listPricingHistory(): Promise<PricingVersion[]> {
  const data = await adminFetch<{ versions: PricingVersion[] }>('/history')
  return data.versions
}
