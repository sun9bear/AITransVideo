"use client"

import { useCallback, useEffect, useState } from "react"
import {
  Activity,
  BarChart3,
  RefreshCw,
  ShieldAlert,
  TrendingUp,
  Users,
  Wallet,
} from "lucide-react"

type RankedRow = {
  key: string
  label: string
  count: number
  share: number
}

type ConversionRow = {
  key: string
  label: string
  from_count: number
  to_count: number
  rate: number | null
}

type BehaviorDailyRow = {
  date: string
  human_page_visitors: number
  page_views: number
  registrations: number
  login_users: number
  sms_sent: number
  jobs_created: number
  paid_orders: number
}

type RecentUser = {
  identity: string
  plan_code: string
  role: string
  created_at: string | null
}

type RecentJob = {
  job_id: string
  title: string
  status: string
  service_mode: string
  source_type: string
  owner: string
  created_at: string | null
}

type RecentPaidOrder = {
  provider: string
  plan_code: string
  billing_period: string
  amount_cny: number
  paid_at: string | null
  user: string
}

type BehaviorSummary = {
  available: boolean
  error: string | null
  totals: {
    registrations: number
    phone_verified_users: number
    login_sessions: number
    logged_in_users: number
    active_session_users: number
    sms_sent: number
    sms_consumed: number
    sms_expired_unused: number
    jobs_created: number
    job_users: number
    new_user_job_users: number
    jobs_succeeded: number
    jobs_failed: number
    payment_orders_created: number
    payment_orders_paid: number
    paid_order_users: number
    paid_amount_cny: number
    paid_invoices?: number
    active_subscriptions: number
    new_subscriptions: number
  }
  conversion: ConversionRow[]
  daily: BehaviorDailyRow[]
  plans: RankedRow[]
  job_statuses: RankedRow[]
  job_modes: RankedRow[]
  job_sources: RankedRow[]
  payment_providers: RankedRow[]
  payment_plans: RankedRow[]
  recent_users: RecentUser[]
  recent_jobs: RecentJob[]
  recent_paid_orders: RecentPaidOrder[]
  methodology: string[]
}

type TrafficSummary = {
  cached: boolean
  source: {
    generated_at_utc: string
  }
  totals: {
    estimated_human_page_visitors_ip_ua: number
    page_views: number
  }
  behavior: BehaviorSummary
}

const WINDOW_OPTIONS = [1, 7, 14, 30]
const integerFormat = new Intl.NumberFormat("zh-CN")

function fmtInt(value: number | null | undefined) {
  return integerFormat.format(value ?? 0)
}

function fmtPct(value: number | null | undefined) {
  if (value == null) return "-"
  const n = value ?? 0
  return `${(n * 100).toFixed(n >= 0.1 ? 1 : 2)}%`
}

function fmtMoney(value: number | null | undefined) {
  return `¥${(value ?? 0).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`
}

function fmtTime(iso: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(iso))
  } catch {
    return iso
  }
}

