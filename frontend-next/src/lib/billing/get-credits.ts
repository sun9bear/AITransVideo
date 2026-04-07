/**
 * Fetch helpers for V3-2 credits read surfaces.
 *
 * Consumes shadow credits data from Gateway. These are read-only views —
 * they do NOT represent the V2 billing/quota truth.
 */

export type CreditsBucket = {
  id: string
  type: string
  remaining: number
  reserved: number
  granted: number
  expires_at: string | null
  source_label: string | null
}

export type CreditsResponse = {
  total_available: number
  buckets: CreditsBucket[]
  in_trial: boolean
  trial_expires_at: string | null
}

export type LedgerEntry = {
  id: string
  direction: string
  credits_delta: number
  balance_after: number
  related_job_id: string | null
  reason_code: string
  created_at: string | null
}

export type CreditsLedgerResponse = {
  entries: LedgerEntry[]
  count: number
}

export type CreditsEstimateResponse = {
  estimated_credits: number
  minutes: number
  service_mode: string
  quality_tier: string
}

export async function getMyCredits(): Promise<CreditsResponse> {
  const res = await fetch("/api/me/credits", {
    method: "GET",
    credentials: "include",
    headers: { Accept: "application/json" },
  })
  if (!res.ok) {
    throw new Error(`Failed to fetch credits: ${res.status} ${res.statusText}`)
  }
  return (await res.json()) as CreditsResponse
}

export async function getMyCreditsLedger(
  limit = 20,
): Promise<CreditsLedgerResponse> {
  const res = await fetch(`/api/me/credits-ledger?limit=${limit}`, {
    method: "GET",
    credentials: "include",
    headers: { Accept: "application/json" },
  })
  if (!res.ok) {
    throw new Error(
      `Failed to fetch credits ledger: ${res.status} ${res.statusText}`,
    )
  }
  return (await res.json()) as CreditsLedgerResponse
}

export async function getCreditsEstimate(
  minutes: number,
  serviceMode: string = "express",
  qualityTier: string = "standard",
): Promise<CreditsEstimateResponse> {
  const params = new URLSearchParams({
    minutes: String(minutes),
    service_mode: serviceMode,
    quality_tier: qualityTier,
  })
  const res = await fetch(`/api/credits/estimate?${params}`, {
    method: "GET",
    headers: { Accept: "application/json" },
  })
  if (!res.ok) {
    throw new Error(
      `Failed to fetch credits estimate: ${res.status} ${res.statusText}`,
    )
  }
  return (await res.json()) as CreditsEstimateResponse
}
