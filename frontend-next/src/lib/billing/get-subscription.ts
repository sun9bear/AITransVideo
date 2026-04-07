/**
 * Fetch helper for `GET /api/me/subscription`.
 *
 * Returns the current authenticated user's paid subscription state plus
 * trial bookkeeping facts. The gateway is the source of truth — this file
 * only defines the client-side shape. Do NOT hardcode prices, periods, or
 * trial numbers here.
 */

export type SubscriptionStatus = "active" | "none"

export type ActiveSubscription = {
  id: string
  plan_code: string
  billing_period: string
  provider: string
  status: string
  started_at: string | null
  current_period_start: string | null
  current_period_end: string | null
  cancelled_at: string | null
}

export type TrialBookkeeping = {
  granted_at: string | null
  ends_at: string | null
}

export type MeSubscriptionResponse = {
  plan_code: string
  subscription_status: SubscriptionStatus
  subscription: ActiveSubscription | null
  trial: TrialBookkeeping
}

export async function getMySubscription(): Promise<MeSubscriptionResponse> {
  const res = await fetch("/api/me/subscription", {
    method: "GET",
    credentials: "include",
    headers: { Accept: "application/json" },
  })
  if (!res.ok) {
    throw new Error(
      `Failed to fetch subscription: ${res.status} ${res.statusText}`,
    )
  }
  return (await res.json()) as MeSubscriptionResponse
}
