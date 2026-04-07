/**
 * Fetch helper for `GET /api/billing/history`.
 *
 * Returns the current authenticated user's billing invoice history, newest
 * first. The gateway strictly scopes the query to `user.id` — there is no
 * tenant-wide view from this path, and no admin variant.
 *
 * Task 6 scope: baseline Billing UI only. We do not add pagination, filters,
 * export, or refund/cancel actions here. Those belong to later tasks.
 */

export type BillingInvoiceStatus = "paid" | "failed" | "refunded" | string

export type BillingInvoice = {
  id: string
  subscription_id: string | null
  payment_order_id: string
  provider: string
  provider_order_id: string | null
  plan_code: string
  billing_period: string
  amount_cny: number
  currency: string
  status: BillingInvoiceStatus
  issued_at: string | null
  paid_at: string | null
  created_at: string | null
}

export type BillingHistoryResponse = {
  invoices: BillingInvoice[]
}

export async function getBillingHistory(): Promise<BillingHistoryResponse> {
  const res = await fetch("/api/billing/history", {
    method: "GET",
    credentials: "include",
    headers: { Accept: "application/json" },
  })
  if (!res.ok) {
    throw new Error(
      `Failed to fetch billing history: ${res.status} ${res.statusText}`,
    )
  }
  return (await res.json()) as BillingHistoryResponse
}
