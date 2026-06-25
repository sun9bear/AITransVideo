"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import type { ReactNode } from "react"
import Link from "next/link"
import {
  AlertTriangle,
  Download,
  FileSearch,
  RefreshCw,
  SlidersHorizontal,
} from "lucide-react"

type ReportMetric = {
  exists: boolean
  issue_count?: number
  issue_rate?: number | null
  checked_segments?: number
  row_count?: number
  changed_count?: number
  uncertain_count?: number
  manifest_count?: number
  candidate_count?: number
  hard_reject_candidate_count?: number
  hard_reject_rate?: number | null
  selected_hard_reject_manifest_count?: number
}

type JobReportRow = {
  job_id: string
  user_email: string | null
  display_name: string
  status: string
  service_mode: string
  created_at: string | null
  reports: {
    translation_quality: ReportMetric
    subtitle_width: ReportMetric
    speaker_evidence: ReportMetric
    voice_sample_scoring: ReportMetric
  }
  cost_view_url: string
}

type AnalysisResponse = {
  kpi: {
    total_jobs: number
    jobs_with_any_report: number
    translation_report_jobs: number
    translation_issue_jobs: number
    translation_issue_count: number
    translation_checked_segments: number
    translation_issue_rate: number | null
    voice_sample_report_jobs: number
    voice_manifest_count: number
    voice_candidate_count: number
    voice_hard_reject_candidate_count: number
    voice_hard_reject_rate: number | null
    voice_selected_hard_reject_manifest_count: number
    subtitle_width_issue_count: number
    speaker_changed_rows: number
    speaker_uncertain_rows: number
  }
  recommendations: Record<string, { status: string; rationale: string }>
  reason_counts: Record<string, Record<string, number>>
  jobs: JobReportRow[]
}

type FlagRow = {
  key: string
  label: string
  category: "shadow" | "behavior"
  implemented: boolean
  risk: string
  env: string
  admin_value: boolean | null
  env_value: boolean | null
  effective: boolean
  effective_source: string
}

type FlagsResponse = {
  flags: FlagRow[]
}

const WINDOW_OPTIONS = [7, 30, 90, 180, 365]
const STATUS_OPTIONS = ["all", "succeeded", "failed", "editing", "running"]
const SERVICE_MODE_OPTIONS = ["all", "smart", "studio", "express"]

