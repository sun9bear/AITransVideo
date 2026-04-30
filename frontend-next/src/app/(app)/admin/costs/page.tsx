"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  AlertTriangle,
  Calculator,
  ChevronDown,
  ChevronRight,
  RefreshCw,
  X,
} from "lucide-react"

type LLMUsageRow = {
  provider: string
  model: string
  model_id: string
  task: string
  phase: string
  calls: number
  success_calls: number
  input_tokens: number
  output_tokens: number
  input_audio_tokens: number
  output_audio_tokens: number
  cached_input_tokens: number
  audio_input_seconds: number
  audio_input_bytes: number
  cost_rmb: number | null
  rate_status: string
  rate_source: string
}

type TTSUsageRow = {
  provider: string
  model: string
  bucket: string
  calls: number
  input_chars: number
  billed_chars: number
  duration_ms: number
  included_in_job_cost: boolean
  cost_rmb: number | null
  rate_status: string
  rate_source: string
}

type CostJob = {
  job_id: string
  title: string
  owner_email: string | null
  owner_display_name: string | null
  status: string
  current_stage: string | null
  service_mode: string | null
  tts_provider: string | null
  tts_model: string | null
  plan_code_snapshot: string | null
  quality_tier: string | null
  created_at: string | null
  completed_at: string | null
  minutes: number | null
  usage_events_count: number
  has_usage_events: boolean
  llm_cost_rmb: number
  tts_cost_rmb: number
  total_cost_rmb: number
  cost_per_minute_rmb: number | null
  credits_charged: number | null
  credits_source: string
  point_price_rmb: number | null
  point_price_source: string
  revenue_estimate_rmb: number | null
  server_overhead_cost_rmb: number | null
  server_cost_per_min_rmb: number | null
  server_cost_source: string
  margin_cost_rmb: number | null
  gross_profit_rmb: number | null
  gross_margin_pct: number | null
  missing_rate_rows: number
  warnings: string[]
  llm: LLMUsageRow[]
  tts: TTSUsageRow[]
}

type CostResponse = {
  window_days: number
  limit: number
  currency: string
  pricing_version: string
  catalog_path: string
  totals: {
    jobs: number
    jobs_with_usage_events: number
    jobs_with_missing_rates: number
    missing_rate_rows: number
    minutes: number
    llm_cost_rmb: number
    tts_cost_rmb: number
    total_cost_rmb: number
    revenue_estimate_rmb: number | null
    server_overhead_cost_rmb: number | null
    server_cost_per_min_rmb: number | null
    server_cost_source: string
    margin_cost_rmb: number | null
    gross_profit_rmb: number | null
    gross_margin_pct: number | null
    point_price_rmb: number | null
    point_price_source: string
    cost_per_minute_rmb: number | null
  }
  jobs: CostJob[]
}

const WINDOW_OPTIONS = [7, 30, 90, 180]
const LIMIT_OPTIONS = [25, 50, 100, 200]

