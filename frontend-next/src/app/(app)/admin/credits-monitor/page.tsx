"use client"

import { useEffect, useState, useCallback } from "react"
import { Info, TrendingUp } from "lucide-react"

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface KActual {
  avg: number | null
  p50: number | null
  p75: number | null
  p90: number | null
}

interface CreditsActualSource {
  source_counts: {
    snapshot: number
    ledger_derived: number
    missing: number
  }
  snapshot_sum: number
  ledger_derived_sum: number
  effective_sum: number
  ledger_capture_jobs: number
  methodology: string
}

interface CostMetrics {
  window_days: number
  jobs_total: number
  credits_estimated_sum: number | null
  credits_actual_sum: number | null
  credits_actual_effective_sum?: number | null
  credits_actual_source?: CreditsActualSource
  estimate_actual_delta_pct: number | null
  estimate_effective_delta_pct?: number | null
  k_actual: KActual
  rewrite_rate_pct: number | null
  rewrite_count_avg: number | null
  service_mode_dist: Record<string, number>
  tts_billed_chars_coverage_pct: number | null
  jobs_unsettled: number
}

interface ProviderRow {
  provider: string
  model: string
  job_count: number
  total_minutes: number | null
  total_billed_chars: number | null
  avg_billed_per_min: number | null
  avg_credits_per_min: number | null
}

interface ProviderBreakdown {
  window_days: number
  providers: ProviderRow[]
}

interface EstimateActualOutlier {
  job_id: string
  title: string | null
  service_mode: string | null
  credits_estimated: number | null
  credits_actual: number | null
  delta: number | null
  actual_minutes: number | null
}

interface RewriteTopRow {
  job_id: string
  title: string | null
  rewrite_count: number | null
  actual_minutes: number | null
}

interface MissingFieldsJob {
  job_id: string
  missing: string[]
}

interface Outliers {
  window_days: number
  estimate_actual_outliers: EstimateActualOutlier[]
  rewrite_top: RewriteTopRow[]
  unsettled_jobs: string[]
  missing_fields_jobs: MissingFieldsJob[]
}

// Summary types — must match backend GET /api/admin/credits/summary exactly
interface BucketRow {
  bucket_type: string
  count: number
  total_granted: number
  total_remaining: number
  total_reserved: number
}

interface RecentEntry {
  direction: string
  credits_delta: number
  balance_after: number
  related_job_id: string | null
  reason_code: string | null
  created_at: string | null
}

interface ClosenessInfo {
  jobs_with_reserve: number
  jobs_with_settle: number
  jobs_unsettled: number
  unsettled_job_ids_sample: string[]
  note: string
  methodology: string
}

interface FieldStatusValue {
  status: string
  source: string
  coverage?: Record<string, string>
}

interface Summary {
  buckets: BucketRow[]
  ledger: {
    by_direction: Record<string, number>
    total_entries: number
    recent: RecentEntry[]
  }
  metering: Record<string, number>
  credits_actual_source?: CreditsActualSource
  reserve_capture_closeness: ClosenessInfo
  field_status: Record<string, FieldStatusValue>
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

class ForbiddenError extends Error {
  constructor() {
    super("Forbidden")
    this.name = "ForbiddenError"
  }
}

async function adminFetch<T>(path: string): Promise<T> {
  const resp = await fetch(`/api/admin/credits${path}`, {
    credentials: "include",
  })
  if (!resp.ok) {
    if (resp.status === 403) throw new ForbiddenError()
    const body = await resp.json().catch(() => ({}))
    const detail = (body as Record<string, unknown>)?.detail
    throw new Error(typeof detail === "string" ? detail : `HTTP ${resp.status}`)
  }
  return resp.json() as Promise<T>
}

function fmt(n: number | null | undefined): string {
  if (n == null) return "-"
  return n.toLocaleString("zh-CN")
}

function fmtPct(n: number | null | undefined): string {
  if (n == null) return "-"
  return `${n.toFixed(1)}%`
}

function fmtFloat(n: number | null | undefined, decimals = 1): string {
  if (n == null) return "-"
  return n.toFixed(decimals)
}

function shortId(id: string): string {
  return id.slice(0, 8)
}

function fmtDate(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-base font-semibold text-foreground mb-3">{children}</h2>
  )
}

