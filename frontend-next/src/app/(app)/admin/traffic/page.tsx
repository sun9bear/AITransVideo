"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Globe2,
  RefreshCw,
  Search,
  ShieldAlert,
  Users,
} from "lucide-react"

type RankedRow = {
  key: string
  label: string
  count: number
  share: number
}

type TrafficExample = {
  time_utc: string
  ip: string
  country: string
  method: string
  path: string
  status: number
  user_agent: string
  referer: string
}

type DailyRow = {
  date: string
  total: number
  page_views: number
  human_page_visitors: number
  categories: Record<string, number>
}

type TrafficSummary = {
  available: boolean
  cached: boolean
  error: string | null
  window: {
    days: number
    from_utc: string
    to_utc: string
  }
  source: {
    log_dir: string
    files: string[]
    generated_at_utc: string
    cache_ttl_seconds: number
  }
  totals: {
    requests: number
    page_views: number
    estimated_unique_visitors_ip_ua: number
    estimated_human_visitors_ip_ua: number
    estimated_human_page_visitors_ip_ua: number
    malformed_rows: number
  }
  categories: RankedRow[]
  countries: RankedRow[]
  human_countries: RankedRow[]
  statuses: RankedRow[]
  methods: RankedRow[]
  top_paths: RankedRow[]
  top_page_views: RankedRow[]
  top_crawler_user_agents: RankedRow[]
  top_scanner_paths: RankedRow[]
  daily: DailyRow[]
  examples: Record<string, TrafficExample[]>
  recommendations: string[]
  methodology: string[]
}

const WINDOW_OPTIONS = [1, 7, 14, 30]

const CATEGORY_LABELS: Record<string, string> = {
  likely_human_browser: "可能真实用户",
  search_engine: "搜索引擎",
  ai_crawler: "AI 爬虫",
  automation_or_probe: "工具/探针",
  scanner: "攻击扫描",
  unknown: "未知",
}

const CATEGORY_STYLES: Record<string, string> = {
  likely_human_browser: "bg-[color:var(--bamboo)]/10 text-[color:var(--bamboo)] border-[color:var(--bamboo)]/25",
  search_engine: "bg-primary/10 text-primary border-primary/25",
  ai_crawler: "bg-[color:var(--ochre)]/10 text-[color:var(--ochre)] border-[color:var(--ochre)]/25",
  automation_or_probe: "bg-muted text-muted-foreground border-border",
  scanner: "bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)] border-[color:var(--cinnabar)]/25",
  unknown: "bg-muted/60 text-muted-foreground border-border",
}

const integerFormat = new Intl.NumberFormat("zh-CN")

function fmtInt(value: number | null | undefined) {
  return integerFormat.format(value ?? 0)
}

