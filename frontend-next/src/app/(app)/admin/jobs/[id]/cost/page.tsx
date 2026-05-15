"use client"

/**
 * Smart MVP P3-c admin cost page (decision log §2).
 *
 * Reads ``GET /api/admin/jobs/{id}/cost`` — Gateway-served verbatim
 * payload of ``audit/smart_cost_summary.json``. Admin-only by route
 * placement under ``/admin``; Gateway's ``_require_admin`` enforces
 * the auth gate server-side.
 *
 * User-facing workspace MUST NEVER show cost data. This page is the
 * single authoritative view (matches the existing ``/admin/disk``
 * pattern).
 */

import { useEffect, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import {
  ArrowLeft,
  Calculator,
  Clock,
  Coins,
  Loader2,
  AlertTriangle,
} from "lucide-react"
import { Button } from "@/components/ui/button"

// SmartCostSummary lives here (admin-only route) — keeping the type
// out of /types so the leak-guard
// ``test_no_cost_data_leak_in_workspace_frontend`` (decision log §2)
// enforces "cost fields don't appear in user-facing code".
interface SmartCostSummary {
  schema_version: 1
  job_id: string
  service_mode: "smart" | string
  minutes_processed: number
  pending_credits_charged: number | null
  credits_policy:
    | "capture_full"
    | "pending_settle"
    | "capture_partial"
    | "refund_full"
    | string
  cost_breakdown_internal_only: {
    asr_seconds: number
    llm_translation_chars: number
    tts_chars: number
    voice_clone_calls: number
    pending_minimax_quota_used_after: number | null
  }
  generated_at: string
}

type FetchState =
  | { kind: "loading" }
  | { kind: "ok"; cost: SmartCostSummary }
  | { kind: "not_found"; reason: string }
  | { kind: "error"; message: string }

export default function AdminJobCostPage() {
  const params = useParams()
  const router = useRouter()
  const jobId = ((params.id as string) ?? "").trim()
  const [state, setState] = useState<FetchState>({ kind: "loading" })

  useEffect(() => {
    if (!jobId) return
    let cancelled = false
    async function load() {
      try {
        const res = await fetch(`/api/admin/jobs/${jobId}/cost`, {
          credentials: "include",
        })
        if (cancelled) return
        if (res.ok) {
          const cost = (await res.json()) as SmartCostSummary
          setState({ kind: "ok", cost })
          return
        }
        if (res.status === 404) {
          let reason = "cost_summary_not_found"
          try {
            const body = await res.json()
            reason = body?.reason || body?.error || reason
          } catch {
            // ignore parse error
          }
          setState({ kind: "not_found", reason })
          return
        }
        setState({ kind: "error", message: `HTTP ${res.status}` })
      } catch (err) {
        if (cancelled) return
        const message = err instanceof Error ? err.message : "unknown error"
        setState({ kind: "error", message })
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [jobId])

  return (
    <div className="space-y-6 p-6">
      <header className="flex items-center gap-3">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => router.push("/admin/jobs")}
        >
          <ArrowLeft className="mr-1 h-4 w-4" />
          返回任务列表
        </Button>
        <div className="flex items-center gap-2">
          <Calculator className="h-6 w-6 text-primary" />
          <h1 className="text-2xl font-semibold">智能版成本明细</h1>
        </div>
      </header>

      <div className="text-sm text-muted-foreground">
        Job ID:{" "}
        <code className="rounded bg-muted px-2 py-0.5 font-mono">{jobId}</code>
      </div>

      {state.kind === "loading" ? (
        <div className="surface-card flex items-center gap-2 p-6 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          加载中…
        </div>
      ) : null}

      {state.kind === "not_found" ? (
        <div className="surface-card p-6">
          <div className="flex items-center gap-2 text-sm">
            <AlertTriangle className="h-5 w-5 text-amber-500" />
            <span className="font-medium">未找到成本汇总文件</span>
          </div>
          <p className="mt-2 text-sm text-muted-foreground">
            原因：<code className="rounded bg-muted px-2 py-0.5 font-mono">{state.reason}</code>
          </p>
          <p className="mt-2 text-sm text-muted-foreground">
            该 Job 可能是 PR#3C-P3-b 前创建的非智能版任务，或
            pipeline 尚未完成。
          </p>
        </div>
      ) : null}

      {state.kind === "error" ? (
        <div className="surface-card border border-red-200 p-6 dark:border-red-500/20">
          <div className="flex items-center gap-2 text-sm text-red-600">
            <AlertTriangle className="h-5 w-5" />
            <span className="font-medium">加载失败</span>
          </div>
          <p className="mt-2 text-sm text-red-600/80">{state.message}</p>
        </div>
      ) : null}

      {state.kind === "ok" ? <CostSummaryCard cost={state.cost} /> : null}
    </div>
  )
}

function CostSummaryCard({ cost }: { cost: SmartCostSummary }) {
  const cb = cost.cost_breakdown_internal_only
  return (
    <div className="space-y-4">
      {/* Top-line — settle-dependent fields prominently show "pending" */}
      <section className="surface-card p-6">
        <h2 className="mb-3 flex items-center gap-2 text-lg font-semibold">
          <Coins className="h-5 w-5 text-primary" />
          计费摘要
        </h2>
        <dl className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <KV
            label="处理时长"
            value={`${cost.minutes_processed.toFixed(3)} 分钟`}
            icon={<Clock className="h-4 w-4 text-muted-foreground" />}
          />
          <KV
            label="计费策略"
            value={creditsPolicyLabel(cost.credits_policy)}
          />
          <KV
            label="实际扣点"
            value={
              cost.pending_credits_charged === null
                ? "待结算"
                : `${cost.pending_credits_charged} 点`
            }
            tone={cost.pending_credits_charged === null ? "pending" : undefined}
          />
        </dl>
      </section>

      {/* Internal-only breakdown */}
      <section className="surface-card p-6">
        <h2 className="mb-3 text-lg font-semibold">内部成本明细</h2>
        <p className="mb-3 text-xs text-muted-foreground">
          以下字段为内部成本审计，仅 admin 可见，
          <span className="font-medium">绝不应展示给用户</span>。
        </p>
        <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <KV label="ASR 时长" value={`${cb.asr_seconds.toFixed(3)} 秒`} />
          <KV
            label="LLM 翻译字符数"
            value={`${cb.llm_translation_chars.toLocaleString()} 字符`}
          />
          <KV
            label="TTS 计费字符数"
            value={`${cb.tts_chars.toLocaleString()} 字符`}
          />
          <KV
            label="音色克隆调用"
            value={`${cb.voice_clone_calls} 次`}
          />
          <KV
            label="克隆后音色库余额"
            value={
              cb.pending_minimax_quota_used_after === null
                ? "待查询"
                : `${cb.pending_minimax_quota_used_after}`
            }
            tone={
              cb.pending_minimax_quota_used_after === null
                ? "pending"
                : undefined
            }
          />
        </dl>
      </section>

      <footer className="text-xs text-muted-foreground">
        生成于 {cost.generated_at} · schema_version={cost.schema_version}
      </footer>
    </div>
  )
}

function KV({
  label,
  value,
  icon,
  tone,
}: {
  label: string
  value: string
  icon?: React.ReactNode
  tone?: "pending"
}) {
  return (
    <div className="rounded-lg border border-border bg-muted/20 p-3">
      <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {icon}
        <span>{label}</span>
      </dt>
      <dd
        className={`mt-1 text-base font-semibold ${
          tone === "pending"
            ? "text-amber-600 dark:text-amber-400"
            : "text-foreground"
        }`}
      >
        {value}
      </dd>
    </div>
  )
}

function creditsPolicyLabel(policy: string): string {
  if (policy === "capture_full") return "正常计费 (capture_full)"
  if (policy === "capture_partial") return "部分计费 (capture_partial)"
  if (policy === "refund_full") return "全额退款 (refund_full)"
  if (policy === "pending_settle") return "待结算 (pending_settle)"
  return policy
}