export default function AdminReportAnalysisPage() {
  const [days, setDays] = useState(30)
  const [status, setStatus] = useState("all")
  const [serviceMode, setServiceMode] = useState("all")
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null)
  const [flags, setFlags] = useState<FlagsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [savingFlag, setSavingFlag] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const query = useMemo(() => {
    return new URLSearchParams({
      days: String(days),
      status,
      service_mode: serviceMode,
      user: "all",
    }).toString()
  }, [days, status, serviceMode])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [summaryRes, flagsRes] = await Promise.all([
        fetch(`/api/admin/smart-analytics/job-reports-summary?${query}`, {
          credentials: "include",
        }),
        fetch("/api/admin/smart-analytics/phase1b-flags", {
          credentials: "include",
        }),
      ])
      if (!summaryRes.ok) {
        throw new Error(`summary HTTP ${summaryRes.status}`)
      }
      if (!flagsRes.ok) {
        throw new Error(`flags HTTP ${flagsRes.status}`)
      }
      setAnalysis((await summaryRes.json()) as AnalysisResponse)
      setFlags((await flagsRes.json()) as FlagsResponse)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载报告分析失败")
    } finally {
      setLoading(false)
    }
  }, [query])

  useEffect(() => {
    void load()
  }, [load])

  const toggleFlag = useCallback(
    async (flag: FlagRow) => {
      setSavingFlag(flag.key)
      setError(null)
      try {
        const res = await fetch("/api/admin/smart-analytics/phase1b-flags", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ flags: { [flag.key]: !flag.effective } }),
        })
        if (!res.ok) {
          throw new Error(`flag HTTP ${res.status}`)
        }
        setFlags((await res.json()) as FlagsResponse)
      } catch (err) {
        setError(err instanceof Error ? err.message : "保存开关失败")
      } finally {
        setSavingFlag(null)
      }
    },
    [],
  )

  const exportCsv = useCallback(() => {
    window.location.href = `/api/admin/smart-analytics/job-reports-csv?${query}`
  }, [query])

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <FileSearch className="h-6 w-6 text-primary" aria-hidden="true" />
            <h1 className="text-2xl font-bold text-foreground">报告分析</h1>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            汇总 translation / speaker / subtitle / voice sample reports，用于灰度决策。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Select value={days} onChange={(value) => setDays(Number(value))}>
            {WINDOW_OPTIONS.map((value) => (
              <option key={value} value={value}>
                最近 {value} 天
              </option>
            ))}
          </Select>
          <Select value={status} onChange={setStatus}>
            {STATUS_OPTIONS.map((value) => (
              <option key={value} value={value}>
                {value === "all" ? "全部状态" : value}
              </option>
            ))}
          </Select>
          <Select value={serviceMode} onChange={setServiceMode}>
            {SERVICE_MODE_OPTIONS.map((value) => (
              <option key={value} value={value}>
                {value === "all" ? "全部模式" : value}
              </option>
            ))}
          </Select>
          <button
            type="button"
            onClick={() => void load()}
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-border bg-card px-3 text-sm text-foreground hover:bg-accent"
            disabled={loading}
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            刷新
          </button>
          <button
            type="button"
            onClick={exportCsv}
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-primary/50 bg-primary/10 px-3 text-sm text-primary hover:bg-primary/20"
            disabled={!analysis}
          >
            <Download className="h-4 w-4" />
            导出 CSV
          </button>
        </div>
      </header>

      {error ? (
        <div className="rounded-lg border border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/5 px-4 py-3 text-sm text-[color:var(--cinnabar)]">
          {error}
        </div>
      ) : null}

      {loading && !analysis ? (
        <div className="rounded-lg border border-border bg-card p-10 text-center text-sm text-muted-foreground">
          正在加载报告分析...
        </div>
      ) : null}

      {analysis ? (
        <>
          <KpiGrid data={analysis} />
          <RecommendationBand recommendations={analysis.recommendations} />
          <FlagPanel flags={flags?.flags ?? []} savingFlag={savingFlag} onToggle={toggleFlag} />
          <ReasonTables reasonCounts={analysis.reason_counts} />
          <JobReportTable rows={analysis.jobs} />
        </>
      ) : null}
    </div>
  )
}

function KpiGrid({ data }: { data: AnalysisResponse }) {
  const kpi = data.kpi
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Kpi label="报告任务" value={`${kpi.jobs_with_any_report}/${kpi.total_jobs}`} sub="有任一 reports sidecar" />
      <Kpi
        label="翻译疑似脚本错误"
        value={fmtPct(kpi.translation_issue_rate)}
        sub={`${kpi.translation_issue_count} / ${kpi.translation_checked_segments} 段`}
        warn={(kpi.translation_issue_rate ?? 0) > 0.08}
      />
      <Kpi
        label="音色候选 hard reject"
        value={fmtPct(kpi.voice_hard_reject_rate)}
        sub={`${kpi.voice_hard_reject_candidate_count} / ${kpi.voice_candidate_count} 候选`}
        warn={(kpi.voice_hard_reject_rate ?? 0) > 0.4}
      />
      <Kpi
        label="字幕/说话人提醒"
        value={`${kpi.subtitle_width_issue_count}/${kpi.speaker_changed_rows + kpi.speaker_uncertain_rows}`}
        sub="超宽字幕 / speaker 变更+不确定"
        warn={kpi.subtitle_width_issue_count > 0 || kpi.speaker_uncertain_rows > 0}
      />
    </div>
  )
}

function RecommendationBand({
  recommendations,
}: {
  recommendations: AnalysisResponse["recommendations"]
}) {
  const entries = Object.entries(recommendations)
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      {entries.map(([key, rec]) => (
        <div key={key} className="rounded-lg border border-border bg-card p-4">
          <div className="flex items-center justify-between gap-3">
            <h2 className="text-sm font-medium text-foreground">{flagTitle(key)}</h2>
            <span className="rounded-md bg-muted px-2 py-1 text-xs font-medium text-muted-foreground">
              {rec.status}
            </span>
          </div>
          <p className="mt-2 text-sm text-muted-foreground">{rec.rationale}</p>
        </div>
      ))}
    </div>
  )
}

