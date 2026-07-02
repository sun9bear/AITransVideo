/**
 * Fetch helpers for the topup credit-pack lane (CM-01).
 *
 * `GET /api/billing/topup/packages` — purchasable packages + per-SKU rails.
 * `POST /api/billing/topup/orders` — create a topup payment order.
 *
 * The gateway owns package availability (enabled gate, per-SKU provider
 * fail-closed filtering). The frontend hides the whole section when
 * `enabled` is false or the fetch fails — topup is default-inert.
 */

export type TopupPackage = {
  code: string
  credits: number
  price_cny_fen: number
  /** PayPal-lane USD list price (cents); null → PayPal hidden for this SKU. */
  price_usd_cents: number | null
  /** Rails that can actually charge this SKU right now (gateway-owned). */
  providers: string[]
}

export type TopupPackagesResponse = {
  enabled: boolean
  packages: TopupPackage[]
}

export async function getTopupPackages(): Promise<TopupPackagesResponse> {
  const res = await fetch("/api/billing/topup/packages", {
    credentials: "include",
    headers: { Accept: "application/json" },
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const detail =
      typeof data?.detail === "string" ? data.detail : `HTTP ${res.status}`
    throw new Error(detail)
  }
  return data as TopupPackagesResponse
}

export type CreateTopupOrderRequest = {
  topup_code: string
  provider: string
}

export type CreateTopupOrderResponse = {
  order_id: string
  status: string
  order_kind: "topup"
  topup_code: string
  credits: number
  amount_cny: number
  provider: string
  checkout_surface?: string
  checkout_url: string
  // "qrcode" => render qr_code_url in-page (WeChat Native weixin:// string);
  // "redirect" / absent => navigate to checkout_url (PayPal/fake).
  display_mode?: "redirect" | "qrcode"
  qr_code_url?: string | null
  expires_at: string | null
}

export async function createTopupOrder(
  body: CreateTopupOrderRequest,
): Promise<CreateTopupOrderResponse> {
  const res = await fetch("/api/billing/topup/orders", {
    method: "POST",
    credentials: "include",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const detail =
      typeof data?.detail === "string" ? data.detail : `HTTP ${res.status}`
    throw new Error(detail)
  }
  return data as CreateTopupOrderResponse
}