function StatCard({
  label,
  value,
  sub,
  icon,
  tone = "default",
}: {
  label: string
  value: string
  sub?: string
  icon: React.ReactNode
  tone?: "default" | "warn" | "danger"
}) {
  const toneClass =
    tone === "danger"
      ? "border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/5"
      : tone === "warn"
        ? "border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/5"
        : "border-border bg-card"
  return (
    <div className={`rounded-lg border p-4 ${toneClass}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="text-muted-foreground">{icon}</div>
      </div>
      <div className="mt-2 text-2xl font-bold tabular-nums text-foreground">{value}</div>
      {sub && <div className="mt-1 text-xs text-muted-foreground">{sub}</div>}
    </div>
  )
}

function Panel({
  title,
  children,
  className = "",
}: {
  title: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <section className={`rounded-lg border border-border bg-card p-4 ${className}`}>
      <h2 className="text-sm font-semibold text-foreground">{title}</h2>
      <div className="mt-3">{children}</div>
    </section>
  )
}

function RankList({
  rows,
  empty = "暂无数据",
}: {
  rows: RankedRow[]
  empty?: string
}) {
  if (!rows.length) {
    return <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">{empty}</div>
  }
  const max = Math.max(...rows.map((row) => row.count), 1)
  return (
    <div className="space-y-2">
      {rows.map((row) => (
        <div key={row.key} className="space-y-1">
          <div className="flex items-center justify-between gap-3 text-sm">
            <span className="min-w-0 truncate text-foreground">{row.label || row.key}</span>
            <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
              {fmtInt(row.count)} · {fmtPct(row.share)}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-primary/70"
              style={{ width: `${Math.max(4, (row.count / max) * 100)}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

function ConversionFunnel({ rows }: { rows: ConversionRow[] }) {
  if (!rows.length) {
    return <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">暂无转化数据</div>
  }
  return (
    <div className="space-y-3">
      {rows.map((row) => (
        <div key={row.key} className="space-y-1.5">
          <div className="flex items-center justify-between gap-3 text-sm">
            <span className="text-foreground">{row.label}</span>
            <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
              {fmtInt(row.to_count)} / {fmtInt(row.from_count)} · {fmtPct(row.rate)}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-[color:var(--cinnabar)]/80"
              style={{ width: `${Math.max(3, (row.rate ?? 0) * 100)}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

function BehaviorDailyTrend({ rows }: { rows: BehaviorDailyRow[] }) {
  if (!rows.length) {
    return <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">暂无行为趋势</div>
  }
  const max = Math.max(...rows.map((row) => Math.max(row.human_page_visitors, row.registrations, row.jobs_created)), 1)
  return (
    <div className="space-y-3">
      {rows.map((row) => (
        <div key={row.date} className="grid grid-cols-[76px_1fr_92px] items-center gap-3 text-xs">
          <span className="text-muted-foreground">{row.date.slice(5)}</span>
          <div className="grid gap-1">
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-[color:var(--bamboo)]/70" style={{ width: `${Math.max(3, (row.human_page_visitors / max) * 100)}%` }} />
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-primary/70" style={{ width: `${Math.max(3, (row.registrations / max) * 100)}%` }} />
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-[color:var(--cinnabar)]/70" style={{ width: `${Math.max(3, (row.jobs_created / max) * 100)}%` }} />
            </div>
          </div>
          <span className="text-right tabular-nums text-muted-foreground">
            {fmtInt(row.human_page_visitors)} / {fmtInt(row.registrations)} / {fmtInt(row.jobs_created)}
          </span>
        </div>
      ))}
      <div className="flex flex-wrap gap-3 pt-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-[color:var(--bamboo)]/70" />访客</span>
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-primary/70" />注册</span>
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-[color:var(--cinnabar)]/70" />任务</span>
      </div>
    </div>
  )
}

function SimpleTable({
  headers,
  children,
}: {
  headers: string[]
  children: React.ReactNode
}) {
  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="min-w-full text-sm">
        <thead className="bg-muted/40">
          <tr>
            {headers.map((header) => (
              <th key={header} className="whitespace-nowrap px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  )
}

export default function AdminConversionsPage() {
  const [windowDays, setWindowDays] = useState(7)
  const [data, setData] = useState<TrafficSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadConversions = useCallback(async (silent = false, force = false) => {
    if (silent) {
      setRefreshing(true)
    } else {
      setLoading(true)
    }
    setError(null)
    try {
      const qs = new URLSearchParams({
        window: String(windowDays),
        limit: "20",
        include_behavior: "true",
      })
      if (force) qs.set("force", "true")
      const resp = await fetch(`/api/admin/traffic/summary?${qs.toString()}`, {
        credentials: "include",
      })
      if (resp.status === 401 || resp.status === 403) {
        setForbidden(true)
        return
      }
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}))
        const detail = (body as { detail?: string }).detail
        throw new Error(detail || `HTTP ${resp.status}`)
      }
      setData((await resp.json()) as TrafficSummary)
      setForbidden(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载转化监控数据失败")
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [windowDays])

  useEffect(() => {
    void loadConversions()
  }, [loadConversions])

  const behavior = data?.behavior

  if (forbidden) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center px-4">
        <div className="rounded-lg border border-[color:var(--cinnabar)]/25 bg-[color:var(--cinnabar)]/5 px-6 py-5 text-center">
          <p className="text-base font-semibold text-[color:var(--cinnabar)]">仅管理员可访问</p>
          <p className="mt-2 text-sm text-muted-foreground">当前账号没有查看转化监控的权限。</p>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <TrendingUp className="h-5 w-5 text-primary" />
            <h1 className="text-xl font-bold text-foreground">转化监控</h1>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            第二期监控页，聚合注册、登录、短信验证、任务创建和支付订阅数据。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            className="h-9 rounded-md border border-input bg-background px-3 text-sm text-foreground"
            value={windowDays}
            onChange={(event) => setWindowDays(Number(event.target.value))}
            disabled={loading || refreshing}
          >
            {WINDOW_OPTIONS.map((days) => (
              <option key={days} value={days}>近 {days} 天</option>
            ))}
          </select>
          <button
            type="button"
            className="inline-flex h-9 items-center gap-2 rounded-md border border-border bg-background px-3 text-sm font-medium text-foreground hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60"
            disabled={loading || refreshing}
            onClick={() => void loadConversions(true, true)}
          >
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
            刷新
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-[color:var(--cinnabar)]/25 bg-[color:var(--cinnabar)]/5 px-4 py-3 text-sm text-[color:var(--cinnabar)]">
          加载失败：{error}
        </div>
      )}

      {loading && !data ? (
        <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
          正在读取业务行为数据...
        </div>
      ) : behavior?.available && data ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
            <StatCard
              label="估算真实访客"
              value={fmtInt(data.totals.estimated_human_page_visitors_ip_ua)}
              sub={`${fmtInt(data.totals.page_views)} 次页面浏览`}
              icon={<Users className="h-4 w-4" />}
            />
            <StatCard
              label="注册用户"
              value={fmtInt(behavior.totals.registrations)}
              sub={`${fmtInt(behavior.totals.phone_verified_users)} 个完成手机验证`}
              icon={<Users className="h-4 w-4" />}
            />
            <StatCard
              label="登录用户"
              value={fmtInt(behavior.totals.logged_in_users)}
              sub={`${fmtInt(behavior.totals.login_sessions)} 次登录会话`}
              icon={<Activity className="h-4 w-4" />}
            />
            <StatCard
              label="短信验证"
              value={fmtInt(behavior.totals.sms_consumed)}
              sub={`${fmtInt(behavior.totals.sms_sent)} 次发送，${fmtInt(behavior.totals.sms_expired_unused)} 次过期未用`}
              icon={<ShieldAlert className="h-4 w-4" />}
              tone={behavior.totals.sms_expired_unused > behavior.totals.sms_consumed ? "warn" : "default"}
            />
            <StatCard
              label="创建任务"
              value={fmtInt(behavior.totals.jobs_created)}
              sub={`${fmtInt(behavior.totals.job_users)} 个任务用户`}
              icon={<BarChart3 className="h-4 w-4" />}
            />
            <StatCard
              label="实收金额"
              value={fmtMoney(behavior.totals.paid_amount_cny)}
              sub={`${fmtInt(behavior.totals.payment_orders_paid)} 个已支付订单`}
              icon={<Wallet className="h-4 w-4" />}
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="转化漏斗">
              <ConversionFunnel rows={behavior.conversion} />
            </Panel>
            <Panel title="业务行为趋势">
              <BehaviorDailyTrend rows={behavior.daily} />
            </Panel>
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="任务状态">
              <RankList rows={behavior.job_statuses} empty="暂无任务" />
            </Panel>
            <Panel title="任务模式">
              <RankList rows={behavior.job_modes} empty="暂无任务模式数据" />
            </Panel>
            <Panel title="当前用户计划">
              <RankList rows={behavior.plans} empty="暂无用户计划数据" />
            </Panel>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="支付渠道">
              <RankList rows={behavior.payment_providers} empty="当前窗口暂无已支付订单" />
            </Panel>
            <Panel title="付费计划">
              <RankList rows={behavior.payment_plans} empty="当前窗口暂无付费计划数据" />
            </Panel>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <Panel title="最近注册">
              {behavior.recent_users.length ? (
                <SimpleTable headers={["时间", "用户", "计划", "角色"]}>
                  {behavior.recent_users.map((row, index) => (
                    <tr key={`${row.identity}-${index}`} className="border-t border-border/60">
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">{row.created_at ? fmtTime(row.created_at) : "-"}</td>
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-foreground">{row.identity}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">{row.plan_code}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">{row.role}</td>
                    </tr>
                  ))}
                </SimpleTable>
              ) : (
                <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">当前窗口暂无注册用户</div>
              )}
            </Panel>

            <Panel title="最近任务">
              {behavior.recent_jobs.length ? (
                <SimpleTable headers={["时间", "用户", "状态", "模式", "标题"]}>
                  {behavior.recent_jobs.map((row) => (
                    <tr key={row.job_id} className="border-t border-border/60">
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">{row.created_at ? fmtTime(row.created_at) : "-"}</td>
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-muted-foreground">{row.owner}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-foreground">{row.status}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">{row.service_mode}</td>
                      <td className="max-w-[260px] truncate px-3 py-2 text-xs text-foreground" title={row.title}>
                        {row.title || row.source_type}
                      </td>
                    </tr>
                  ))}
                </SimpleTable>
              ) : (
                <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">当前窗口暂无任务</div>
              )}
            </Panel>

            <Panel title="最近支付" className="xl:col-span-2">
              {behavior.recent_paid_orders.length ? (
                <SimpleTable headers={["时间", "用户", "渠道", "计划", "周期", "金额"]}>
                  {behavior.recent_paid_orders.map((row, index) => (
                    <tr key={`${row.user}-${row.paid_at}-${index}`} className="border-t border-border/60">
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">{row.paid_at ? fmtTime(row.paid_at) : "-"}</td>
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-xs text-muted-foreground">{row.user}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-foreground">{row.provider}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-foreground">{row.plan_code}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">{row.billing_period}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-xs tabular-nums text-foreground">{fmtMoney(row.amount_cny)}</td>
                    </tr>
                  ))}
                </SimpleTable>
              ) : (
                <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">当前窗口暂无已支付订单</div>
              )}
            </Panel>
          </div>

          <Panel title="口径说明">
            <ul className="space-y-2 text-sm text-muted-foreground">
              {behavior.methodology.map((item) => (
                <li key={item} className="flex gap-2">
                  <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-muted-foreground" />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
            <div className="mt-3 rounded-md bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
              生成时间：{fmtTime(data.source.generated_at_utc)}
              {data.cached ? " · 来自短缓存" : ""}
            </div>
          </Panel>
        </>
      ) : behavior ? (
        <div className="rounded-lg border border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/5 px-4 py-3 text-sm text-[color:var(--ochre)]">
          行为漏斗暂不可用：{behavior.error}
        </div>
      ) : null}
    </div>
  )
}