function Card({
  label,
  value,
  sub,
  highlight,
}: {
  label: string
  value: string | React.ReactNode
  sub?: string
  highlight?: "warn" | "ok" | "info"
}) {
  const highlightClass =
    highlight === "warn"
      ? "border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/5"
      : highlight === "ok"
        ? "border-[color:var(--bamboo)]/30 bg-[color:var(--bamboo)]/5"
        : "border-border bg-card"

  return (
    <div className={`rounded-xl border p-4 flex flex-col gap-1 ${highlightClass}`}>
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-2xl font-bold text-foreground">{value}</span>
      {sub && <span className="text-xs text-muted-foreground">{sub}</span>}
    </div>
  )
}

function TableWrapper({ children }: { children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm">{children}</table>
    </div>
  )
}

function Th({ children, right }: { children: React.ReactNode; right?: boolean }) {
  return (
    <th
      className={`px-3 py-2 text-xs font-medium text-muted-foreground bg-muted/30 border-b border-border whitespace-nowrap ${right ? "text-right" : "text-left"}`}
    >
      {children}
    </th>
  )
}

function Td({ children, right, mono }: { children: React.ReactNode; right?: boolean; mono?: boolean }) {
  return (
    <td
      className={`px-3 py-2 border-b border-border/50 text-foreground/90 ${right ? "text-right tabular-nums" : ""} ${mono ? "font-mono text-xs" : ""}`}
    >
      {children}
    </td>
  )
}

function ErrorBox({ msg }: { msg: string }) {
  return (
    <div className="rounded-lg border border-[color:var(--cinnabar)]/20 bg-[color:var(--cinnabar)]/5 px-4 py-3 text-sm text-[color:var(--cinnabar)]">
      加载失败：{msg}
    </div>
  )
}

function EmptyBox({ text }: { text: string }) {
  return (
    <div className="rounded-lg border border-border bg-muted/10 px-4 py-3 text-sm text-muted-foreground text-center">
      {text}
    </div>
  )
}