export default function AdminCostManagementPage() {
  const [data, setData] = useState<CostResponse | null>(null)
  const [windowDays, setWindowDays] = useState(7)
  const [limit, setLimit] = useState(50)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expandedJobId, setExpandedJobId] = useState<string | null>(null)

  const loadCosts = useCallback(async (silent = false) => {
    if (silent) {
      setRefreshing(true)
    } else {
      setLoading(true)
    }
    setError(null)
    try {
      const qs = new URLSearchParams({
        window: String(windowDays),
        limit: String(limit),
      })
      const res = await fetch(`/api/admin/costs/jobs?${qs.toString()}`, {
        credentials: "include",
      })
      if (res.status === 403) {
        setForbidden(true)
        return
      }
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }
      const payload = (await res.json()) as CostResponse
      setData(payload)
      setForbidden(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载成本数据失败")
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [limit, windowDays])

  useEffect(() => {
    void loadCosts()
  }, [loadCosts])

  const expandedJob = useMemo(
    () => data?.jobs.find((job) => job.job_id === expandedJobId) ?? null,
    [data, expandedJobId],
  )

  useEffect(() => {
    if (!expandedJobId) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = "hidden"
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setExpandedJobId(null)
      }
    }
    window.addEventListener("keydown", handleKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener("keydown", handleKeyDown)
    }
  }, [expandedJobId])

  if (forbidden) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <p className="text-lg text-muted-foreground">仅管理员可访问</p>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Calculator className="h-6 w-6 text-primary" aria-hidden="true" />
            <h1 className="text-2xl font-bold text-foreground">成本管理</h1>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            按任务、模型和 TTS 引擎拆分用量，并按 Gateway 价格表估算成本
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            className="h-9 rounded-lg border border-border bg-card px-3 text-sm text-foreground"
            value={windowDays}
            onChange={(event) => setWindowDays(Number(event.target.value))}
          >
            {WINDOW_OPTIONS.map((days) => (
              <option key={days} value={days}>近 {days} 天</option>
            ))}
          </select>
          <select
            className="h-9 rounded-lg border border-border bg-card px-3 text-sm text-foreground"
            value={limit}
            onChange={(event) => setLimit(Number(event.target.value))}
          >
            {LIMIT_OPTIONS.map((value) => (
              <option key={value} value={value}>最多 {value} 条</option>
            ))}
          </select>
          <button
            type="button"
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-border bg-card px-3 text-sm text-foreground hover:bg-accent disabled:opacity-60"
            onClick={() => void loadCosts(true)}
            disabled={refreshing || loading}
          >
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} aria-hidden="true" />
            刷新
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {loading ? (
        <div className="rounded-lg border border-border bg-card p-12 text-center text-muted-foreground">
          正在加载成本数据...
        </div>
      ) : data ? (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4 xl:grid-cols-7">
            <MetricCard label="任务数" value={fmt(data.totals.jobs)} sub={`${fmt(data.totals.jobs_with_usage_events)} 个有 usage_events`} />
            <MetricCard label="总成本" value={fmtMoney(data.totals.total_cost_rmb)} sub={perMinute(data.totals.cost_per_minute_rmb)} />
            <MetricCard
              label="预估收入"
              value={fmtMoney(data.totals.revenue_estimate_rmb)}
              sub={`按 ${fmtMoney(data.totals.point_price_rmb)} / 点`}
            />
            <MetricCard
              label="预估毛利率"
              value={fmtPercent(data.totals.gross_margin_pct)}
              sub={`毛利 ${fmtMoney(data.totals.gross_profit_rmb)} · 含服务器估算`}
              warn={(data.totals.gross_margin_pct ?? 100) < 50}
            />
            <MetricCard label="LLM 成本" value={fmtMoney(data.totals.llm_cost_rmb)} sub="文本/音频输入与输出" />
            <MetricCard label="TTS 成本" value={fmtMoney(data.totals.tts_cost_rmb)} sub="按 provider billed_chars" />
            <MetricCard
              label="缺价格项"
              value={fmt(data.totals.missing_rate_rows)}
              sub={`${fmt(data.totals.jobs_with_missing_rates)} 个任务受影响`}
              warn={data.totals.missing_rate_rows > 0}
            />
          </div>

          <div className="rounded-lg border border-border bg-card px-4 py-3 text-xs text-muted-foreground">
            价格版本：<span className="text-foreground">{data.pricing_version}</span>
            <span className="mx-2 text-border">|</span>
            价格表：<span className="text-foreground">{data.catalog_path}</span>
            <span className="mx-2 text-border">|</span>
            收入口径：<span className="text-foreground">实扣/预估点数 × {fmtMoney(data.totals.point_price_rmb)} / 点</span>
            <span className="mx-2 text-border">|</span>
            服务器估算：<span className="text-foreground">{fmtMoney(data.totals.server_cost_per_min_rmb)} / 分钟</span>
          </div>

          <div className="overflow-hidden rounded-lg border border-border bg-card">
            <div className="overflow-x-auto">
                <table className="w-full min-w-[1320px] text-sm">
                <thead className="border-b border-border bg-muted/30 text-xs text-muted-foreground">
                  <tr>
                    <Th>任务</Th>
                    <Th>用户</Th>
                    <Th>状态</Th>
                    <Th right>分钟</Th>
                    <Th right>LLM</Th>
                    <Th right>TTS</Th>
                    <Th right>总成本</Th>
                    <Th right>预估收入</Th>
                    <Th right>毛利率</Th>
                    <Th right>每分钟</Th>
                    <Th right>事件</Th>
                    <Th right>缺价</Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {data.jobs.length === 0 ? (
                    <tr>
                      <td colSpan={12} className="px-4 py-10 text-center text-muted-foreground">
                        当前窗口暂无任务
                      </td>
                    </tr>
                  ) : (
                    data.jobs.map((job) => (
                      <CostJobRow
                        key={job.job_id}
                        job={job}
                        expanded={expandedJobId === job.job_id}
                        onToggle={() => setExpandedJobId(expandedJobId === job.job_id ? null : job.job_id)}
                      />
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {expandedJob && (
            <JobDetails job={expandedJob} onClose={() => setExpandedJobId(null)} />
          )}
        </>
      ) : null}
    </div>
  )
}

function CostJobRow({
  job,
  expanded,
  onToggle,
}: {
  job: CostJob
  expanded: boolean
  onToggle: () => void
}) {
  return (
    <tr className="hover:bg-muted/20">
      <Td>
        <button
          type="button"
          className="flex max-w-[320px] items-center gap-2 text-left"
          onClick={onToggle}
        >
          {expanded ? (
            <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          ) : (
            <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          )}
          <span className="min-w-0">
            <span className="block truncate font-medium text-foreground">{job.title}</span>
            <span className="block truncate text-xs text-muted-foreground">{job.job_id}</span>
          </span>
        </button>
      </Td>
      <Td>
        <span className="block max-w-[180px] truncate">{job.owner_email || job.owner_display_name || "-"}</span>
      </Td>
      <Td>
        <div className="flex flex-wrap gap-1.5">
          <Badge>{job.status}</Badge>
          {job.service_mode && <Badge muted>{job.service_mode}</Badge>}
        </div>
      </Td>
      <Td right>{job.minutes ? fmt(job.minutes) : "-"}</Td>
      <Td right>{fmtMoney(job.llm_cost_rmb)}</Td>
      <Td right>{fmtMoney(job.tts_cost_rmb)}</Td>
      <Td right>
        <span className="font-medium text-foreground">{fmtMoney(job.total_cost_rmb)}</span>
      </Td>
      <Td right>{fmtMoney(job.revenue_estimate_rmb)}</Td>
      <Td right>
        <span className={marginClass(job.gross_margin_pct)}>
          {fmtPercent(job.gross_margin_pct)}
        </span>
      </Td>
      <Td right>{job.cost_per_minute_rmb == null ? "-" : fmtMoney(job.cost_per_minute_rmb)}</Td>
      <Td right>
        <span className={job.has_usage_events ? "text-green-300" : "text-yellow-300"}>
          {fmt(job.usage_events_count)}
        </span>
      </Td>
      <Td right>
        {job.missing_rate_rows > 0 ? (
          <span className="inline-flex items-center justify-end gap-1 text-yellow-300">
            <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
            {fmt(job.missing_rate_rows)}
          </span>
        ) : (
          <span className="text-green-300">0</span>
        )}
      </Td>
    </tr>
  )
}

function JobDetails({ job, onClose }: { job: CostJob; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label="任务成本详情"
        className="h-full w-full max-w-6xl overflow-y-auto border-l border-border bg-background shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="sticky top-0 z-10 border-b border-border bg-background/95 px-4 py-4 backdrop-blur lg:px-6">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h2 className="truncate text-lg font-semibold text-foreground">{job.title}</h2>
              <p className="mt-1 text-xs text-muted-foreground">
                {job.job_id} · 创建时间 {fmtDate(job.created_at)} · TTS {job.tts_provider || "-"} / {job.tts_model || "-"}
              </p>
            </div>
            <button
              type="button"
              className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground hover:bg-accent hover:text-foreground"
              onClick={onClose}
              aria-label="关闭详情"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
        </div>
        <div className="space-y-4 p-4 lg:p-6">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
            <MetricCard label="总成本" value={fmtMoney(job.total_cost_rmb)} sub={perMinute(job.cost_per_minute_rmb)} />
            <MetricCard
              label="预估收入"
              value={fmtMoney(job.revenue_estimate_rmb)}
              sub={`${fmt(job.credits_charged ?? 0)} 点 · ${creditsSourceLabel(job.credits_source)}`}
            />
            <MetricCard
              label="预估毛利率"
              value={fmtPercent(job.gross_margin_pct)}
              sub={`毛利 ${fmtMoney(job.gross_profit_rmb)}`}
              warn={(job.gross_margin_pct ?? 100) < 50}
            />
            <MetricCard label="LLM 成本" value={fmtMoney(job.llm_cost_rmb)} sub={`${fmt(job.llm.length)} 个模型/阶段项`} />
            <MetricCard label="TTS 成本" value={fmtMoney(job.tts_cost_rmb)} sub={`${fmt(job.tts.length)} 个 TTS bucket`} />
            <MetricCard label="缺价格项" value={fmt(job.missing_rate_rows)} sub="缺失时不会计入总成本" warn={job.missing_rate_rows > 0} />
          </div>

          <div className="rounded-lg border border-border bg-card px-3 py-2 text-xs text-muted-foreground">
            收入估算：{creditsSourceLabel(job.credits_source)}
            {job.credits_charged == null ? "" : ` ${fmt(job.credits_charged)} 点`}
            <span className="mx-2 text-border">|</span>
            点数单价：<span className="text-foreground">{fmtMoney(job.point_price_rmb)} / 点</span>
            <span className="mx-2 text-border">|</span>
            服务器估算：<span className="text-foreground">{fmtMoney(job.server_overhead_cost_rmb)}</span>
            <span className="mx-2 text-border">|</span>
            毛利成本基数：<span className="text-foreground">{fmtMoney(job.margin_cost_rmb)}</span>
            <span className="mx-2 text-border">|</span>
            套餐快照：<span className="text-foreground">{job.plan_code_snapshot || "-"}</span>
          </div>

          {job.warnings.length > 0 && (
            <div className="rounded-lg border border-yellow-500/30 bg-yellow-500/5 px-3 py-2 text-xs text-yellow-200">
              {job.warnings.slice(0, 3).join("；")}
            </div>
          )}

          <UsageTable title="大模型调用">
            <thead className="border-b border-border bg-muted/30 text-xs text-muted-foreground">
              <tr>
                <Th>阶段/任务</Th>
                <Th>Provider</Th>
                <Th>Model</Th>
                <Th right>Calls</Th>
                <Th right>输入 token</Th>
                <Th right>输出 token</Th>
                <Th right>音频秒</Th>
                <Th right>成本</Th>
                <Th>价格</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {job.llm.length === 0 ? (
                <EmptyRow colSpan={9} text="没有 LLM usage 记录" />
              ) : (
                job.llm.map((row, index) => (
                  <tr key={`${row.provider}-${row.model_id}-${row.task}-${row.phase}-${index}`}>
                    <Td>
                      <span className="block font-medium text-foreground">{taskLabel(row.task)}</span>
                      <span className="block text-xs text-muted-foreground">{row.phase || "-"}</span>
                    </Td>
                    <Td>{row.provider}</Td>
                    <Td>
                      <span className="block max-w-[220px] truncate">{row.model_id || row.model}</span>
                    </Td>
                    <Td right>{fmt(row.calls)}</Td>
                    <Td right>{fmt(row.input_tokens + row.cached_input_tokens + row.input_audio_tokens)}</Td>
                    <Td right>{fmt(row.output_tokens + row.output_audio_tokens)}</Td>
                    <Td right>{row.audio_input_seconds ? fmt(row.audio_input_seconds) : "-"}</Td>
                    <Td right>{row.cost_rmb == null ? "-" : fmtMoney(row.cost_rmb)}</Td>
                    <Td><RateStatus row={row} /></Td>
                  </tr>
                ))
              )}
            </tbody>
          </UsageTable>

          <UsageTable title="TTS 合成">
            <thead className="border-b border-border bg-muted/30 text-xs text-muted-foreground">
              <tr>
                <Th>Bucket</Th>
                <Th>Provider</Th>
                <Th>Model</Th>
                <Th right>Calls</Th>
                <Th right>输入字符</Th>
                <Th right>计费字符</Th>
                <Th right>成本</Th>
                <Th>价格</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {job.tts.length === 0 ? (
                <EmptyRow colSpan={8} text="没有 TTS usage 记录" />
              ) : (
                job.tts.map((row, index) => (
                  <tr key={`${row.provider}-${row.model}-${row.bucket}-${index}`}>
                    <Td>
                      <span className="font-medium text-foreground">{bucketLabel(row.bucket)}</span>
                    </Td>
                    <Td>{row.provider}</Td>
                    <Td>{row.model}</Td>
                    <Td right>{fmt(row.calls)}</Td>
                    <Td right>{fmt(row.input_chars)}</Td>
                    <Td right>{fmt(row.billed_chars)}</Td>
                    <Td right>{row.cost_rmb == null ? "-" : fmtMoney(row.cost_rmb)}</Td>
                    <Td><RateStatus row={row} /></Td>
                  </tr>
                ))
              )}
            </tbody>
          </UsageTable>
        </div>
      </section>
    </div>
  )
}

function UsageTable({
  title,
  children,
}: {
  title: string
  children: React.ReactNode
}) {
  return (
    <div>
      <h3 className="mb-2 text-sm font-medium text-foreground">{title}</h3>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full min-w-[900px] text-sm">
          {children}
        </table>
      </div>
    </div>
  )
}

function RateStatus({ row }: { row: LLMUsageRow | TTSUsageRow }) {
  if (row.rate_status === "configured") {
    return (
      <span className="inline-flex rounded-md bg-green-500/10 px-2 py-0.5 text-xs text-green-300">
        已配置
      </span>
    )
  }
  if (row.rate_status === "excluded_interactive") {
    return (
      <span className="inline-flex rounded-md bg-muted px-2 py-0.5 text-xs text-muted-foreground">
        不计入任务
      </span>
    )
  }
  return (
    <span className="inline-flex rounded-md bg-yellow-500/10 px-2 py-0.5 text-xs text-yellow-300">
      缺价格
    </span>
  )
}

function MetricCard({
  label,
  value,
  sub,
  warn = false,
}: {
  label: string
  value: string
  sub: string
  warn?: boolean
}) {
  return (
    <div className={`rounded-lg border bg-card p-4 ${warn ? "border-yellow-500/30" : "border-border"}`}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${warn ? "text-yellow-300" : "text-foreground"}`}>
        {value}
      </p>
      <p className="mt-1 truncate text-xs text-muted-foreground">{sub}</p>
    </div>
  )
}

function Badge({ children, muted = false }: { children: React.ReactNode; muted?: boolean }) {
  return (
    <span className={`rounded-md px-2 py-0.5 text-xs ${muted ? "bg-muted text-muted-foreground" : "bg-primary/10 text-primary"}`}>
      {children}
    </span>
  )
}

function Th({ children, right = false }: { children: React.ReactNode; right?: boolean }) {
  return (
    <th className={`px-4 py-3 font-medium ${right ? "text-right" : "text-left"}`}>
      {children}
    </th>
  )
}

function Td({ children, right = false }: { children: React.ReactNode; right?: boolean }) {
  return (
    <td className={`px-4 py-3 align-middle text-muted-foreground ${right ? "text-right tabular-nums" : ""}`}>
      {children}
    </td>
  )
}

function EmptyRow({ colSpan, text }: { colSpan: number; text: string }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-4 py-8 text-center text-muted-foreground">
        {text}
      </td>
    </tr>
  )
}

function fmt(value: number) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)
}

function fmtMoney(value: number | null) {
  if (value == null) return "-"
  return `¥${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: value >= 1 ? 2 : 4,
    maximumFractionDigits: value >= 1 ? 2 : 4,
  }).format(value)}`
}

function fmtPercent(value: number | null) {
  if (value == null) return "-"
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(value)}%`
}

function marginClass(value: number | null) {
  if (value == null) return "text-muted-foreground"
  if (value >= 70) return "font-medium text-green-300"
  if (value >= 50) return "font-medium text-yellow-200"
  return "font-medium text-red-300"
}

function perMinute(value: number | null) {
  return value == null ? "无分钟数" : `${fmtMoney(value)} / 分钟`
}

function fmtDate(value: string | null) {
  if (!value) return "-"
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value))
}