function FlagPanel({
  flags,
  savingFlag,
  onToggle,
}: {
  flags: FlagRow[]
  savingFlag: string | null
  onToggle: (flag: FlagRow) => void
}) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <SlidersHorizontal className="h-4 w-4 text-muted-foreground" />
        <h2 className="text-sm font-medium text-foreground">Phase 1b 开关</h2>
      </div>
      <div className="divide-y divide-border">
        {flags.map((flag) => (
          <div key={flag.key} className="flex flex-col gap-3 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-foreground">{flagLabel(flag.key)}</span>
                <Badge tone={flag.category === "shadow" ? "blue" : "amber"}>
                  {flag.category === "shadow" ? "shadow" : "behavior"}
                </Badge>
                {!flag.implemented ? <Badge tone="red">待行为代码接入</Badge> : null}
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                {flag.env} · 当前来源 {flag.effective_source} · 风险 {flag.risk}
              </p>
            </div>
            <div className="flex items-center gap-3">
              <span
                className={`text-sm font-medium ${
                  flag.effective ? "text-primary" : "text-muted-foreground"
                }`}
              >
                {savingFlag === flag.key ? "保存中" : flag.effective ? "已开启" : "已关闭"}
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={flag.effective}
                aria-label={`${flagLabel(flag.key)}${flag.effective ? "已开启" : "已关闭"}`}
                onClick={() => onToggle(flag)}
                disabled={savingFlag === flag.key}
                className={`relative inline-flex h-8 w-14 shrink-0 items-center rounded-full border transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:cursor-wait disabled:opacity-60 ${
                  flag.effective
                    ? "border-primary bg-primary"
                    : "border-border bg-muted"
                }`}
              >
                <span
                  className={`inline-block h-6 w-6 rounded-full bg-background shadow-sm transition-transform duration-200 ${
                    flag.effective ? "translate-x-7" : "translate-x-1"
                  }`}
                />
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

function ReasonTables({ reasonCounts }: { reasonCounts: AnalysisResponse["reason_counts"] }) {
  return (
    <div className="grid gap-3 lg:grid-cols-4">
      {Object.entries(reasonCounts).map(([key, counts]) => (
        <div key={key} className="rounded-lg border border-border bg-card">
          <div className="border-b border-border px-3 py-2 text-xs font-medium text-foreground">
            {reasonTitle(key)}
          </div>
          <div className="max-h-52 overflow-auto">
            {Object.entries(counts).length === 0 ? (
              <p className="px-3 py-4 text-sm text-muted-foreground">无数据</p>
            ) : (
              <table className="w-full text-sm">
                <tbody className="divide-y divide-border">
                  {Object.entries(counts).slice(0, 8).map(([reason, count]) => (
                    <tr key={reason}>
                      <td className="px-3 py-2 font-mono text-xs text-foreground">{reason}</td>
                      <td className="px-3 py-2 text-right text-muted-foreground">{count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

function JobReportTable({ rows }: { rows: JobReportRow[] }) {
  return (
    <section>
      <div className="mb-2 flex items-center gap-2">
        <AlertTriangle className="h-4 w-4 text-muted-foreground" />
        <h2 className="text-sm font-medium text-foreground">任务报告明细</h2>
        <span className="text-xs text-muted-foreground">{rows.length} 条</span>
      </div>
      <div className="overflow-x-auto rounded-lg border border-border bg-card">
        <table className="w-full min-w-[1100px] text-sm">
          <thead className="border-b border-border bg-muted/30 text-xs text-muted-foreground">
            <tr>
              <Th>任务</Th>
              <Th>模式/状态</Th>
              <Th right>翻译问题</Th>
              <Th right>字幕超宽</Th>
              <Th right>Speaker</Th>
              <Th right>Voice scoring</Th>
              <Th>创建时间</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-3 py-8 text-center text-muted-foreground">
                  当前筛选范围内没有任务
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr key={row.job_id} className="hover:bg-muted/20">
                  <Td>
                    <Link href={row.cost_view_url || "/admin/jobs"} className="block max-w-[320px]">
                      <span className="block truncate font-medium text-foreground hover:text-primary">
                        {row.display_name || row.job_id}
                      </span>
                      <span className="block truncate font-mono text-xs text-muted-foreground">
                        {row.job_id}
                      </span>
                    </Link>
                  </Td>
                  <Td>
                    <span className="block text-foreground">{row.service_mode || "-"}</span>
                    <span className="text-xs text-muted-foreground">{row.status || "-"}</span>
                  </Td>
                  <Td right>
                    {fmtCountRate(
                      row.reports.translation_quality.issue_count,
                      row.reports.translation_quality.issue_rate,
                    )}
                  </Td>
                  <Td right>{fmtInt(row.reports.subtitle_width.issue_count)}</Td>
                  <Td right>
                    {fmtInt(row.reports.speaker_evidence.changed_count)} / {fmtInt(row.reports.speaker_evidence.uncertain_count)}
                  </Td>
                  <Td right>
                    {fmtInt(row.reports.voice_sample_scoring.hard_reject_candidate_count)} / {fmtInt(row.reports.voice_sample_scoring.candidate_count)}
                  </Td>
                  <Td>{row.created_at ? new Date(row.created_at).toLocaleString("zh-CN") : "-"}</Td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function Kpi({
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
    <div className={`rounded-lg border bg-card p-4 ${warn ? "border-[color:var(--ochre)]/40" : "border-border"}`}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${warn ? "text-[color:var(--ochre)]" : "text-foreground"}`}>
        {value}
      </p>
      <p className="mt-1 text-xs text-muted-foreground">{sub}</p>
    </div>
  )
}

function Select({
  value,
  onChange,
  children,
}: {
  value: string | number
  onChange: (value: string) => void
  children: ReactNode
}) {
  return (
    <select
      className="h-9 rounded-lg border border-border bg-card px-3 text-sm text-foreground"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      {children}
    </select>
  )
}

function Badge({ children, tone }: { children: ReactNode; tone: "blue" | "amber" | "red" }) {
  const cls =
    tone === "blue"
      ? "bg-primary/10 text-primary"
      : tone === "amber"
        ? "bg-[color:var(--ochre)]/10 text-[color:var(--ochre)]"
        : "bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)]"
  return <span className={`rounded-md px-2 py-0.5 text-[11px] font-medium ${cls}`}>{children}</span>
}

function Th({ children, right = false }: { children: ReactNode; right?: boolean }) {
  return <th className={`px-3 py-2 font-medium ${right ? "text-right" : "text-left"}`}>{children}</th>
}

function Td({ children, right = false }: { children: ReactNode; right?: boolean }) {
  return <td className={`px-3 py-2 align-top ${right ? "text-right" : "text-left"}`}>{children}</td>
}

function fmtPct(value: number | null | undefined) {
  if (value == null) return "-"
  return `${(value * 100).toFixed(1)}%`
}

function fmtInt(value: number | null | undefined) {
  if (value == null) return "0"
  return Math.round(value).toLocaleString("zh-CN")
}

function fmtCountRate(count: number | undefined, rate: number | null | undefined) {
  return `${fmtInt(count)}${rate == null ? "" : ` (${fmtPct(rate)})`}`
}

function flagTitle(key: string) {
  if (key === "translation_script_gate") return "翻译 script gate"
  if (key === "voice_sample_scoring") return "音色样本评分"
  return key
}

function flagLabel(key: string) {
  const labels: Record<string, string> = {
    translation_script_gate_shadow: "翻译 script gate shadow",
    voice_sample_scoring_shadow: "音色样本评分 shadow",
    translation_script_gate: "翻译 script gate 行为",
    voice_sample_scoring: "音色样本评分行为",
    audio_tail_trim: "TTS 尾部静音裁剪",
    whisper_quality_gate: "Whisper 对齐质量门",
  }
  return labels[key] ?? key
}

function reasonTitle(key: string) {
  const labels: Record<string, string> = {
    translation: "翻译原因",
    speaker_decisions: "Speaker 决策",
    voice_hard_reject_reasons: "Voice hard reject",
    voice_warnings: "Voice warnings",
  }
  return labels[key] ?? key
}
