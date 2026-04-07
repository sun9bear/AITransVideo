"use client"

import { useEffect, useState } from "react"
import { getPlans } from "@/lib/billing/get-plans"
import type { PlansResponse } from "@/lib/billing/types"

type UsePlansState =
  | { status: "loading"; data: null; error: null }
  | { status: "ready"; data: PlansResponse; error: null }
  | { status: "error"; data: null; error: string }

/**
 * Client-side hook for the public `GET /api/plans` endpoint.
 *
 * Per T2 instruction §"默认实施决策": marketing pages consume plans via a
 * browser-side fetch in the first round. We do NOT refactor `get-plans.ts`
 * into a server-safe variant in this task. Server components that need plans
 * will be addressed separately.
 */
export function usePlans(): UsePlansState {
  const [state, setState] = useState<UsePlansState>({
    status: "loading",
    data: null,
    error: null,
  })

  useEffect(() => {
    let cancelled = false
    getPlans()
      .then((data) => {
        if (cancelled) return
        setState({ status: "ready", data, error: null })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const message = err instanceof Error ? err.message : "加载失败"
        setState({ status: "error", data: null, error: message })
      })
    return () => {
      cancelled = true
    }
  }, [])

  return state
}
