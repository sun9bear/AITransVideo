"use client"

import { useState, useCallback, useRef, useEffect } from "react"
import { Loader2, ClipboardList, Sparkles, ChevronDown } from "lucide-react"
import { toast } from "sonner"
import { usePollingTask } from "@/lib/react/usePollingTask"
import { LogViewer } from "@/components/log-viewer"
import { toJobLogEntries } from "@/lib/api/mappers"
import type { ApiJobEvent } from "@/types/api"
import type { JobLogEntry } from "@/types/jobs"

type AdminJob = {
  job_id: string
  video_title: string
  owner_email: string
  status: string
  current_stage: string
  created_at: string
  // Phase 2 Task 0 — metering snapshot exposed via /api/admin/jobs.
  // All fields are optional / best-effort; pipeline reports them at S6 completion.
  metering_snapshot?: {
    total_segments?: number
    catalog_hit_count?: number
    catalog_hit_rate?: number
    skip_probe?: boolean
    rewrite_count?: number
    rewrite_triggered?: boolean
    needs_review_count?: number
    needs_review_rate?: number
    first_pass_error_pct_avg?: number
    first_pass_error_pct_p50?: number
    first_pass_error_pct_p90?: number
    first_pass_error_pct_n?: number
    alignment_method_distribution?: Record<string, number>
    speed_param_distribution?: Record<string, number>
    glossary_total_terms?: number
    glossary_preserved_terms?: number
    term_preservation_rate?: number
    missing_glossary_terms?: string[]
    final_cn_chars?: number
    tts_billed_chars?: number
  }
}

type AnalysisTimelineItem = {
  stage: string
  start: string
  end: string
  duration: string
  note?: string
}

type AnalysisIssue = {
  title: string
  severity: "high" | "medium" | "low"
  detail: string
  evidence: string
}

type AnalysisResult = {
  summary: string
  timeline: AnalysisTimelineItem[]
  issues: AnalysisIssue[]
  suggestions: string[]
}

// Ink palette: same semantics as the user-facing StatusBadge (see
// components/status-badge.tsx). Wrapped here as plain class strings
// because the admin job table renders raw badges inline rather than
// going through StatusBadge.
const STATUS_BADGE: Record<string, string> = {
  running: "border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/10 text-[color:var(--ochre)]",
  queued: "border-muted-foreground/30 bg-muted/40 text-muted-foreground",
  waiting_for_review: "border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/10 text-[color:var(--ochre)]",
  succeeded: "border-[color:var(--bamboo)]/30 bg-[color:var(--bamboo)]/10 text-[color:var(--bamboo)]",
  failed: "border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)]",
  cancelled: "border-muted-foreground/30 bg-muted/40 text-muted-foreground",
}

const STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  queued: "排队中",
  waiting_for_review: "待审核",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
}

const CANCELABLE = new Set(["running", "queued", "waiting_for_review"])

const SEVERITY_STYLE: Record<string, string> = {
  high: "border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)]",
  medium: "border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/10 text-[color:var(--ochre)]",
  low: "border-muted-foreground/30 bg-muted/40 text-muted-foreground",
}

const SEVERITY_LABEL: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
}

function formatDate(iso: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(iso))
  } catch {
    return iso
  }
}