function bucketLabel(value: string) {
  const labels: Record<string, string> = {
    probe_tts: "探针 TTS",
    first_tts: "首次 TTS",
    post_tts_resynth: "TTS 后重合成",
    post_edit_resynth: "编辑后再生成",
    interactive_preview: "试听预览",
  }
  return labels[value] ?? value
}

function taskLabel(value: string) {
  const labels: Record<string, string> = {
    pass1: "S2 Pass1",
    pass2: "S2 Pass2",
    pass3: "S2 Pass3",
    speaker_verifier: "说话人 verifier",
    legacy_review: "Legacy review",
    probe_translate: "探针翻译",
    s3_translate: "正式翻译",
    pre_tts_rewrite: "pre-TTS 重写",
    post_tts_rewrite: "TTS 后重写",
    s1_gemini_transcribe: "S1 Gemini 转录",
    snapshot_total: "Snapshot 汇总",
  }
  return labels[value] ?? value
}

function creditsSourceLabel(value: string) {
  const labels: Record<string, string> = {
    credits_ledger_capture: "实际账本扣点",
    credits_actual: "实扣点数",
    credits_estimated: "预估点数",
    derived_from_actual_minutes: "实际分钟数推导",
    derived_from_minutes: "分钟数推导",
    missing: "缺少点数",
  }
  return labels[value] ?? value
}
