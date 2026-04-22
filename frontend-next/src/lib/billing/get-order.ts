export type PaymentOrderStatus =
  | "created"
  | "pending"
  | "paid"
  | "failed"
  | "cancelled"
  | "expired"
  | "refunded"

export type PaymentOrderResponse = {
  order_id: string
  status: PaymentOrderStatus
  amount_cny: number
  target_plan_code: string
  billing_period: string
  provider: string
  provider_order_id: string | null
  paid_at: string | null
  created_at: string | null
}

export async function getOrder(
  orderId: string,
  options?: { refresh?: boolean },
): Promise<PaymentOrderResponse> {
  const query = options?.refresh ? "?refresh=1" : ""
  const res = await fetch(`/api/billing/orders/${encodeURIComponent(orderId)}${query}`, {
    method: "GET",
    credentials: "include",
    headers: { Accept: "application/json" },
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const detail =
      typeof data?.detail === "string" ? data.detail : `Failed to fetch order (${res.status})`
    throw new Error(detail)
  }
  return data as PaymentOrderResponse
}
