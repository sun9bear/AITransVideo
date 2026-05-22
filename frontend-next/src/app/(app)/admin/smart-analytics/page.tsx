"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import Link from "next/link"
import {
  Activity,
  AlertTriangle,
  Download,
  RefreshCw,
  TrendingUp,
} from "lucide-react"
import {
  ResponsiveContainer,
  PieChart as RechartsPieChart,
  Pie,
  Cell,
  Tooltip as ChartTooltip,
  Legend,
  ScatterChart,
  Scatter,
  CartesianGrid,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts"

// ---------------------------------------------------------------------------
// types — mirror gateway/admin_smart_analytics_api.py response shape
// ---------------------------------------------------------------------------

type HandoffRow = {
  reason_code: string
  count: number
  pct: number
  sample_job_ids: string[]
}

type AlignmentRow = {
  job_id: string
  display_name: string
  user_email: string | null
  source_duration_seconds: number | null
  source_duration_minutes: number | null
  total_segments: number | null
  direct_pct: number | null
  dsp_pct: number | null
  rewrite_direct_pct: number | null
  rewrite_dsp_pct: number | null
  forced_dsp_pct: number | null
  short_segment_dsp_pct: number | null
  manual_review_segments: number | null
}

type ReworkByUserRow = {
  user_id: string
  user_email: string | null
  smart_job_count: number
  entered_editing_count: number
  rework_rate: number
  avg_edited_segments: number
}

type EditEventRow = {
  event_type: string
  count: number
  pct: number
}

type TaskTableRow = {
  job_id: string
  user_id: string
  user_email: string | null
  display_name: string
  status: string
  source_duration_minutes: number | null
  total_segments: number | null
  smart_handoff_reason: string | null
  outcome_category: string
  forced_dsp_pct: number | null
  dsp_pct: number | null
  direct_pct: number | null
  manual_review_segments: number | null
  entered_editing: boolean
  edit_event_count: number
  created_at: string | null
  cost_view_url: string
}

type SummaryResponse = {
  window: { days: number; from: string; to: string }
  filters: { status: string; user: string }
  kpi: {
    total_smart_jobs: number
    succeeded: number
    failed: number
    editing: number
    handoff_rate: number
    top_handoff_reason: string | null
    avg_forced_dsp_pct: number
    p90_forced_dsp_pct: number
    rework_rate: number
    avg_edited_segments: number
  }
  handoff_distribution: HandoffRow[]
  alignment_quality: AlignmentRow[]
  rework_by_user: ReworkByUserRow[]
  edit_event_distribution: EditEventRow[]
  task_table: TaskTableRow[]
}

// ---------------------------------------------------------------------------
// constants
// ---------------------------------------------------------------------------

const WINDOW_OPTIONS = [
  { value: 7, label: "近 7 天" },
  { value: 30, label: "近 30 天" },
  { value: 90, label: "近 90 天" },
  { value: 180, label: "近 180 天" },
  { value: 365, label: "近 1 年" },
]

const STATUS_OPTIONS = [
  { value: "all", label: "全部状态" },
  { value: "succeeded", label: "已完成" },
  { value: "failed", label: "已失败" },
  { value: "editing", label: "修改中" },
  { value: "running", label: "运行中" },
]

const TAB_OPTIONS = [
  { value: "handoff", label: "Handoff 分布" },
  { value: "alignment", label: "对齐质量" },
  { value: "rework", label: "用户返工" },
] as const

type TabValue = (typeof TAB_OPTIONS)[number]["value"]

// Synthetix-aligned pie palette
const PIE_COLORS = [
  "#8B5CF6", // primary purple
  "#06B6D4", // secondary cyan
  "#F59E0B", // amber
  "#EF4444", // red
  "#10B981", // green
  "#EC4899", // pink
  "#6366F1", // indigo
  "#84CC16", // lime
]

// ---------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------

export default function AdminSmartAnalyticsPage() {
  const [data, setData] = useState<SummaryResponse | null>(null)
  const [days, setDays] = useState<number>(30)
  const [status, setStatus] = useState<string>("all")
  const [user, setUser] = useState<string>("all")
  const [activeTab, setActiveTab] = useState<TabValue>("handoff")
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(
    async (silent = false) => {
      if (silent) {
        setRefreshing(true)
      } else {
        setLoading(true)
      }
      setError(null)
      try {
        const qs = new URLSearchParams({
          days: String(days),
          status,
          user,
        })
        const res = await fetch(
          `/api/admin/smart-analytics/summary?${qs.toString()}`,
          { credentials: "include" },
        )
        if (res.status === 403 || res.status === 401) {
          setForbidden(true)
          return
        }
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`)
        }
        const payload = (await res.json()) as SummaryResponse
        setData(payload)
        setForbidden(false)
      } catch (err) {
        setError(err instanceof Error ? err.message : "加载智能版数据失败")
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [days, status, user],
  )

  useEffect(() => {
    void load()
  }, [load])

  const userOptions = useMemo(() => {
    const set = new Map<string, string>()
    set.set("all", "全部用户")
    for (const row of data?.rework_by_user ?? []) {
      set.set(row.user_id, row.user_email || row.user_id.slice(0, 8))
    }
    return Array.from(set.entries()).map(([value, label]) => ({ value, label }))
  }, [data])

  const handleCsvExport = useCallback(() => {
    const qs = new URLSearchParams({
      days: String(days),
      status,
      user,
    })
    window.location.href = `/api/admin/smart-analytics/csv?${qs.toString()}`
  }, [days, status, user])

  if (forbidden) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <p className="text-lg text-muted-foreground">仅管理员可访问</p>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Activity className="h-6 w-6 text-primary" aria-hidden="true" />
            <h1 className="text-2xl font-bold text-foreground">智能版监控</h1>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            Smart 模式任务的 handoff 分布、对齐质量、用户返工指标
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            className="h-9 rounded-lg border border-border bg-card px-3 text-sm text-foreground"
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
          >
            {WINDOW_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <select
            className="h-9 rounded-lg border border-border bg-card px-3 text-sm text-foreground"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <select
            className="h-9 rounded-lg border border-border bg-card px-3 text-sm text-foreground"
            value={user}
            onChange={(event) => setUser(event.target.value)}
          >
            {userOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-border bg-card px-3 text-sm text-foreground hover:bg-accent disabled:opacity-60"
            onClick={() => void load(true)}
            disabled={refreshing || loading}
          >
            <RefreshCw
              className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`}
              aria-hidden="true"
            />
            刷新
          </button>
          <button
            type="button"
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-primary/50 bg-primary/10 px-3 text-sm text-primary hover:bg-primary/20 disabled:opacity-60"
            onClick={handleCsvExport}
            disabled={loading || !data}
          >
            <Download className="h-4 w-4" aria-hidden="true" />
            导出 CSV
          </button>
        </div>
      </header>

      {error && (
        <div className="rounded-lg border border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/5 px-4 py-3 text-sm text-[color:var(--cinnabar)]">
          {error}
        </div>
      )}

      {loading ? (
        <div className="rounded-lg border border-border bg-card p-12 text-center text-muted-foreground">
          正在加载智能版监控数据...
        </div>
      ) : data ? (
        <>
          <KpiGrid kpi={data.kpi} window={data.window} />

          <div className="rounded-lg border border-border bg-card">
            <div className="flex flex-wrap items-center gap-1 border-b border-border px-2 pt-2">
              {TAB_OPTIONS.map((tab) => (
                <button
                  key={tab.value}
                  type="button"
                  onClick={() => setActiveTab(tab.value)}
                  className={`rounded-t-md px-4 py-2 text-sm transition-colors ${
                    activeTab === tab.value
                      ? "bg-background font-medium text-foreground border-b-2 border-primary"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>
            <div className="p-4 lg:p-6">
              {activeTab === "handoff" && (
                <HandoffTab rows={data.handoff_distribution} />
              )}
              {activeTab === "alignment" && (
                <AlignmentTab rows={data.alignment_quality} />
              )}
              {activeTab === "rework" && (
                <ReworkTab
                  byUser={data.rework_by_user}
                  events={data.edit_event_distribution}
                />
              )}
            </div>
          </div>

          <FullTaskTable rows={data.task_table} />
        </>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// KPI grid
// ---------------------------------------------------------------------------

function KpiGrid({
  kpi,
  window,
}: {
  kpi: SummaryResponse["kpi"]
  window: SummaryResponse["window"]
}) {
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Kpi
        label="总任务数"
        value={fmtInt(kpi.total_smart_jobs)}
        sub={`成功 ${fmtInt(kpi.succeeded)} · 失败 ${fmtInt(kpi.failed)} · 修改中 ${fmtInt(kpi.editing)}`}
        sub2={`${window.from} → ${window.to}`}
      />
      <Kpi
        label="Handoff 率"
        value={fmtPct(kpi.handoff_rate)}
        sub={kpi.top_handoff_reason ? `top: ${kpi.top_handoff_reason}` : "无 handoff"}
        warn={kpi.handoff_rate > 0.3}
      />
      <Kpi
        label="平均强制 DSP"
        value={fmtPct(kpi.avg_forced_dsp_pct)}
        sub={`P90: ${fmtPct(kpi.p90_forced_dsp_pct)}`}
        warn={kpi.avg_forced_dsp_pct > 0.25}
      />
      <Kpi
        label="用户返工率"
        value={fmtPct(kpi.rework_rate)}
        sub={`平均改 ${kpi.avg_edited_segments.toFixed(1)} 段`}
        warn={kpi.rework_rate > 0.4}
      />
    </div>
  )
}

function Kpi({
  label,
  value,
  sub,
  sub2,
  warn = false,
}: {
  label: string
  value: string
  sub: string
  sub2?: string
  warn?: boolean
}) {
  return (
    <div
      className={`rounded-lg border bg-card p-4 ${
        warn ? "border-[color:var(--ochre)]/40" : "border-border"
      }`}
    >
      <p className="text-xs text-muted-foreground">{label}</p>
      <p
        className={`mt-1 text-2xl font-semibold ${
          warn ? "text-[color:var(--ochre)]" : "text-foreground"
        }`}
      >
        {value}
      </p>
      <p className="mt-1 truncate text-xs text-muted-foreground">{sub}</p>
      {sub2 ? (
        <p className="mt-0.5 truncate text-[11px] text-muted-foreground/80">{sub2}</p>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tab 1 — Handoff distribution
// ---------------------------------------------------------------------------

function HandoffTab({ rows }: { rows: HandoffRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        当前筛选范围内无 handoff / 失败任务（所有任务自动完成）
      </p>
    )
  }

  const pieData = rows.map((row) => ({
    name: row.reason_code,
    value: row.count,
  }))

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
      <div className="rounded-lg border border-border bg-background p-4">
        <h3 className="mb-3 text-sm font-medium text-foreground">类型占比</h3>
        <div className="h-[260px]">
          <ResponsiveContainer width="100%" height="100%">
            <RechartsPieChart>
              <Pie
                data={pieData}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy="50%"
                outerRadius={85}
                label={({ percent }) => `${((percent ?? 0) * 100).toFixed(0)}%`}
              >
                {pieData.map((_, index) => (
                  <Cell
                    key={`cell-${index}`}
                    fill={PIE_COLORS[index % PIE_COLORS.length]}
                  />
                ))}
              </Pie>
              <ChartTooltip
                contentStyle={{
                  background: "rgb(var(--card-rgb, 30 30 30))",
                  border: "1px solid rgb(var(--border-rgb, 60 60 60))",
                  fontSize: "12px",
                }}
                formatter={(value, name) => [`${value} 次`, String(name)]}
              />
              <Legend
                wrapperStyle={{ fontSize: "11px" }}
                formatter={(value: string) => truncate(value, 28)}
              />
            </RechartsPieChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border border-border bg-background">
        <table className="w-full text-sm">
          <thead className="border-b border-border bg-muted/30 text-xs text-muted-foreground">
            <tr>
              <Th>Reason code</Th>
              <Th right>次数</Th>
              <Th right>占比</Th>
              <Th>示例任务</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.map((row) => (
              <tr key={row.reason_code} className="hover:bg-muted/20">
                <Td>
                  <span className="font-mono text-xs text-foreground">
                    {row.reason_code}
                  </span>
                </Td>
                <Td right>{fmtInt(row.count)}</Td>
                <Td right>{fmtPct(row.pct)}</Td>
                <Td>
                  <div className="flex flex-wrap gap-1.5">
                    {row.sample_job_ids.slice(0, 2).map((jid) => (
                      <Link
                        key={jid}
                        href={`/admin/jobs/${jid}/cost`}
                        className="rounded-md bg-primary/10 px-2 py-0.5 font-mono text-[11px] text-primary hover:bg-primary/20"
                      >
                        {jid.slice(0, 14)}…
                      </Link>
                    ))}
                  </div>
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tab 2 — Alignment quality table + scatter plot
// ---------------------------------------------------------------------------

function AlignmentTab({ rows }: { rows: AlignmentRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        当前筛选范围内无有效对齐报告
      </p>
    )
  }

  const scatterData = rows
    .filter(
      (row) =>
        row.source_duration_minutes != null && row.forced_dsp_pct != null,
    )
    .map((row) => ({
      x: row.source_duration_minutes!,
      y: (row.forced_dsp_pct ?? 0) * 100,
      job_id: row.job_id,
      display_name: row.display_name,
    }))

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-border bg-background p-4">
        <h3 className="mb-3 text-sm font-medium text-foreground">
          长度 vs 强制 DSP 散点（检验「视频越长越容易强制对齐」假设）
        </h3>
        <div className="h-[260px]">
          <ResponsiveContainer width="100%" height="100%">
            <ScatterChart margin={{ top: 10, right: 20, bottom: 30, left: 10 }}>
              <CartesianGrid stroke="rgb(120 120 120 / 0.2)" />
              <XAxis
                dataKey="x"
                name="时长"
                unit=" 分钟"
                stroke="rgb(150 150 150)"
                tick={{ fontSize: 11 }}
                label={{ value: "视频时长（分钟）", position: "insideBottom", offset: -20, fontSize: 11 }}
              />
              <YAxis
                dataKey="y"
                name="强制 DSP"
                unit="%"
                stroke="rgb(150 150 150)"
                tick={{ fontSize: 11 }}
              />
              <ZAxis range={[60, 60]} />
              <ChartTooltip
                cursor={{ strokeDasharray: "3 3" }}
                contentStyle={{
                  background: "rgb(var(--card-rgb, 30 30 30))",
                  border: "1px solid rgb(var(--border-rgb, 60 60 60))",
                  fontSize: "12px",
                }}
                formatter={(_value, _name, props) => {
                  const point = props.payload as {
                    x: number
                    y: number
                    job_id: string
                    display_name: string
                  }
                  return [
                    `${point.x.toFixed(1)} min · ${point.y.toFixed(1)}%`,
                    truncate(point.display_name || point.job_id, 30),
                  ]
                }}
              />
              <Scatter data={scatterData} fill="#8B5CF6" />
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border border-border bg-background">
        <table className="w-full min-w-[900px] text-sm">
          <thead className="border-b border-border bg-muted/30 text-xs text-muted-foreground">
            <tr>
              <Th>任务</Th>
              <Th right>时长</Th>
              <Th right>段数</Th>
              <Th right>直接</Th>
              <Th right>DSP</Th>
              <Th right>重写直接</Th>
              <Th right>强制 DSP</Th>
              <Th right>短段保护</Th>
              <Th right>复查段</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.map((row) => (
              <tr key={row.job_id} className="hover:bg-muted/20">
                <Td>
                  <Link
                    href={`/admin/jobs/${row.job_id}/cost`}
                    className="block max-w-[280px]"
                  >
                    <span className="block truncate font-medium text-foreground hover:text-primary">
                      {row.display_name || "(无标题)"}
                    </span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {row.job_id}
                    </span>
                  </Link>
                </Td>
                <Td right>{fmtMinutes(row.source_duration_minutes)}</Td>
                <Td right>{fmtInt(row.total_segments)}</Td>
                <Td right>{fmtPct(row.direct_pct)}</Td>
                <Td right>{fmtPct(row.dsp_pct)}</Td>
                <Td right>{fmtPct(row.rewrite_direct_pct)}</Td>
                <Td right>
                  <span className={forcedDspClass(row.forced_dsp_pct)}>
                    {fmtPct(row.forced_dsp_pct)}
                  </span>
                </Td>
                <Td right>{fmtPct(row.short_segment_dsp_pct)}</Td>
                <Td right>
                  {row.manual_review_segments == null ? (
                    "-"
                  ) : (
                    <span
                      className={
                        row.manual_review_segments > 0
                          ? "text-[color:var(--ochre)]"
                          : "text-muted-foreground"
                      }
                    >
                      {fmtInt(row.manual_review_segments)}
                    </span>
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tab 3 — Rework by user + edit event distribution
// ---------------------------------------------------------------------------

function ReworkTab({
  byUser,
  events,
}: {
  byUser: ReworkByUserRow[]
  events: EditEventRow[]
}) {
  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
      <div className="overflow-x-auto rounded-lg border border-border bg-background">
        <div className="border-b border-border bg-muted/30 px-3 py-2 text-xs font-medium text-foreground">
          按用户聚合
        </div>
        <table className="w-full text-sm">
          <thead className="border-b border-border bg-muted/10 text-xs text-muted-foreground">
            <tr>
              <Th>用户</Th>
              <Th right>Smart 任务</Th>
              <Th right>进编辑</Th>
              <Th right>返工率</Th>
              <Th right>平均改段</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {byUser.length === 0 ? (
              <EmptyRow colSpan={5} text="无用户数据" />
            ) : (
              byUser.map((row) => (
                <tr key={row.user_id} className="hover:bg-muted/20">
                  <Td>
                    <span className="block truncate font-medium text-foreground">
                      {row.user_email || row.user_id.slice(0, 8)}
                    </span>
                    <span className="block truncate text-[11px] text-muted-foreground">
                      {row.user_id}
                    </span>
                  </Td>
                  <Td right>{fmtInt(row.smart_job_count)}</Td>
                  <Td right>{fmtInt(row.entered_editing_count)}</Td>
                  <Td right>
                    <span className={reworkRateClass(row.rework_rate)}>
                      {fmtPct(row.rework_rate)}
                    </span>
                  </Td>
                  <Td right>{row.avg_edited_segments.toFixed(1)}</Td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="overflow-x-auto rounded-lg border border-border bg-background">
        <div className="border-b border-border bg-muted/30 px-3 py-2 text-xs font-medium text-foreground">
          修改事件类型分布
        </div>
        <table className="w-full text-sm">
          <thead className="border-b border-border bg-muted/10 text-xs text-muted-foreground">
            <tr>
              <Th>事件类型</Th>
              <Th right>次数</Th>
              <Th right>占比</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {events.length === 0 ? (
              <EmptyRow colSpan={3} text="无修改事件" />
            ) : (
              events.map((row) => (
                <tr key={row.event_type} className="hover:bg-muted/20">
                  <Td>
                    <span className="font-mono text-xs">{row.event_type}</span>
                  </Td>
                  <Td right>{fmtInt(row.count)}</Td>
                  <Td right>{fmtPct(row.pct)}</Td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Full task table (bottom of page, always visible)
// ---------------------------------------------------------------------------

function FullTaskTable({ rows }: { rows: TaskTableRow[] }) {
  return (
    <div>
      <div className="mb-2 flex items-center gap-2">
        <TrendingUp className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        <h2 className="text-sm font-medium text-foreground">
          完整任务表（点击行进单任务成本页）
        </h2>
        <span className="text-xs text-muted-foreground">
          {rows.length} 条 · 按创建时间倒序
        </span>
      </div>
      <div className="overflow-x-auto rounded-lg border border-border bg-card">
        <table className="w-full min-w-[1200px] text-sm">
          <thead className="border-b border-border bg-muted/30 text-xs text-muted-foreground">
            <tr>
              <Th>任务</Th>
              <Th>用户</Th>
              <Th>状态</Th>
              <Th right>时长(min)</Th>
              <Th right>强制DSP</Th>
              <Th>Handoff</Th>
              <Th right>进编辑</Th>
              <Th right>改段</Th>
              <Th>创建</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.length === 0 ? (
              <EmptyRow colSpan={9} text="当前筛选范围内无任务" />
            ) : (
              rows.map((row) => (
                <tr key={row.job_id} className="hover:bg-muted/20">
                  <Td>
                    <Link
                      href={row.cost_view_url}
                      className="block max-w-[260px]"
                    >
                      <span className="block truncate font-medium text-foreground hover:text-primary">
                        {row.display_name || "(无标题)"}
                      </span>
                      <span className="block truncate text-xs text-muted-foreground">
                        {row.job_id}
                      </span>
                    </Link>
                  </Td>
                  <Td>
                    <span className="block max-w-[180px] truncate text-xs">
                      {row.user_email || row.user_id.slice(0, 8)}
                    </span>
                  </Td>
                  <Td>
                    <StatusBadge status={row.status} outcome={row.outcome_category} />
                  </Td>
                  <Td right>{fmtMinutes(row.source_duration_minutes)}</Td>
                  <Td right>
                    <span className={forcedDspClass(row.forced_dsp_pct)}>
                      {fmtPct(row.forced_dsp_pct)}
                    </span>
                  </Td>
                  <Td>
                    {row.smart_handoff_reason ? (
                      <span className="rounded-md bg-[color:var(--ochre)]/10 px-2 py-0.5 font-mono text-[11px] text-[color:var(--ochre)]">
                        {truncate(row.smart_handoff_reason, 26)}
                      </span>
                    ) : (
                      <span className="text-xs text-muted-foreground">-</span>
                    )}
                  </Td>
                  <Td right>
                    {row.entered_editing ? (
                      <span className="inline-flex items-center gap-1 text-[color:var(--cinnabar)]">
                        <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                        是
                      </span>
                    ) : (
                      <span className="text-xs text-muted-foreground">否</span>
                    )}
                  </Td>
                  <Td right>{fmtInt(row.edit_event_count)}</Td>
                  <Td>
                    <span className="text-xs text-muted-foreground">
                      {fmtDate(row.created_at)}
                    </span>
                  </Td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function StatusBadge({
  status,
  outcome,
}: {
  status: string
  outcome: string
}) {
  let color = "bg-muted text-muted-foreground"
  if (status === "succeeded") {
    color = outcome.includes("with_handoff")
      ? "bg-[color:var(--ochre)]/10 text-[color:var(--ochre)]"
      : "bg-[color:var(--bamboo)]/10 text-[color:var(--bamboo)]"
  } else if (status === "failed") {
    color = "bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)]"
  } else if (status === "editing") {
    color = "bg-primary/10 text-primary"
  } else if (status === "running") {
    color = "bg-secondary/10 text-secondary"
  }
  return (
    <span className={`inline-flex rounded-md px-2 py-0.5 text-[11px] ${color}`}>
      {status}
    </span>
  )
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function Th({
  children,
  right = false,
}: {
  children: React.ReactNode
  right?: boolean
}) {
  return (
    <th
      className={`px-3 py-2 font-medium ${right ? "text-right" : "text-left"}`}
    >
      {children}
    </th>
  )
}

function Td({
  children,
  right = false,
}: {
  children: React.ReactNode
  right?: boolean
}) {
  return (
    <td
      className={`px-3 py-2 align-middle text-muted-foreground ${
        right ? "text-right tabular-nums" : ""
      }`}
    >
      {children}
    </td>
  )
}

function EmptyRow({ colSpan, text }: { colSpan: number; text: string }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-3 py-8 text-center text-sm text-muted-foreground">
        {text}
      </td>
    </tr>
  )
}

function fmtInt(value: number | null | undefined) {
  if (value == null) return "-"
  return new Intl.NumberFormat("zh-CN").format(value)
}

function fmtPct(value: number | null | undefined) {
  if (value == null || isNaN(value)) return "-"
  return `${(value * 100).toFixed(1)}%`
}

function fmtMinutes(value: number | null | undefined) {
  if (value == null) return "-"
  return value.toFixed(1)
}

function fmtDate(value: string | null) {
  if (!value) return "-"
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(value))
  } catch {
    return value
  }
}

function truncate(s: string, max: number) {
  if (s.length <= max) return s
  return `${s.slice(0, max - 1)}…`
}

function forcedDspClass(value: number | null | undefined) {
  if (value == null) return "text-muted-foreground"
  if (value > 0.3) return "font-medium text-[color:var(--cinnabar)]"
  if (value > 0.15) return "font-medium text-[color:var(--ochre)]"
  return "text-muted-foreground"
}

function reworkRateClass(value: number) {
  if (value > 0.5) return "font-medium text-[color:var(--cinnabar)]"
  if (value > 0.25) return "font-medium text-[color:var(--ochre)]"
  return "text-muted-foreground"
}