function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center py-8">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function CreditsMonitorPage() {
  const [windowDays, setWindowDays] = useState<"7" | "30">("7")

  // ── summary (no window param)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [summaryLoading, setSummaryLoading] = useState(true)
  const [summaryError, setSummaryError] = useState<string | null>(null)

  // ── cost metrics
  const [metrics, setMetrics] = useState<CostMetrics | null>(null)
  const [metricsLoading, setMetricsLoading] = useState(true)
  const [metricsError, setMetricsError] = useState<string | null>(null)

  // ── provider breakdown
  const [breakdown, setBreakdown] = useState<ProviderBreakdown | null>(null)
  const [breakdownLoading, setBreakdownLoading] = useState(true)
  const [breakdownError, setBreakdownError] = useState<string | null>(null)

  // ── outliers
  const [outliers, setOutliers] = useState<Outliers | null>(null)
  const [outliersLoading, setOutliersLoading] = useState(true)
  const [outliersError, setOutliersError] = useState<string | null>(null)

  // ── 403
  const [forbidden, setForbidden] = useState(false)

  // Load summary once (no window param)
  const loadSummary = useCallback(async () => {
    setSummaryLoading(true)
    setSummaryError(null)
    try {
      const data = await adminFetch<Summary>("/summary")
      setSummary(data)
    } catch (err) {
      if (err instanceof ForbiddenError) {
        setForbidden(true)
        return
      }
      setSummaryError(err instanceof Error ? err.message : "未知错误")
    } finally {
      setSummaryLoading(false)
    }
  }, [])

  // Load window-dependent APIs
  const loadWindowData = useCallback(
    async (w: string) => {
      setMetricsLoading(true)
      setBreakdownLoading(true)
      setOutliersLoading(true)
      setMetricsError(null)
      setBreakdownError(null)
      setOutliersError(null)

      const [metricsRes, breakdownRes, outliersRes] = await Promise.allSettled([
        adminFetch<CostMetrics>(`/cost-metrics?window=${w}`),
        adminFetch<ProviderBreakdown>(`/provider-breakdown?window=${w}`),
        adminFetch<Outliers>(`/outliers?window=${w}`),
      ])

      if (metricsRes.status === "fulfilled") {
        setMetrics(metricsRes.value)
      } else {
        const err = metricsRes.reason
        if (err instanceof ForbiddenError) {
          setForbidden(true)
          return
        }
        setMetricsError(err instanceof Error ? err.message : "未知错误")
      }
      setMetricsLoading(false)

      if (breakdownRes.status === "fulfilled") {
        setBreakdown(breakdownRes.value)
      } else {
        const err = breakdownRes.reason
        if (!(err instanceof ForbiddenError)) {
          setBreakdownError(err instanceof Error ? err.message : "未知错误")
        }
      }
      setBreakdownLoading(false)

      if (outliersRes.status === "fulfilled") {
        setOutliers(outliersRes.value)
      } else {
        const err = outliersRes.reason
        if (!(err instanceof ForbiddenError)) {
          setOutliersError(err instanceof Error ? err.message : "未知错误")
        }
      }
      setOutliersLoading(false)
    },
    [],
  )

  useEffect(() => {
    void loadSummary()
  }, [loadSummary])

  useEffect(() => {
    void loadWindowData(windowDays)
  }, [windowDays, loadWindowData])

  // ── 403 gate
  if (forbidden) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3">
        <div className="rounded-2xl border border-[color:var(--cinnabar)]/20 bg-[color:var(--cinnabar)]/5 p-6 text-center">
          <p className="text-lg font-semibold text-[color:var(--cinnabar)]">仅管理员可访问</p>
          <p className="text-sm text-muted-foreground mt-2">
            你没有查看此页面的权限
          </p>
        </div>
      </div>
    )
  }

  // ── service mode totals for % display
  const totalJobs = metrics ? Object.values(metrics.service_mode_dist).reduce((a, b) => a + b, 0) : 0

  // ── K value deviation note
  const kAvg = metrics?.k_actual?.avg
  const kDevNote =
    kAvg != null
      ? kAvg > 250
        ? `↑ 高于冻结假设 ${((kAvg - 250) / 250 * 100).toFixed(0)}%`
        : kAvg < 250
          ? `↓ 低于冻结假设 ${((250 - kAvg) / 250 * 100).toFixed(0)}%`
          : "与冻结假设持平"
      : null

  return (
    <div className="max-w-6xl mx-auto px-4 py-6 space-y-8">
      {/* ── Header ── */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-2">
          <TrendingUp className="h-5 w-5 text-primary" />
          <h1 className="text-xl font-bold text-foreground">Shadow Credits 校准</h1>
        </div>

        {/* Window selector */}
        <div className="flex items-center gap-1 rounded-lg border border-border bg-muted/20 p-1">
          {(["7", "30"] as const).map((w) => (
            <button
              key={w}
              onClick={() => setWindowDays(w)}
              className={`px-3 py-1 rounded text-sm font-medium transition-colors ${
                windowDays === w
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {w === "7" ? "近 7 天" : "近 30 天"}
            </button>
          ))}
        </div>
      </div>

      {/* ── Scope banner ── */}
      <div className="flex items-start gap-2 rounded-lg px-4 py-3 text-sm border border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/8 text-[color:var(--ochre)]">
        <Info className="h-4 w-4 mt-0.5 shrink-0" />
        <span>
          仅覆盖内部 shadow metering / credits 数据，不含外部 TTS 账单、LLM token、云资源成本。
          适合校准冻结参数（K 值、rewrite 率），不适合直接作为完整单位成本核算依据。
        </span>
      </div>

      {/* ── Overview cards ── */}
      <section>
        <SectionTitle>总览（近 {windowDays} 天）</SectionTitle>
        {metricsLoading ? (
          <LoadingSpinner />
        ) : metricsError ? (
          <ErrorBox msg={metricsError} />
        ) : metrics ? (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <Card label="任务数" value={fmt(metrics.jobs_total)} />
            <Card
              label="预估点数"
              value={fmt(metrics.credits_estimated_sum)}
            />
            <Card
              label="实扣点数"
              value={fmt(metrics.credits_actual_sum)}
            />
            <Card
              label="实扣点数(有效)"
              value={fmt(metrics.credits_actual_effective_sum ?? metrics.credits_actual_sum)}
              sub={
                metrics.credits_actual_source
                  ? `snapshot ${metrics.credits_actual_source.source_counts.snapshot} / ledger ${metrics.credits_actual_source.source_counts.ledger_derived} / missing ${metrics.credits_actual_source.source_counts.missing}`
                  : undefined
              }
              highlight={
                metrics.credits_actual_source?.source_counts.missing
                  ? "warn"
                  : undefined
              }
            />
            <Card
              label="预估/实扣偏差率"
              value={fmtPct(metrics.estimate_effective_delta_pct ?? metrics.estimate_actual_delta_pct)}
              highlight={
                (metrics.estimate_effective_delta_pct ?? metrics.estimate_actual_delta_pct) != null &&
                Math.abs(metrics.estimate_effective_delta_pct ?? metrics.estimate_actual_delta_pct ?? 0) > 20
                  ? "warn"
                  : undefined
              }
            />
            <Card
              label="Rewrite 触发率"
              value={fmtPct(metrics.rewrite_rate_pct)}
              sub="冻结假设: 30%"
              highlight={
                metrics.rewrite_rate_pct != null &&
                metrics.rewrite_rate_pct > 50
                  ? "warn"
                  : undefined
              }
            />
            <Card
              label="K_actual 均值"
              value={metrics.k_actual?.avg != null ? fmt(Math.round(metrics.k_actual.avg)) : "-"}
              sub="冻结假设: K=250"
            />
            <Card
              label="TTS 计费字符覆盖率"
              value={fmtPct(metrics.tts_billed_chars_coverage_pct)}
              highlight={
                metrics.tts_billed_chars_coverage_pct != null &&
                metrics.tts_billed_chars_coverage_pct < 80
                  ? "warn"
                  : undefined
              }
            />
            <Card
              label="未闭环任务"
              value={fmt(metrics.jobs_unsettled)}
              sub="可能正在运行中"
              highlight={metrics.jobs_unsettled > 0 ? "warn" : "ok"}
            />
          </div>
        ) : (
          <EmptyBox text="暂无数据" />
        )}
      </section>

      {/* ── Cost calibration ── */}
      <section>
        <SectionTitle>成本校准</SectionTitle>

        <div className="space-y-5">
          {/* K value distribution */}
          <div>
            <p className="text-sm font-medium text-foreground/80 mb-2">K 值分布（字符/分钟）</p>
            {metricsLoading ? (
              <LoadingSpinner />
            ) : metricsError ? (
              <ErrorBox msg={metricsError} />
            ) : metrics?.k_actual ? (
              <div className="rounded-lg border border-border bg-card p-4">
                <div className="flex flex-wrap gap-6 text-sm">
                  {(
                    [
                      ["avg", "均值"],
                      ["p50", "P50"],
                      ["p75", "P75"],
                      ["p90", "P90"],
                    ] as const
                  ).map(([key, label]) => (
                    <div key={key} className="flex flex-col gap-0.5">
                      <span className="text-xs text-muted-foreground">{label}</span>
                      <span className="text-lg font-semibold tabular-nums">
                        {metrics.k_actual[key] != null
                          ? fmt(Math.round(metrics.k_actual[key]!))
                          : "-"}
                      </span>
                    </div>
                  ))}
                </div>
                <p className="mt-3 text-xs text-muted-foreground">
                  冻结假设: K=250
                  {kDevNote && (
                    <span
                      className={`ml-2 font-medium ${kAvg != null && kAvg > 250 ? "text-[color:var(--ochre)]" : "text-[color:var(--bamboo)]"}`}
                    >
                      {kDevNote}
                    </span>
                  )}
                </p>
              </div>
            ) : (
              <EmptyBox text="暂无 K 值数据" />
            )}
          </div>

          {/* Provider breakdown */}
          <div>
            <p className="text-sm font-medium text-foreground/80 mb-1">
              Provider 分布
              <span className="text-xs text-muted-foreground ml-2 font-normal">
                （按 job 默认引擎，非实际执行 provider）
              </span>
            </p>
            {breakdownLoading ? (
              <LoadingSpinner />
            ) : breakdownError ? (
              <ErrorBox msg={breakdownError} />
            ) : breakdown && breakdown.providers.length > 0 ? (
              <TableWrapper>
                <thead>
                  <tr>
                    <Th>Provider</Th>
                    <Th>Model</Th>
                    <Th right>Jobs</Th>
                    <Th right>分钟数</Th>
                    <Th right>计费字符</Th>
                    <Th right>字符/分钟</Th>
                    <Th right>点数/分钟</Th>
                  </tr>
                </thead>
                <tbody>
                  {breakdown.providers.map((row, i) => (
                    <tr key={i} className="hover:bg-muted/10 transition-colors">
                      <Td>{row.provider}</Td>
                      <Td mono>{row.model}</Td>
                      <Td right>{fmt(row.job_count)}</Td>
                      <Td right>{fmtFloat(row.total_minutes)}</Td>
                      <Td right>{fmt(row.total_billed_chars)}</Td>
                      <Td right>{row.avg_billed_per_min != null ? fmt(Math.round(row.avg_billed_per_min)) : "-"}</Td>
                      <Td right>{fmtFloat(row.avg_credits_per_min)}</Td>
                    </tr>
                  ))}
                </tbody>
              </TableWrapper>
            ) : (
              <EmptyBox text="暂无 Provider 数据" />
            )}
          </div>

          {/* Service mode + rewrite */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {/* Mode distribution */}
            <div>
              <p className="text-sm font-medium text-foreground/80 mb-2">模式分布</p>
              {metricsLoading ? (
                <LoadingSpinner />
              ) : metrics ? (
                <div className="rounded-lg border border-border bg-card p-4 text-sm space-y-2">
                  {totalJobs === 0 ? (
                    <span className="text-muted-foreground">暂无数据</span>
                  ) : (
                    Object.entries(metrics.service_mode_dist).map(([mode, count]) => (
                      <div key={mode} className="flex items-center justify-between">
                        <span className="capitalize text-foreground/80">{mode}</span>
                        <span className="tabular-nums font-medium">
                          {fmt(count)}{" "}
                          <span className="text-muted-foreground text-xs">
                            ({((count / totalJobs) * 100).toFixed(0)}%)
                          </span>
                        </span>
                      </div>
                    ))
                  )}
                </div>
              ) : (
                <EmptyBox text="暂无数据" />
              )}
            </div>

            {/* Rewrite pressure */}
            <div>
              <p className="text-sm font-medium text-foreground/80 mb-2">Rewrite 压力</p>
              {metricsLoading ? (
                <LoadingSpinner />
              ) : metrics ? (
                <div className="rounded-lg border border-border bg-card p-4 text-sm space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-foreground/80">触发率</span>
                    <span className="tabular-nums font-medium">
                      {fmtPct(metrics.rewrite_rate_pct)}
                      <span className="text-muted-foreground text-xs ml-2">冻结假设 30%</span>
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-foreground/80">平均次数</span>
                    <span className="tabular-nums font-medium">
                      {fmtFloat(metrics.rewrite_count_avg)} 次
                    </span>
                  </div>
                </div>
              ) : (
                <EmptyBox text="暂无数据" />
              )}
            </div>
          </div>
        </div>
      </section>

      {/* ── Ledger health ── */}
      <section>
        <SectionTitle>账本健康</SectionTitle>
        {summaryLoading ? (
          <LoadingSpinner />
        ) : summaryError ? (
          <ErrorBox msg={summaryError} />
        ) : summary ? (
          <div className="space-y-5">
            {/* Bucket summary */}
            <div>
              <p className="text-sm font-medium text-foreground/80 mb-2">Bucket 汇总</p>
              {summary.buckets.length === 0 ? (
                <EmptyBox text="暂无 Bucket 数据" />
              ) : (
                <TableWrapper>
                  <thead>
                    <tr>
                      <Th>类型</Th>
                      <Th right>数量</Th>
                      <Th right>已授点数</Th>
                      <Th right>余额</Th>
                      <Th right>预扣</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.buckets.map((row, i) => (
                      <tr key={i} className="hover:bg-muted/10 transition-colors">
                        <Td>{row.bucket_type}</Td>
                        <Td right>{fmt(row.count)}</Td>
                        <Td right>{fmt(row.total_granted)}</Td>
                        <Td right>{fmt(row.total_remaining)}</Td>
                        <Td right>{fmt(row.total_reserved)}</Td>
                      </tr>
                    ))}
                  </tbody>
                </TableWrapper>
              )}
            </div>

            {/* Ledger distribution */}
            <div>
              <p className="text-sm font-medium text-foreground/80 mb-2">
                Ledger 分布
                <span className="text-xs text-muted-foreground ml-2 font-normal">
                  共 {fmt(summary.ledger.total_entries)} 条
                </span>
              </p>
              {Object.keys(summary.ledger.by_direction).length === 0 ? (
                <EmptyBox text="暂无 Ledger 数据" />
              ) : (
                <div className="flex flex-wrap gap-3">
                  {Object.entries(summary.ledger.by_direction).map(([direction, count]) => (
                    <div
                      key={direction}
                      className="rounded-lg border border-border bg-card px-4 py-2 text-sm"
                    >
                      <span className="text-muted-foreground">{direction}</span>
                      <span className="ml-2 font-semibold tabular-nums">{fmt(count)} 条</span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Closeness */}
            <div>
              <p className="text-sm font-medium text-foreground/80 mb-2">闭环状态</p>
              <div
                className={`rounded-lg border px-4 py-3 text-sm ${
                  summary.reserve_capture_closeness.jobs_unsettled > 0
                    ? "border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/5 text-[color:var(--ochre)]"
                    : "border-[color:var(--bamboo)]/30 bg-[color:var(--bamboo)]/5 text-[color:var(--bamboo)]"
                }`}
              >
                {summary.reserve_capture_closeness.jobs_unsettled === 0 ? (
                  <span>全部已闭环（{fmt(summary.reserve_capture_closeness.jobs_with_reserve)} 个 reserve 均已 settle）</span>
                ) : (
                  <span>
                    {summary.reserve_capture_closeness.jobs_unsettled} 个未闭环 job（可能正在运行中）：
                    {summary.reserve_capture_closeness.unsettled_job_ids_sample.slice(0, 5).map((id) => (
                      <span
                        key={id}
                        title={id}
                        className="inline-block ml-2 font-mono text-xs bg-black/20 rounded px-1"
                      >
                        {shortId(id)}
                      </span>
                    ))}
                    {summary.reserve_capture_closeness.unsettled_job_ids_sample.length > 5 && (
                      <span className="ml-1 text-xs">
                        …共 {summary.reserve_capture_closeness.unsettled_job_ids_sample.length} 个
                      </span>
                    )}
                  </span>
                )}
              </div>
            </div>

            {/* Field status */}
            <div>
              <p className="text-sm font-medium text-foreground/80 mb-2">Field Status</p>
              {Object.keys(summary.field_status).length === 0 ? (
                <EmptyBox text="暂无字段状态" />
              ) : (
                <div className="flex flex-wrap gap-2">
                  {Object.entries(summary.field_status).map(([field, info]) => (
                    <div
                      key={field}
                      title={info.source}
                      className={`rounded border px-2 py-1 text-xs font-mono ${
                        info.status === "LIVE"
                          ? "border-[color:var(--bamboo)]/30 bg-[color:var(--bamboo)]/5 text-[color:var(--bamboo)]"
                          : info.status === "LIVE_PARTIAL"
                            ? "border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/5 text-[color:var(--ochre)]"
                            : "border-border text-muted-foreground"
                      }`}
                    >
                      {field.replace("metering_snapshot.", "")}
                      <span className="ml-1 opacity-70">{info.status}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Recent ledger */}
            <div>
              <p className="text-sm font-medium text-foreground/80 mb-2">最近 Ledger 流水</p>
              {summary.ledger.recent.length === 0 ? (
                <EmptyBox text="暂无流水记录" />
              ) : (
                <TableWrapper>
                  <thead>
                    <tr>
                      <Th>方向</Th>
                      <Th right>金额</Th>
                      <Th right>余额后</Th>
                      <Th>关联 Job</Th>
                      <Th>原因码</Th>
                      <Th>时间</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.ledger.recent.map((row, i) => (
                      <tr key={i} className="hover:bg-muted/10 transition-colors">
                        <Td>
                          <span
                            className={`text-xs font-medium ${
                              row.direction === "grant"
                                ? "text-[color:var(--bamboo)]"
                                : row.direction === "capture"
                                  ? "text-[color:var(--cinnabar)]"
                                  : row.direction === "reserve"
                                    ? "text-[color:var(--ochre)]"
                                    : "text-muted-foreground"
                            }`}
                          >
                            {row.direction}
                          </span>
                        </Td>
                        <Td right>
                          <span className={row.credits_delta < 0 ? "text-[color:var(--cinnabar)]" : "text-[color:var(--bamboo)]"}>
                            {row.credits_delta > 0 ? "+" : ""}
                            {fmt(row.credits_delta)}
                          </span>
                        </Td>
                        <Td right>{fmt(row.balance_after)}</Td>
                        <Td mono>
                          {row.related_job_id ? (
                            <span title={row.related_job_id}>{shortId(row.related_job_id)}</span>
                          ) : (
                            "-"
                          )}
                        </Td>
                        <Td mono>{row.reason_code ?? "-"}</Td>
                        <Td>{row.created_at ? fmtDate(row.created_at) : "-"}</Td>
                      </tr>
                    ))}
                  </tbody>
                </TableWrapper>
              )}
            </div>
          </div>
        ) : (
          <EmptyBox text="暂无账本数据" />
        )}
      </section>

      {/* ── Outliers ── */}
      <section>
        <SectionTitle>异常作业（近 {windowDays} 天）</SectionTitle>
        {outliersLoading ? (
          <LoadingSpinner />
        ) : outliersError ? (
          <ErrorBox msg={outliersError} />
        ) : outliers ? (
          <div className="space-y-5">
            {/* Estimate vs actual outliers */}
            <div>
              <p className="text-sm font-medium text-foreground/80 mb-2">预估/实扣偏差最大 Top 10</p>
              {outliers.estimate_actual_outliers.length === 0 ? (
                <EmptyBox text="暂无异常记录" />
              ) : (
                <TableWrapper>
                  <thead>
                    <tr>
                      <Th>Job ID</Th>
                      <Th>标题</Th>
                      <Th>模式</Th>
                      <Th right>预估</Th>
                      <Th right>实扣</Th>
                      <Th right>偏差</Th>
                      <Th right>分钟数</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {outliers.estimate_actual_outliers.map((row) => (
                      <tr key={row.job_id} className="hover:bg-muted/10 transition-colors">
                        <Td mono>
                          <span title={row.job_id}>{shortId(row.job_id)}</span>
                        </Td>
                        <Td>
                          <span
                            className="block max-w-[180px] truncate"
                            title={row.title ?? undefined}
                          >
                            {row.title ?? "-"}
                          </span>
                        </Td>
                        <Td>{row.service_mode ?? "-"}</Td>
                        <Td right>{fmt(row.credits_estimated)}</Td>
                        <Td right>{fmt(row.credits_actual)}</Td>
                        <Td right>
                          <span
                            className={
                              row.delta != null && row.delta > 0
                                ? "text-[color:var(--bamboo)]"
                                : "text-[color:var(--cinnabar)]"
                            }
                          >
                            {row.delta != null
                              ? (row.delta > 0 ? "+" : "") + fmt(row.delta)
                              : "-"}
                          </span>
                        </Td>
                        <Td right>{fmtFloat(row.actual_minutes)}</Td>
                      </tr>
                    ))}
                  </tbody>
                </TableWrapper>
              )}
            </div>

            {/* Rewrite top */}
            <div>
              <p className="text-sm font-medium text-foreground/80 mb-2">Rewrite 次数最多 Top 10</p>
              {outliers.rewrite_top.length === 0 ? (
                <EmptyBox text="暂无数据" />
              ) : (
                <TableWrapper>
                  <thead>
                    <tr>
                      <Th>Job ID</Th>
                      <Th>标题</Th>
                      <Th right>Rewrite 次数</Th>
                      <Th right>分钟数</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {outliers.rewrite_top.map((row) => (
                      <tr key={row.job_id} className="hover:bg-muted/10 transition-colors">
                        <Td mono>
                          <span title={row.job_id}>{shortId(row.job_id)}</span>
                        </Td>
                        <Td>
                          <span
                            className="block max-w-[240px] truncate"
                            title={row.title ?? undefined}
                          >
                            {row.title ?? "-"}
                          </span>
                        </Td>
                        <Td right>
                          <span
                            className={
                              row.rewrite_count != null && row.rewrite_count >= 5
                                ? "text-[color:var(--ochre)] font-semibold"
                                : ""
                            }
                          >
                            {fmt(row.rewrite_count)}
                          </span>
                        </Td>
                        <Td right>{fmtFloat(row.actual_minutes)}</Td>
                      </tr>
                    ))}
                  </tbody>
                </TableWrapper>
              )}
            </div>

            {/* Unsettled jobs */}
            <div>
              <p className="text-sm font-medium text-foreground/80 mb-2">
                未闭环 Jobs
                <span className="ml-2 text-xs text-muted-foreground font-normal">
                  （reserve 有记录但无 capture/release）
                </span>
              </p>
              {outliers.unsettled_jobs.length === 0 ? (
                <EmptyBox text="全部已闭环" />
              ) : (
                <div className="flex flex-wrap gap-2">
                  {outliers.unsettled_jobs.map((id) => (
                    <span
                      key={id}
                      title={id}
                      className="font-mono text-xs rounded border border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/5 text-[color:var(--ochre)] px-2 py-1"
                    >
                      {shortId(id)}
                    </span>
                  ))}
                </div>
              )}
            </div>

            {/* Missing fields jobs */}
            <div>
              <p className="text-sm font-medium text-foreground/80 mb-2">缺字段 Jobs</p>
              {outliers.missing_fields_jobs.length === 0 ? (
                <EmptyBox text="所有 metering_snapshot 字段完整" />
              ) : (
                <div className="space-y-1.5">
                  {outliers.missing_fields_jobs.map((row) => (
                    <div
                      key={row.job_id}
                      className="flex items-center gap-2 rounded border border-border bg-card px-3 py-2 text-sm"
                    >
                      <span
                        title={row.job_id}
                        className="font-mono text-xs text-muted-foreground shrink-0"
                      >
                        {shortId(row.job_id)}
                      </span>
                      <span className="text-muted-foreground">缺</span>
                      <div className="flex flex-wrap gap-1">
                        {row.missing.map((f) => (
                          <span
                            key={f}
                            className="font-mono text-xs rounded bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)] border border-[color:var(--cinnabar)]/20 px-1.5 py-0.5"
                          >
                            {f}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        ) : (
          <EmptyBox text="暂无异常数据" />
        )}
      </section>
    </div>
  )
}
