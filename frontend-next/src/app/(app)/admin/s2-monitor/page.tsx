"use client"

import { useEffect, useState, useCallback } from "react"
import { toast } from "sonner"
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid,
} from "recharts"
import {
  Activity,
  ArrowDown,
  Check,
  ChevronLeft,
  ChevronRight,
  FileQuestion,
  X,
} from "lucide-react"

// ---------------------------------------------------------------------------
// types
// ---------------------------------------------------------------------------

interface PassAggregate {
  total: number
  skipped_count?: number
  missing_artifact_count?: number
  model_downgrade_count: number
  model_downgrade_rate_pct: number
  avg_corrections: number
  avg_sanity_applied?: number
  avg_glossary_terms?: number
  avg_line_change?: number
  total_contract_violations: number
  models_used: Record<string, number>
  avg_duration_ms?: number
  total_parse_failures?: number
  avg_attempts_to_success?: number
}

interface Pass3Aggregate {
  total: number
  missing_count: number
  success_rate_pct: number
  avg_profiles_generated: number
  avg_clips_extracted: number
  total_contract_violations: number
  avg_duration_ms?: number
  total_parse_failures?: number
}

interface Aggregate {
  eligible_total: number
  three_pass_count: number
  legacy_or_old_count: number
  no_s2_data_count: number
  pass1: PassAggregate
  pass2: PassAggregate
  pass3: Pass3Aggregate
}

interface DailyTrend {
  date: string
  job_count: number
  three_pass_count: number
  legacy_count: number
  pass3_success_rate_pct: number | null
  avg_corrections_p2: number
}

interface FilterOptions {
  service_modes: string[]
  review_models: string[]
}

interface JobSummary {
  job_id: string
  video_title: string
  service_mode: string
  status: string
  created_at: string
  eligible: boolean
  orchestrator_mode: string | null
  note?: string
  speakers_count?: number
  pass1_model?: string
  pass1_skipped?: boolean
  pass1_missing?: boolean
  pass1_model_downgrade?: boolean
  pass1_corrections?: number
  pass1_sanity?: number
  pass1_violations?: number
  pass1_has_audio?: boolean
  pass1_duration_ms?: number
  pass1_attempts_count?: number
  pass1_parse_failures?: number
  pass2_model?: string
  pass2_model_downgrade?: boolean
  pass2_corrections?: number
  pass2_glossary_terms?: number
  pass2_violations?: number
  pass2_duration_ms?: number
  pass2_attempts_count?: number
  pass2_parse_failures?: number
  pass3_success?: boolean
  pass3_profiles?: number
  pass3_clips?: number
  pass3_violations?: number
  pass3_duration_ms?: number
  lines_before?: number
  lines_after?: number
}

interface S2StatsResponse {
  filter: { days: number; limit: number; offset: number; service_mode: string; review_model: string }
  filter_options: FilterOptions
  total_jobs_in_range: number
  jobs_eligible: number
  jobs_not_eligible: number
  aggregate: Aggregate
  daily_trends: DailyTrend[]
  jobs: JobSummary[]
}

interface AttemptInfo {
  attempt: number
  label: string
  model: string
  success: boolean
  error: string | null
  response_length?: number
  response_text_preview?: string
  response_text_length?: number
  timestamp: string
}

interface JobDetail {
  job_id: string
  pass1: { result: Record<string, unknown> | null; attempts: AttemptInfo[] }
  pass2: { result: Record<string, unknown> | null; attempts: AttemptInfo[] }
  pass3: { result: Record<string, unknown> | null; attempts: AttemptInfo[] }
  review_result: Record<string, unknown> | null
  audit: Record<string, unknown> | null
  speaker_diff: Record<string, unknown> | null
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "-"
  const d = new Date(iso)
  return d.toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  })
}

function shortId(id: string): string {
  return id.length > 10 ? id.slice(0, 8) + "..." : id
}

function fmtDuration(ms: number | undefined | null): string {
  if (ms == null) return "-"
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

function shortModel(m: string): string {
  return m.replace("gemini-", "").replace("-preview", "")
}

const DAYS_OPTIONS = [
  { value: 7, label: "最近 7 天" },
  { value: 30, label: "最近 30 天" },
  { value: 365, label: "全部" },
]

const selectClass = "bg-muted border border-border rounded px-3 py-1.5 text-sm"

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function fetchS2Stats(
  days: number, limit: number, offset: number,
  serviceMode: string, reviewModel: string,
): Promise<S2StatsResponse> {
  const params = new URLSearchParams({
    days: String(days), limit: String(limit), offset: String(offset),
  })
  if (serviceMode && serviceMode !== "all") params.set("service_mode", serviceMode)
  if (reviewModel) params.set("review_model", reviewModel)
  const resp = await fetch(`/api/admin/s2-stats?${params}`, { credentials: "include" })
  if (resp.status === 403) throw new Error("需要管理员权限")
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}))
    throw new Error((body as { detail?: string }).detail || `HTTP ${resp.status}`)
  }
  return resp.json()
}

