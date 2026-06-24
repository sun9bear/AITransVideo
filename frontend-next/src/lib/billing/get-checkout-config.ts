/**
 * Fetch helper for `GET /api/billing/checkout-config`.
 *
 * The gateway decides which payment providers are currently usable. The
 * frontend MUST NOT read `AVT_ALIPAY_*` env vars or hardcode provider
 * selection logic — always ask the gateway.
 *
 * Rule summary (mirrors gateway behavior):
 * - `providers` lists every known provider with its `operational` flag.
 * - `default_provider` is the first operational provider in the gateway's
 *   preference order (currently: alipay → wechatpay → paddle → stripe → fake).
 * - `fake` is the default safe path when no real provider is operational.
 * - Pricing facts are NEVER returned here; `/api/plans` stays the price truth.
 */

export type CheckoutProvider = {
  code: string
  display_name: string
  operational: boolean
}

export type CheckoutConfigResponse = {
  default_provider: string
  // Gateway recommendation for the initially-selected option. Current policy
  // prefers wechatpay on both desktop and mobile when operational.
  recommended_provider?: string
  checkout_surface?: string
  providers: CheckoutProvider[]
}

export async function getCheckoutConfig(): Promise<CheckoutConfigResponse> {
  const res = await fetch("/api/billing/checkout-config", {
    method: "GET",
    credentials: "include",
    headers: { Accept: "application/json" },
  })
  if (!res.ok) {
    throw new Error(
      `Failed to fetch checkout config: ${res.status} ${res.statusText}`,
    )
  }
  return (await res.json()) as CheckoutConfigResponse
}