export default function AdminJobsPage() {
  const [jobs, setJobs] = useState<AdminJob[]>([])
  const [loading, setLoading] = useState(true)
  const [forbidden, setForbidden] = useState(false)
  const [acting, setActing] = useState<string | null>(null)

  // Log expansion state
  const [expandedJobId, setExpandedJobId] = useState<string | null>(null)
  const [logs, setLogs] = useState<JobLogEntry[]>([])
  const [logsLoading, setLogsLoading] = useState(false)
  const logsAbortRef = useRef<AbortController | null>(null)

  // AI analysis state
  const [analysis, setAnalysis] = useState<AnalysisResult | null>(null)
  const [analysisError, setAnalysisError] = useState<string | null>(null)
  const [analyzing, setAnalyzing] = useState(false)
  const analysisCacheRef = useRef<Record<string, AnalysisResult>>({})
  const analysisAbortRef = useRef<AbortController | null>(null)

  const loadJobs = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const res = await fetch("/api/admin/jobs", { credentials: "include" })
      if (res.status === 403) {
        setForbidden(true)
        return
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setJobs(data.jobs ?? data)
      setForbidden(false)
    } catch (err) {
      if (!silent) toast.error("加载任务列表失败")
      console.error(err)
    } finally {
      if (!silent) setLoading(false)
    }
  }, [])

  // Initial non-silent load (toggles loading spinner)
  useEffect(() => { loadJobs() }, [loadJobs])
  // Subsequent silent polling (no spinner flicker)
  usePollingTask(() => loadJobs(true), { intervalMs: 10000, immediate: false })

  const handleRowClick = useCallback(async (jobId: string) => {
    // Abort any in-flight requests from previous row and reset stale state
    logsAbortRef.current?.abort()
    analysisAbortRef.current?.abort()
    setAnalyzing(false)
    setLogsLoading(false)

    if (expandedJobId === jobId) {
      setExpandedJobId(null)
      setLogs([])
      setAnalysis(null)
      setAnalysisError(null)
      return
    }

    setExpandedJobId(jobId)
    setLogs([])
    setAnalysis(null)
    setAnalysisError(null)
    setLogsLoading(true)

    const controller = new AbortController()
    logsAbortRef.current = controller

    try {
      const res = await fetch(`/api/admin/jobs/${jobId}/logs`, {
        credentials: "include",
        signal: controller.signal,
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      if (controller.signal.aborted) return
      const entries = toJobLogEntries(data.events as ApiJobEvent[])
      setLogs(entries)
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return
      toast.error("加载日志失败")
      setLogs([])
    } finally {
      if (!controller.signal.aborted) {
        setLogsLoading(false)
      }
    }

    // Restore cached analysis if available
    if (!controller.signal.aborted) {
      const cached = analysisCacheRef.current[jobId]
      if (cached) {
        setAnalysis(cached)
      }
    }
  }, [expandedJobId])

  const handleAnalyze = useCallback(async (jobId: string) => {
    // Check cache first
    const cached = analysisCacheRef.current[jobId]
    if (cached) {
      setAnalysis(cached)
      setAnalysisError(null)
      return
    }

    // Abort any previous analysis request
    analysisAbortRef.current?.abort()
    const controller = new AbortController()
    analysisAbortRef.current = controller

    setAnalyzing(true)
    setAnalysis(null)
    setAnalysisError(null)

    try {
      const res = await fetch(`/api/admin/jobs/${jobId}/analyze-logs`, {
        method: "POST",
        credentials: "include",
        signal: controller.signal,
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      if (controller.signal.aborted) return
      if (data.error) {
        setAnalysisError(data.error)
      } else if (data.analysis) {
        setAnalysis(data.analysis)
        analysisCacheRef.current[jobId] = data.analysis
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return
      setAnalysisError("请求失败，请稍后重试")
    } finally {
      if (!controller.signal.aborted) {
        setAnalyzing(false)
      }
    }
  }, [])

  const handleCancel = async (jobId: string) => {
    setActing(jobId)
    try {
      const res = await fetch(`/api/admin/jobs/${jobId}/cancel`, {
        method: "POST",
        credentials: "include",
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      toast.success("任务已取消")
      await loadJobs(true)
    } catch {
      toast.error("取消任务失败")
    } finally {
      setActing(null)
    }
  }

  const handleDelete = async (jobId: string) => {
    if (!confirm("确定要删除该任务吗？此操作不可恢复。")) return
    setActing(jobId)
    try {
      const res = await fetch(`/api/admin/jobs/${jobId}/delete`, {
        method: "POST",
        credentials: "include",
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      toast.success("任务已删除")
      await loadJobs(true)
    } catch {
      toast.error("删除任务失败")
    } finally {
      setActing(null)
    }
  }

  if (forbidden) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <p className="text-lg text-muted-foreground">仅管理员可访问</p>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-foreground">任务管理</h1>
        <span className="text-sm text-muted-foreground">
          共 {jobs.length} 个任务
        </span>
      </div>

      {jobs.length === 0 ? (
        <div className="rounded-2xl border border-border bg-card p-12 text-center">
          <ClipboardList className="mx-auto mb-3 h-10 w-10 text-muted-foreground" />
          <p className="text-muted-foreground">当前没有任务</p>
        </div>
      ) : (
        <div className="rounded-2xl border border-border bg-card p-5 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-muted-foreground">
                <th className="pb-3 pr-4 font-medium">Job ID</th>
                <th className="pb-3 pr-4 font-medium">视频标题</th>
                <th className="pb-3 pr-4 font-medium">用户</th>
                <th className="pb-3 pr-4 font-medium">状态</th>
                <th className="pb-3 pr-4 font-medium">阶段</th>
                <th className="pb-3 pr-4 font-medium">创建时间</th>
                <th className="pb-3 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <JobRow
                  key={job.job_id}
                  job={job}
                  isExpanded={expandedJobId === job.job_id}
                  acting={acting}
                  logs={expandedJobId === job.job_id ? logs : []}
                  logsLoading={expandedJobId === job.job_id && logsLoading}
                  analysis={expandedJobId === job.job_id ? analysis : null}
                  analysisError={expandedJobId === job.job_id ? analysisError : null}
                  analyzing={expandedJobId === job.job_id && analyzing}
                  onRowClick={handleRowClick}
                  onAnalyze={handleAnalyze}
                  onCancel={handleCancel}
                  onDelete={handleDelete}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Job row + expandable panel
// ---------------------------------------------------------------------------

function JobRow({
  job,
  isExpanded,
  acting,
  logs,
  logsLoading,
  analysis,
  analysisError,
  analyzing,
  onRowClick,
  onAnalyze,
  onCancel,
  onDelete,
}: {
  job: AdminJob
  isExpanded: boolean
  acting: string | null
  logs: JobLogEntry[]
  logsLoading: boolean
  analysis: AnalysisResult | null
  analysisError: string | null
  analyzing: boolean
  onRowClick: (jobId: string) => void
  onAnalyze: (jobId: string) => void
  onCancel: (jobId: string) => void
  onDelete: (jobId: string) => void
}) {
  return (
    <>
      <tr
        className="border-b border-border cursor-pointer transition-colors hover:bg-muted/20"
        onClick={() => onRowClick(job.job_id)}
      >
        <td className="py-3 pr-4 font-mono text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <ChevronDown
              className={`h-3 w-3 transition-transform ${isExpanded ? "rotate-0" : "-rotate-90"}`}
            />
            {job.job_id.slice(0, 8)}
          </span>
        </td>
        <td className="py-3 pr-4 text-foreground">
          {job.video_title || "—"}
        </td>
        <td className="py-3 pr-4 text-muted-foreground">
          {job.owner_email || "—"}
        </td>
        <td className="py-3 pr-4">
          <span
            className={`inline-block rounded-full border px-2 py-0.5 text-xs ${STATUS_BADGE[job.status] ?? "border-gray-500/30 bg-gray-500/10 text-gray-400"}`}
          >
            {STATUS_LABEL[job.status] ?? job.status}
          </span>
        </td>
        <td className="py-3 pr-4 text-muted-foreground">
          {job.current_stage || "—"}
        </td>
        <td className="py-3 pr-4 text-muted-foreground whitespace-nowrap">
          {formatDate(job.created_at)}
        </td>
        <td className="py-3">
          <div className="flex gap-2" onClick={(e) => e.stopPropagation()}>
            {CANCELABLE.has(job.status) && (
              <button
                disabled={acting === job.job_id}
                onClick={() => onCancel(job.job_id)}
                className="rounded-lg px-3 py-1 text-xs disabled:opacity-50 border border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/10 text-[color:var(--ochre)] hover:bg-[color:var(--ochre)]/20"
              >
                取消
              </button>
            )}
            <button
              disabled={acting === job.job_id}
              onClick={() => onDelete(job.job_id)}
              className="rounded-lg border border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/10 px-3 py-1 text-xs text-[color:var(--cinnabar)] hover:bg-[color:var(--cinnabar)]/20 disabled:opacity-50"
            >
              删除
            </button>
          </div>
        </td>
      </tr>
      {isExpanded && (
        <tr>
          <td colSpan={7} className="p-0">
            <div className="border-b border-border bg-muted/5 px-5 py-4 space-y-4">
              {/* Phase 2 Task 0 — Metrics panel */}
              <MeteringPanel snapshot={job.metering_snapshot} />

              {/* Log viewer */}
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-foreground">
                  任务日志 — {job.job_id.slice(0, 12)}...
                </h3>
                <button
                  disabled={analyzing}
                  onClick={() => onAnalyze(job.job_id)}
                  className="flex items-center gap-1.5 rounded-lg border border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/10 px-3 py-1 text-xs font-medium text-[color:var(--ochre)] hover:bg-[color:var(--ochre)]/20 disabled:opacity-50 transition-colors"
                >
                  {analyzing ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <Sparkles className="h-3 w-3" />
                  )}
                  {analyzing ? "分析中..." : "AI 分析"}
                </button>
              </div>

              {logsLoading ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
                </div>
              ) : (
                <LogViewer
                  entries={logs}
                  title="事件日志"
                  description="完整事件日志，默认展示最近 20 条。"
                  emptyMessage="该任务暂无日志。"
                  initialVisibleCount={20}
                />
              )}

              {/* AI analysis error */}
              {analysisError && (
                <div className="rounded-xl border border-[color:var(--cinnabar)]/20 bg-[color:var(--cinnabar)]/8 px-4 py-3 text-sm text-[color:var(--cinnabar)]">
                  {analysisError}
                </div>
              )}

              {/* AI analysis result */}
              {analysis && <AnalysisPanel analysis={analysis} />}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// AI Analysis panel
// ---------------------------------------------------------------------------

function AnalysisPanel({ analysis }: { analysis: AnalysisResult }) {
  return (
    <div className="space-y-4 rounded-xl border border-[color:var(--ochre)]/20 bg-[color:var(--ochre)]/5 p-4">
      <h3 className="flex items-center gap-1.5 text-sm font-semibold text-[color:var(--ochre)]">
        <Sparkles className="h-3.5 w-3.5" />
        AI 分析结果
      </h3>

      {/* Summary */}
      <div>
        <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-1">
          概要
        </h4>
        <p className="text-sm text-foreground leading-6">{analysis.summary}</p>
      </div>

      {/* Timeline */}
      {analysis.timeline.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
            流程耗时
          </h4>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-muted-foreground">
                  <th className="pb-1 pr-3 font-medium">阶段</th>
                  <th className="pb-1 pr-3 font-medium">开始</th>
                  <th className="pb-1 pr-3 font-medium">结束</th>
                  <th className="pb-1 pr-3 font-medium">耗时</th>
                  <th className="pb-1 font-medium">备注</th>
                </tr>
              </thead>
              <tbody>
                {analysis.timeline.map((item, i) => (
                  <tr key={i} className="text-foreground/80">
                    <td className="py-1 pr-3">{item.stage}</td>
                    <td className="py-1 pr-3 font-mono">{item.start}</td>
                    <td className="py-1 pr-3 font-mono">{item.end}</td>
                    <td className="py-1 pr-3 font-mono">{item.duration}</td>
                    <td className="py-1 text-muted-foreground">
                      {item.note || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Issues */}
      {analysis.issues.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
            发现的问题
          </h4>
          <div className="space-y-2">
            {analysis.issues.map((issue, i) => (
              <div
                key={i}
                className={`rounded-lg border px-3 py-2 ${SEVERITY_STYLE[issue.severity] ?? SEVERITY_STYLE.low}`}
              >
                <div className="flex items-center gap-2 text-xs font-semibold">
                  <span className="rounded-full border border-current/30 px-1.5 py-0.5 text-[10px]">
                    {SEVERITY_LABEL[issue.severity] ?? issue.severity}
                  </span>
                  <span>{issue.title}</span>
                </div>
                <p className="mt-1 text-xs text-foreground/70 leading-5">
                  {issue.detail}
                </p>
                {issue.evidence && (
                  <p className="mt-1 text-xs font-mono text-muted-foreground leading-5">
                    {issue.evidence}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Suggestions */}
      {analysis.suggestions.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
            建议
          </h4>
          <ol className="list-decimal list-inside space-y-1 text-sm text-foreground/80 leading-6">
            {analysis.suggestions.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        </div>
      )}

      {/* No issues */}
      {analysis.issues.length === 0 && analysis.suggestions.length === 0 && (
        <p className="text-xs text-muted-foreground">
          未发现异常，任务流程正常。
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Phase 2 Task 0 — Metering panel (catalog hit / rewrite / first-pass error)
// Renders 6 mini-stat cards + alignment method distribution + glossary status.
// All fields are optional; "—" shown when missing so that legacy jobs without
// the new metering keys don't render as zeros.
// ---------------------------------------------------------------------------

function MeteringPanel({
  snapshot,
}: {
  snapshot?: AdminJob["metering_snapshot"]
}) {
  if (!snapshot || Object.keys(snapshot).length === 0) {
    return (
      <div className="rounded-xl border border-border bg-muted/10 px-4 py-3 text-xs text-muted-foreground">
        指标尚未上报（任务可能未运行至 S6，或为遗留 job）
      </div>
    )
  }

  const total = snapshot.total_segments ?? 0
  const catalogPct = formatPct(snapshot.catalog_hit_rate)
  const rewriteCount = snapshot.rewrite_count ?? 0
  const rewriteRate = total > 0 ? rewriteCount / total : 0
  const reviewPct = formatPct(snapshot.needs_review_rate)
  const fpAvg = formatPct(snapshot.first_pass_error_pct_avg)
  const fpP90 = formatPct(snapshot.first_pass_error_pct_p90)
  const termPct = formatPct(snapshot.term_preservation_rate)

  return (
    <div className="rounded-xl border border-border bg-muted/10 px-4 py-3 space-y-3">
      <h3 className="text-sm font-semibold text-foreground">任务指标</h3>

      {/* Top metrics row */}
      <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
        <MetricCard
          label="总段数"
          value={total.toString()}
        />
        <MetricCard
          label="目录命中"
          value={catalogPct}
          tone={pickToneByRate(snapshot.catalog_hit_rate, 'higher_better')}
          subtitle={snapshot.skip_probe ? '已跳过 probe' : '走 probe'}
        />
        <MetricCard
          label="重写率"
          value={`${(rewriteRate * 100).toFixed(1)}%`}
          tone={pickToneByRate(rewriteRate, 'lower_better')}
          subtitle={`${rewriteCount} 次`}
        />
        <MetricCard
          label="需复核"
          value={reviewPct}
          tone={pickToneByRate(snapshot.needs_review_rate, 'lower_better')}
          subtitle={`${snapshot.needs_review_count ?? 0} 段`}
        />
        <MetricCard
          label="首轮误差均值"
          value={fpAvg}
          tone={pickToneByPct(snapshot.first_pass_error_pct_avg, 0.08, 0.15)}
        />
        <MetricCard
          label="首轮误差 P90"
          value={fpP90}
          tone={pickToneByPct(snapshot.first_pass_error_pct_p90, 0.15, 0.25)}
          subtitle={`n=${snapshot.first_pass_error_pct_n ?? 0}`}
        />
      </div>

      {/* Alignment + speed distributions */}
      {(snapshot.alignment_method_distribution ||
        snapshot.speed_param_distribution) && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
          {snapshot.alignment_method_distribution && (
            <div>
              <span className="font-medium text-muted-foreground">对齐方式：</span>
              {Object.entries(snapshot.alignment_method_distribution).map(([k, v]) => (
                <span key={k} className="ml-2 inline-block rounded bg-muted/40 px-1.5 py-0.5">
                  {k}={v}
                </span>
              ))}
            </div>
          )}
          {snapshot.speed_param_distribution && (
            <div>
              <span className="font-medium text-muted-foreground">TTS speed：</span>
              {Object.entries(snapshot.speed_param_distribution).map(([k, v]) => (
                <span key={k} className="ml-2 inline-block rounded bg-muted/40 px-1.5 py-0.5">
                  {k}={v}
                </span>
              ))}
              <span className="ml-2 text-muted-foreground/70">(Task 1 启用后会有非默认值)</span>
            </div>
          )}
        </div>
      )}

      {/* Glossary preservation */}
      {snapshot.glossary_total_terms != null && snapshot.glossary_total_terms > 0 && (
        <div className="text-xs">
          <span className="font-medium text-muted-foreground">术语保留：</span>
          <span
            className={`ml-2 ${
              (snapshot.term_preservation_rate ?? 0) >= 0.95
                ? 'text-[color:var(--bamboo)]'
                : (snapshot.term_preservation_rate ?? 0) >= 0.8
                ? 'text-[color:var(--ochre)]'
                : 'text-[color:var(--cinnabar)]'
            }`}
          >
            {snapshot.glossary_preserved_terms ?? 0} / {snapshot.glossary_total_terms} ({termPct})
          </span>
          {snapshot.missing_glossary_terms && snapshot.missing_glossary_terms.length > 0 && (
            <div className="mt-1 text-muted-foreground">
              缺失：{snapshot.missing_glossary_terms.join('、')}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function MetricCard({
  label,
  value,
  subtitle,
  tone = 'neutral',
}: {
  label: string
  value: string
  subtitle?: string
  tone?: 'good' | 'warn' | 'bad' | 'neutral'
}) {
  const toneClass =
    tone === 'good'
      ? 'border-[color:var(--bamboo)]/30 bg-[color:var(--bamboo)]/5'
      : tone === 'warn'
      ? 'border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/8'
      : tone === 'bad'
      ? 'border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/5'
      : 'border-border bg-card'
  return (
    <div className={`rounded-lg border ${toneClass} px-2 py-2`}>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="text-sm font-semibold text-foreground mt-0.5">{value}</div>
      {subtitle && (
        <div className="text-[10px] text-muted-foreground mt-0.5">{subtitle}</div>
      )}
    </div>
  )
}

function formatPct(v?: number | null): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(1)}%`
}

function pickToneByRate(
  v: number | null | undefined,
  direction: 'higher_better' | 'lower_better'
): 'good' | 'warn' | 'bad' | 'neutral' {
  if (v == null) return 'neutral'
  if (direction === 'higher_better') {
    if (v >= 0.8) return 'good'
    if (v >= 0.5) return 'warn'
    return 'bad'
  } else {
    if (v <= 0.2) return 'good'
    if (v <= 0.4) return 'warn'
    return 'bad'
  }
}

function pickToneByPct(
  v: number | null | undefined,
  warnThreshold: number,
  badThreshold: number
): 'good' | 'warn' | 'bad' | 'neutral' {
  if (v == null) return 'neutral'
  const abs = Math.abs(v)
  if (abs <= warnThreshold) return 'good'
  if (abs <= badThreshold) return 'warn'
  return 'bad'
}
