/**
 * Fetch helper for `POST /api/billing/orders`.
 *
 * The gateway owns order creation. The frontend should gate invalid CTAs in
 * the UI (disable / hide downgrade / same-plan buttons), but the real
 * validation boundary is server-side inside `create_order`. This helper
 * forwards the request body and surfaces the order shape back to the caller.
 */

export type CreateOrderRequest = {
  target_plan_code: string
  billing_period: string
  provider: string
}

export type CreateOrderResponse = {
  order_id: string
  status: string
  amount_cny: number
  target_plan_code: string
  billing_period: string
  provider: string
  checkout_surface?: string
  checkout_url: string
  expires_at: string | null
}

export async function createOrder(
  body: CreateOrderRequest,
): Promise<CreateOrderResponse> {
  const res = await fetch("/api/billing/orders", {
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
      typeof data?.detail === "string" ? data.detail : `创建订单失败 (${res.status})`
    throw new Error(detail)
  }
  return data as CreateOrderResponse
}