async function fetchJobDetail(jobId: string): Promise<JobDetail> {
  const resp = await fetch(`/api/admin/s2-stats/${jobId}`, { credentials: "include" })
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}))
    throw new Error((body as { detail?: string }).detail || `HTTP ${resp.status}`)
  }
  return resp.json()
}

// ---------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------

function StatCard({ label, value, sub, warning }: {
  label: string; value: string | number; sub?: string; warning?: boolean
}) {
  return (
    <div className={`rounded-lg border p-4 ${warning ? "border-[color:var(--ochre)]/40 bg-[color:var(--ochre)]/5" : "border-border bg-card"}`}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`text-2xl font-bold mt-1 ${warning ? "text-[color:var(--ochre)]" : ""}`}>{value}</div>
      {sub && <div className="text-xs text-muted-foreground mt-0.5">{sub}</div>}
    </div>
  )
}

function TrendChart({ trends }: { trends: DailyTrend[] }) {
  if (trends.length < 2) return null
  return (
    <div className="border border-border rounded-lg p-4">
      <h3 className="text-sm font-semibold mb-3">每日趋势</h3>
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={trends} margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 11, fill: "#94a3b8" }}
            tickFormatter={(v: string) => v.slice(5)}
            stroke="#334155"
          />
          <YAxis tick={{ fontSize: 11, fill: "#94a3b8" }} stroke="#334155" />
          <Tooltip
            contentStyle={{
              backgroundColor: "#1e293b",
              border: "1px solid #334155",
              color: "#e2e8f0",
              borderRadius: 8,
              fontSize: 12,
            }}
            labelFormatter={(v) => String(v)}
          />
          <Area
            type="monotone" dataKey="job_count" name="任务数"
            stroke="#8b5cf6" fill="#8b5cf6" fillOpacity={0.15}
            strokeWidth={2}
          />
          <Area
            type="monotone" dataKey="avg_corrections_p2" name="P2 平均修正"
            stroke="#06b6d4" fill="#06b6d4" fillOpacity={0.1}
            strokeWidth={2}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function AttemptChain({ attempts }: { attempts: AttemptInfo[] }) {
  if (!attempts.length) return <span className="text-muted-foreground text-xs">无 attempt 数据</span>
  return (
    <div className="space-y-1.5 text-xs">
      {attempts.map((a, i) => (
        <div key={i} className={`flex items-start gap-2 px-2 py-1 rounded ${a.success ? "bg-[color:var(--bamboo)]/10" : "bg-[color:var(--cinnabar)]/10"}`}>
          <span className="shrink-0 mt-0.5">{a.success ? <Check className="h-3 w-3 text-[color:var(--bamboo)]" /> : <X className="h-3 w-3 text-[color:var(--cinnabar)]" />}</span>
          <div>
            <span className="font-mono">{a.label}</span>
            <span className="text-muted-foreground ml-2">{a.model}</span>
            {a.error && <div className="text-[color:var(--cinnabar)] mt-0.5 break-all">{a.error}</div>}
            {a.response_text_length != null && (
              <span className="text-muted-foreground ml-2">({a.response_text_length} chars)</span>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

function PassSummaryLine({ result }: { result: Record<string, unknown> | null }) {
  if (!result) return null
  const duration = result.duration_ms as number | undefined
  const attempts = result.attempts_count as number | undefined
  const failures = result.parse_failures as number | undefined
  if (duration == null && attempts == null) return null
  return (
    <div className="text-xs text-muted-foreground flex gap-3 mt-1">
      {duration != null && <span>耗时 {fmtDuration(duration)}</span>}
      {attempts != null && <span>第 {attempts} 次成功</span>}
      {(failures ?? 0) > 0 && <span className="text-[color:var(--ochre)]">JSON 解析失败 {failures} 次</span>}
    </div>
  )
}

function DetailDrawer({ jobId, onClose }: { jobId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<JobDetail | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    fetchJobDetail(jobId)
      .then(setDetail)
      .catch((err) => toast.error(err.message))
      .finally(() => setLoading(false))
  }, [jobId])

  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose}>
      <div className="absolute inset-0 bg-black/50" />
      <div
        className="relative w-full max-w-xl bg-background border-l border-border overflow-y-auto p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">S2 详情: {shortId(jobId)}</h3>
          <button onClick={onClose} className="p-1 rounded hover:bg-muted"><X className="h-4 w-4" /></button>
        </div>

        {loading && <div className="text-muted-foreground">加载中...</div>}

        {detail && (
          <div className="space-y-6">
            {(["pass1", "pass2", "pass3"] as const).map((pass) => {
              const data = detail[pass]
              return (
                <div key={pass} className="space-y-2">
                  <h4 className="text-sm font-semibold uppercase text-muted-foreground">{pass}</h4>
                  {data.result ? (
                    <>
                      <PassSummaryLine result={data.result} />
                      <details className="text-xs">
                        <summary className="cursor-pointer text-sm font-medium">Result JSON</summary>
                        <pre className="mt-1 p-2 bg-muted rounded text-xs overflow-x-auto max-h-60">
                          {JSON.stringify(data.result, null, 2)}
                        </pre>
                      </details>
                    </>
                  ) : (
                    <div className="text-xs text-muted-foreground">无 result 文件</div>
                  )}
                  <div>
                    <div className="text-sm font-medium mb-1">Attempt 链</div>
                    <AttemptChain attempts={data.attempts} />
                  </div>
                </div>
              )
            })}

            {detail.review_result && (
              <details className="text-xs">
                <summary className="cursor-pointer text-sm font-semibold">聚合 Review Result</summary>
                <pre className="mt-1 p-2 bg-muted rounded text-xs overflow-x-auto max-h-60">
                  {JSON.stringify(detail.review_result, null, 2)}
                </pre>
              </details>
            )}

            {detail.audit && (
              <details className="text-xs">
                <summary className="cursor-pointer text-sm font-semibold">Audit Trail</summary>
                <pre className="mt-1 p-2 bg-muted rounded text-xs overflow-x-auto max-h-60">
                  {JSON.stringify(detail.audit, null, 2)}
                </pre>
              </details>
            )}

            {detail.speaker_diff && (
              <details className="text-xs">
                <summary className="cursor-pointer text-sm font-semibold">Speaker Diff</summary>
                <pre className="mt-1 p-2 bg-muted rounded text-xs overflow-x-auto max-h-60">
                  {JSON.stringify(detail.speaker_diff, null, 2)}
                </pre>
              </details>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function S2MonitorPage() {
  const [data, setData] = useState<S2StatsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [forbidden, setForbidden] = useState(false)
  const [days, setDays] = useState(7)
  const [offset, setOffset] = useState(0)
  const [serviceMode, setServiceMode] = useState("all")
  const [reviewModel, setReviewModel] = useState("")
  const [selectedJob, setSelectedJob] = useState<string | null>(null)
  const pageSize = 50

  const loadData = useCallback(async () => {
    setLoading(true)
    try {
      const result = await fetchS2Stats(days, pageSize, offset, serviceMode, reviewModel)
      setData(result)
    } catch (err) {
      if (err instanceof Error && err.message.includes("管理员")) {
        setForbidden(true)
      } else {
        toast.error(err instanceof Error ? err.message : "加载失败")
      }
    } finally {
      setLoading(false)
    }
  }, [days, offset, serviceMode, reviewModel])

  useEffect(() => { loadData() }, [loadData])

  if (forbidden) {
    return (
      <div className="flex items-center justify-center h-64 text-muted-foreground">
        需要管理员权限
      </div>
    )
  }

  const agg = data?.aggregate
  const totalPages = data ? Math.ceil(data.total_jobs_in_range / pageSize) : 0
  const currentPage = Math.floor(offset / pageSize) + 1

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-6">
      {/* Header + Filters */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Activity className="h-5 w-5 text-primary" />
          <h1 className="text-xl font-bold">S2 审校效果监控</h1>
        </div>
        <div className="flex items-center gap-2">
          {/* Service mode filter */}
          <select
            value={serviceMode}
            onChange={(e) => { setServiceMode(e.target.value); setOffset(0) }}
            className={selectClass}
          >
            <option value="all">全部模式</option>
            <option value="studio">精配</option>
            <option value="express">快捷</option>
          </select>
          {/* Review model filter */}
          {data?.filter_options?.review_models && data.filter_options.review_models.length > 0 && (
            <select
              value={reviewModel}
              onChange={(e) => { setReviewModel(e.target.value); setOffset(0) }}
              className={selectClass}
            >
              <option value="">全部模型</option>
              {data.filter_options.review_models.map((m) => (
                <option key={m} value={m}>{shortModel(m)}</option>
              ))}
            </select>
          )}
          {/* Time range */}
          <select
            value={days}
            onChange={(e) => { setDays(Number(e.target.value)); setOffset(0) }}
            className={selectClass}
          >
            {DAYS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Loading */}
      {loading && !data && (
        <div className="text-muted-foreground text-center py-12">加载中...</div>
      )}

      {/* Stats Cards */}
      {agg && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            <StatCard
              label="S2 任务数"
              value={agg.eligible_total}
              sub={`/${data!.jobs_eligible + data!.jobs_not_eligible} 总任务`}
            />
            <StatCard
              label="三段式率"
              value={`${agg.eligible_total > 0 ? ((agg.three_pass_count / agg.eligible_total) * 100).toFixed(0) : 0}%`}
              sub={`${agg.three_pass_count}/${agg.eligible_total}`}
            />
            <StatCard
              label="Legacy/旧任务"
              value={agg.legacy_or_old_count}
              sub={agg.eligible_total > 0 ? `${((agg.legacy_or_old_count / agg.eligible_total) * 100).toFixed(0)}%` : "0%"}
              warning={agg.legacy_or_old_count > 0}
            />
            <StatCard
              label="无 S2 数据"
              value={agg.no_s2_data_count}
              warning={agg.no_s2_data_count > 0}
            />
            <StatCard
              label="Pass 3 成功率"
              value={`${agg.pass3.success_rate_pct}%`}
              sub={`${agg.pass3.total - agg.pass3.missing_count}/${agg.pass3.total}`}
              warning={agg.pass3.success_rate_pct < 80}
            />
          </div>

          {/* Info bar */}
          <div className="flex flex-wrap gap-4 text-xs text-muted-foreground">
            {data!.jobs_not_eligible > 0 && (
              <span>不参与统计：{data!.jobs_not_eligible} 个任务（running/queued/cancelled）</span>
            )}
            {(agg.pass1.missing_artifact_count ?? 0) > 0 && (
              <span className="text-[color:var(--cinnabar)]">Pass 1 artifact 缺失：{agg.pass1.missing_artifact_count} 个任务</span>
            )}
            {(agg.pass1.avg_duration_ms ?? 0) > 0 && (
              <span>P1 平均耗时 {fmtDuration(agg.pass1.avg_duration_ms)}</span>
            )}
            {(agg.pass2.avg_duration_ms ?? 0) > 0 && (
              <span>P2 平均耗时 {fmtDuration(agg.pass2.avg_duration_ms)}</span>
            )}
            {(agg.pass3.avg_duration_ms ?? 0) > 0 && (
              <span>P3 平均耗时 {fmtDuration(agg.pass3.avg_duration_ms)}</span>
            )}
            {(agg.pass1.total_parse_failures ?? 0) + (agg.pass2.total_parse_failures ?? 0) > 0 && (
              <span className="text-[color:var(--ochre)]">
                JSON 解析失败 {(agg.pass1.total_parse_failures ?? 0) + (agg.pass2.total_parse_failures ?? 0)} 次
              </span>
            )}
          </div>
        </>
      )}

      {/* Trend Chart */}
      {data?.daily_trends && <TrendChart trends={data.daily_trends} />}

      {/* Job Table */}
      {data && data.jobs.length > 0 && (
        <div className="border border-border rounded-lg overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/30">
                <th className="text-left px-3 py-2 font-medium">Job ID</th>
                <th className="text-left px-3 py-2 font-medium">标题</th>
                <th className="text-left px-3 py-2 font-medium">模式</th>
                <th className="text-left px-3 py-2 font-medium">P1 模型</th>
                <th className="text-right px-3 py-2 font-medium">P1 纠正</th>
                <th className="text-right px-3 py-2 font-medium">P2 修正</th>
                <th className="text-center px-3 py-2 font-medium">P3</th>
                <th className="text-right px-3 py-2 font-medium">行数</th>
                <th className="text-left px-3 py-2 font-medium">时间</th>
              </tr>
            </thead>
            <tbody>
              {data.jobs.map((job) => {
                const isEligible = job.eligible
                const isThreePass = job.orchestrator_mode === "three_pass"
                const isLegacy = job.orchestrator_mode === "legacy_or_old"
                const rowClass = !isEligible ? "opacity-40" : isLegacy ? "opacity-60 bg-muted/20" : ""

                return (
                  <tr
                    key={job.job_id}
                    className={`border-b border-border/50 hover:bg-muted/20 cursor-pointer ${rowClass}`}
                    onClick={() => isEligible && job.orchestrator_mode && setSelectedJob(job.job_id)}
                  >
                    <td className="px-3 py-2 font-mono text-xs">{shortId(job.job_id)}</td>
                    <td className="px-3 py-2 max-w-[180px] truncate">{job.video_title || "-"}</td>
                    <td className="px-3 py-2 text-xs">
                      {job.service_mode === "studio" ? "精配" : job.service_mode === "express" ? "快捷" : job.service_mode || "-"}
                    </td>

                    {/* P1 Model */}
                    <td className="px-3 py-2 text-xs">
                      {!isEligible ? (
                        <span className="text-muted-foreground">-</span>
                      ) : isLegacy ? (
                        <span className="text-muted-foreground">legacy/旧</span>
                      ) : job.pass1_skipped ? (
                        <span className="text-muted-foreground">(跳过)</span>
                      ) : job.pass1_missing ? (
                        <span className="text-[color:var(--cinnabar)]">(缺失)</span>
                      ) : (
                        <span>
                          {shortModel(job.pass1_model || "")}
                          {job.pass1_model_downgrade && (
                            <ArrowDown className="inline h-3 w-3 ml-0.5 text-[color:var(--ochre)]" />
                          )}
                        </span>
                      )}
                    </td>

                    {/* P1 Corrections */}
                    <td className="px-3 py-2 text-right text-xs">
                      {isThreePass && !job.pass1_skipped ? (job.pass1_corrections ?? 0) : "-"}
                    </td>

                    {/* P2 Corrections */}
                    <td className="px-3 py-2 text-right text-xs">
                      {isEligible && (isThreePass || isLegacy) ? (job.pass2_corrections ?? "-") : "-"}
                    </td>

                    {/* P3 */}
                    <td className="px-3 py-2 text-center">
                      {!isEligible ? (
                        <span className="text-muted-foreground">-</span>
                      ) : isLegacy ? (
                        <span className="text-muted-foreground">-</span>
                      ) : job.pass3_success ? (
                        <Check className="inline h-3.5 w-3.5 text-[color:var(--bamboo)]" />
                      ) : (
                        <X className="inline h-3.5 w-3.5 text-[color:var(--cinnabar)]" />
                      )}
                    </td>

                    {/* Lines */}
                    <td className="px-3 py-2 text-right text-xs font-mono">
                      {job.lines_before != null && job.lines_after != null
                        ? `${job.lines_before}\u2192${job.lines_after}`
                        : "-"}
                    </td>

                    <td className="px-3 py-2 text-xs text-muted-foreground">{fmtDate(job.created_at)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between px-3 py-2 border-t border-border text-xs text-muted-foreground">
              <span>第 {currentPage}/{totalPages} 页</span>
              <div className="flex gap-1">
                <button
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - pageSize))}
                  className="p-1 rounded hover:bg-muted disabled:opacity-30"
                >
                  <ChevronLeft className="h-4 w-4" />
                </button>
                <button
                  disabled={currentPage >= totalPages}
                  onClick={() => setOffset(offset + pageSize)}
                  className="p-1 rounded hover:bg-muted disabled:opacity-30"
                >
                  <ChevronRight className="h-4 w-4" />
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Legend */}
      {data && data.jobs.length > 0 && (
        <div className="flex flex-wrap gap-4 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <ArrowDown className="h-3 w-3 text-[color:var(--ochre)]" /> 模型降级
          </span>
          <span className="flex items-center gap-1">
            <X className="h-3 w-3 text-[color:var(--cinnabar)]" /> Pass 3 失败
          </span>
          <span className="flex items-center gap-1">
            <Check className="h-3 w-3 text-[color:var(--bamboo)]" /> Pass 3 成功
          </span>
          <span>legacy/旧 = legacy 回退或三段式上线前的旧任务</span>
        </div>
      )}

      {/* Empty */}
      {data && data.jobs.length === 0 && !loading && (
        <div className="text-center py-12 text-muted-foreground">
          <FileQuestion className="h-8 w-8 mx-auto mb-2 opacity-50" />
          <div>所选时间范围内无任务数据</div>
        </div>
      )}

      {/* Detail Drawer */}
      {selectedJob && (
        <DetailDrawer jobId={selectedJob} onClose={() => setSelectedJob(null)} />
      )}
    </div>
  )
}