function fmtPct(value: number | null | undefined) {
  if (value == null) return "-"
  const n = value ?? 0
  return `${(n * 100).toFixed(n >= 0.1 ? 1 : 2)}%`
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

function categoryLabel(key: string) {
  return CATEGORY_LABELS[key] ?? key
}

function categoryClass(key: string) {
  return CATEGORY_STYLES[key] ?? CATEGORY_STYLES.unknown
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
  keyClassName = "",
}: {
  rows: RankedRow[]
  empty?: string
  keyClassName?: string
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
            <span className={`min-w-0 truncate text-foreground ${keyClassName}`}>{row.label || row.key}</span>
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

function CategoryList({ rows }: { rows: RankedRow[] }) {
  if (!rows.length) {
    return <div className="text-sm text-muted-foreground">暂无分类数据</div>
  }
  return (
    <div className="space-y-2">
      {rows.map((row) => (
        <div key={row.key} className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
          <span className={`rounded-full border px-2 py-0.5 text-xs ${categoryClass(row.key)}`}>
            {categoryLabel(row.key)}
          </span>
          <span className="text-sm tabular-nums text-foreground">
            {fmtInt(row.count)}
            <span className="ml-2 text-xs text-muted-foreground">{fmtPct(row.share)}</span>
          </span>
        </div>
      ))}
    </div>
  )
}

function DailyTrend({ rows }: { rows: DailyRow[] }) {
  if (!rows.length) {
    return <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">暂无趋势数据</div>
  }
  const max = Math.max(...rows.map((row) => row.total), 1)
  return (
    <div className="space-y-3">
      {rows.map((row) => {
        const human = row.categories.likely_human_browser ?? 0
        const crawler = (row.categories.search_engine ?? 0) + (row.categories.ai_crawler ?? 0)
        const scanner = row.categories.scanner ?? 0
        return (
          <div key={row.date} className="grid grid-cols-[76px_1fr_64px] items-center gap-3 text-xs">
            <span className="text-muted-foreground">{row.date.slice(5)}</span>
            <div className="h-2 overflow-hidden rounded-full bg-muted">
              <div className="flex h-full" style={{ width: `${Math.max(4, (row.total / max) * 100)}%` }}>
                <div className="bg-[color:var(--bamboo)]/70" style={{ width: `${row.total ? (human / row.total) * 100 : 0}%` }} />
                <div className="bg-primary/70" style={{ width: `${row.total ? (crawler / row.total) * 100 : 0}%` }} />
                <div className="bg-[color:var(--cinnabar)]/70" style={{ width: `${row.total ? (scanner / row.total) * 100 : 0}%` }} />
                <div className="flex-1 bg-muted-foreground/30" />
              </div>
            </div>
            <span className="text-right tabular-nums text-muted-foreground">{fmtInt(row.total)}</span>
          </div>
        )
      })}
      <div className="flex flex-wrap gap-3 pt-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-[color:var(--bamboo)]/70" />真实用户</span>
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-primary/70" />搜索/AI</span>
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-[color:var(--cinnabar)]/70" />扫描</span>
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

export default function AdminTrafficPage() {
  const [windowDays, setWindowDays] = useState(7)
  const [data, setData] = useState<TrafficSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadTraffic = useCallback(async (silent = false, force = false) => {
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
        include_behavior: "false",
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
      const payload = (await resp.json()) as TrafficSummary
      setData(payload)
      setForbidden(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载访问监控数据失败")
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [windowDays])

  useEffect(() => {
    void loadTraffic()
  }, [loadTraffic])

  const categoryCount = useMemo(() => {
    const map = new Map<string, number>()
    for (const row of data?.categories ?? []) {
      map.set(row.key, row.count)
    }
    return map
  }, [data])

  const fivexxCount = useMemo(() => (
    (data?.statuses ?? []).reduce((sum, row) => (
      row.key.startsWith("5") ? sum + row.count : sum
    ), 0)
  ), [data])

  const sampleRows = useMemo(() => {
    const rows: Array<TrafficExample & { category: string }> = []
    for (const [category, examples] of Object.entries(data?.examples ?? {})) {
      for (const example of examples) {
        rows.push({ ...example, category })
      }
    }
    return rows
      .sort((a, b) => new Date(b.time_utc).getTime() - new Date(a.time_utc).getTime())
      .slice(0, 24)
  }, [data])

  if (forbidden) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center px-4">
        <div className="rounded-lg border border-[color:var(--cinnabar)]/25 bg-[color:var(--cinnabar)]/5 px-6 py-5 text-center">
          <p className="text-base font-semibold text-[color:var(--cinnabar)]">仅管理员可访问</p>
          <p className="mt-2 text-sm text-muted-foreground">当前账号没有查看访问监控的权限。</p>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <Globe2 className="h-5 w-5 text-primary" />
            <h1 className="text-xl font-bold text-foreground">访问监控</h1>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            基于 Caddy / Cloudflare access log 的只读统计，用于区分真实访问、搜索爬取和异常扫描。
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
            onClick={() => void loadTraffic(true, true)}
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
          正在读取访问日志...
        </div>
      ) : data ? (
        <>
          {!data.available && (
            <div className="rounded-lg border border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/5 px-4 py-3 text-sm text-[color:var(--ochre)]">
              {data.error}
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
            <StatCard
              label="请求总量"
              value={fmtInt(data.totals.requests)}
              sub={`日志文件 ${data.source.files.length} 个`}
              icon={<Activity className="h-4 w-4" />}
            />
            <StatCard
              label="页面浏览"
              value={fmtInt(data.totals.page_views)}
              sub="排除 API、静态资源和扫描路径"
              icon={<BarChart3 className="h-4 w-4" />}
            />
            <StatCard
              label="估算真实访客"
              value={fmtInt(data.totals.estimated_human_page_visitors_ip_ua)}
              sub={`${fmtInt(data.totals.estimated_human_visitors_ip_ua)} 个浏览器访客指纹`}
              icon={<Users className="h-4 w-4" />}
            />
            <StatCard
              label="搜索/AI 爬虫"
              value={fmtInt((categoryCount.get("search_engine") ?? 0) + (categoryCount.get("ai_crawler") ?? 0))}
              sub="按 User-Agent 初步识别"
              icon={<Search className="h-4 w-4" />}
            />
            <StatCard
              label="扫描请求"
              value={fmtInt(categoryCount.get("scanner") ?? 0)}
              sub="WordPress/.env/.git 等路径"
              icon={<ShieldAlert className="h-4 w-4" />}
              tone={(categoryCount.get("scanner") ?? 0) > 0 ? "danger" : "default"}
            />
            <StatCard
              label="5xx"
              value={fmtInt(fivexxCount)}
              sub={data.totals.malformed_rows ? `异常日志 ${fmtInt(data.totals.malformed_rows)} 行` : "服务端错误"}
              icon={<AlertTriangle className="h-4 w-4" />}
              tone={fivexxCount > 0 ? "warn" : "default"}
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-[1fr_1fr_1fr]">
            <Panel title="流量分类">
              <CategoryList rows={data.categories} />
            </Panel>
            <Panel title="真实用户地区">
              <RankList rows={data.human_countries.length ? data.human_countries : data.countries} />
            </Panel>
            <Panel title="每日趋势">
              <DailyTrend rows={data.daily} />
            </Panel>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="热门页面">
              <RankList rows={data.top_page_views} keyClassName="font-mono text-xs" />
            </Panel>
            <Panel title="搜索引擎 / 工具 UA">
              <RankList rows={data.top_crawler_user_agents} keyClassName="font-mono text-xs" empty="未识别到爬虫或工具访问" />
            </Panel>
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="扫描路径">
              <RankList rows={data.top_scanner_paths} keyClassName="font-mono text-xs" empty="当前窗口没有明显扫描路径" />
            </Panel>
            <Panel title="状态码">
              <RankList rows={data.statuses} />
            </Panel>
            <Panel title="请求方法">
              <RankList rows={data.methods} />
            </Panel>
          </div>

          <Panel title="访问样本">
            {sampleRows.length ? (
              <SimpleTable headers={["时间", "分类", "地区/IP", "状态", "路径", "User-Agent"]}>
                {sampleRows.map((row, index) => (
                  <tr key={`${row.time_utc}-${row.path}-${index}`} className="border-t border-border/60">
                    <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">{fmtTime(row.time_utc)}</td>
                    <td className="whitespace-nowrap px-3 py-2">
                      <span className={`rounded-full border px-2 py-0.5 text-xs ${categoryClass(row.category)}`}>
                        {categoryLabel(row.category)}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">
                      {row.country} · {row.ip}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-xs tabular-nums text-foreground">
                      {row.method} {row.status}
                    </td>
                    <td className="max-w-[260px] truncate px-3 py-2 font-mono text-xs text-foreground" title={row.path}>
                      {row.path}
                    </td>
                    <td className="max-w-[360px] truncate px-3 py-2 font-mono text-xs text-muted-foreground" title={row.user_agent}>
                      {row.user_agent || "-"}
                    </td>
                  </tr>
                ))}
              </SimpleTable>
            ) : (
              <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">暂无样本</div>
            )}
          </Panel>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="建议">
              <ul className="space-y-2 text-sm text-muted-foreground">
                {data.recommendations.map((item) => (
                  <li key={item} className="flex gap-2">
                    <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
                    <span>{item}</span>
                  </li>
                ))}
              </ul>
            </Panel>
            <Panel title="口径说明">
              <ul className="space-y-2 text-sm text-muted-foreground">
                {data.methodology.map((item) => (
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
          </div>
        </>
      ) : null}
    </div>
  )
}
