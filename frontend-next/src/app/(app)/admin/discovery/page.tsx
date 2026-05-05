"use client"

import { useCallback, useEffect, useState } from "react"
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ExternalLink,
  FileSearch,
  Globe2,
  RefreshCw,
  Search,
  Sparkles,
} from "lucide-react"

type RankedRow = {
  key: string
  label: string
  kind?: string
  count: number
  share: number
}

type DiscoveryCheck = {
  key: string
  label: string
  status: "ok" | "notice" | "warning" | "critical"
  detail: string
}

type AllowlistGuidance = {
  crawler: string
  recommendation: string
  reason: string
}

type DailyDiscoveryRow = {
  date: string
  search_engine_requests: number
  ai_crawler_requests: number
  crawler_page_fetches: number
  crawler_errors: number
  robots_requests: number
  sitemap_requests: number
  search_referrals: number
  ai_referrals: number
}

type DiscoverySummary = {
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
    crawler_requests: number
    search_engine_requests: number
    ai_crawler_requests: number
    crawler_page_fetches: number
    crawler_successful_page_fetches: number
    blocked_crawler_requests: number
    crawler_error_requests: number
    robots_requests: number
    robots_successes: number
    sitemap_requests: number
    sitemap_successes: number
    search_referrals: number
    ai_referrals: number
    public_paths_seen: number
    blocked_surface_hits: number
    malformed_rows: number
  }
  crawler_families: RankedRow[]
  crawler_paths: RankedRow[]
  public_paths: RankedRow[]
  crawler_statuses: RankedRow[]
  crawler_countries: RankedRow[]
  search_referrers: RankedRow[]
  ai_referrers: RankedRow[]
  blocked_paths: RankedRow[]
  daily: DailyDiscoveryRow[]
  checks: DiscoveryCheck[]
  recommendations: string[]
  allowlist_guidance: AllowlistGuidance[]
  methodology: string[]
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

function fmtTime(iso: string | null | undefined) {
  if (!iso) return "-"
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

function statusClass(status: DiscoveryCheck["status"]) {
  if (status === "critical" || status === "warning") {
    return "border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/8 text-[color:var(--cinnabar)]"
  }
  if (status === "notice") {
    return "border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/8 text-[color:var(--ochre)]"
  }
  return "border-[color:var(--bamboo)]/30 bg-[color:var(--bamboo)]/8 text-[color:var(--bamboo)]"
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
        <div key={`${row.key}-${row.kind ?? ""}`} className="space-y-1">
          <div className="flex items-center justify-between gap-3 text-sm">
            <span className={`min-w-0 truncate text-foreground ${keyClassName}`} title={row.label || row.key}>
              {row.label || row.key}
            </span>
            <span className="flex shrink-0 items-center gap-2 text-xs tabular-nums text-muted-foreground">
              {row.kind ? (
                <span className="rounded-full border border-border px-1.5 py-0.5 text-[10px] uppercase">
                  {row.kind === "ai" ? "AI" : row.kind}
                </span>
              ) : null}
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

function CheckList({ checks }: { checks: DiscoveryCheck[] }) {
  if (!checks.length) {
    return <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">暂无检查项</div>
  }
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {checks.map((check) => (
        <div key={check.key} className={`rounded-lg border p-3 ${statusClass(check.status)}`}>
          <div className="flex items-center gap-2 text-sm font-semibold">
            {check.status === "ok" ? <CheckCircle2 className="h-4 w-4" /> : <AlertTriangle className="h-4 w-4" />}
            <span>{check.label}</span>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{check.detail}</p>
        </div>
      ))}
    </div>
  )
}

function DailyTrend({ rows }: { rows: DailyDiscoveryRow[] }) {
  if (!rows.length) {
    return <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">暂无趋势数据</div>
  }
  const max = Math.max(
    ...rows.map((row) => Math.max(row.search_engine_requests, row.ai_crawler_requests, row.search_referrals + row.ai_referrals, row.crawler_errors)),
    1,
  )
  return (
    <div className="space-y-3">
      {rows.map((row) => (
        <div key={row.date} className="grid grid-cols-[72px_1fr_94px] items-center gap-3 text-xs">
          <span className="text-muted-foreground">{row.date.slice(5)}</span>
          <div className="grid gap-1">
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-[color:var(--bamboo)]/70" style={{ width: `${Math.max(0, (row.search_engine_requests / max) * 100)}%` }} />
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-primary/70" style={{ width: `${Math.max(0, (row.ai_crawler_requests / max) * 100)}%` }} />
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-[color:var(--ochre)]/70" style={{ width: `${Math.max(0, ((row.search_referrals + row.ai_referrals) / max) * 100)}%` }} />
            </div>
          </div>
          <span className="text-right tabular-nums text-muted-foreground">
            {fmtInt(row.search_engine_requests)} / {fmtInt(row.ai_crawler_requests)} / {fmtInt(row.search_referrals + row.ai_referrals)}
          </span>
        </div>
      ))}
      <div className="flex flex-wrap gap-3 pt-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-[color:var(--bamboo)]/70" />搜索 crawler</span>
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-primary/70" />AI crawler</span>
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-[color:var(--ochre)]/70" />真实入口</span>
      </div>
    </div>
  )
}

function GuidanceTable({ rows }: { rows: AllowlistGuidance[] }) {
  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="min-w-full text-sm">
        <thead className="bg-muted/40">
          <tr>
            <th className="whitespace-nowrap px-3 py-2 text-left text-xs font-medium text-muted-foreground">Crawler</th>
            <th className="whitespace-nowrap px-3 py-2 text-left text-xs font-medium text-muted-foreground">建议</th>
            <th className="whitespace-nowrap px-3 py-2 text-left text-xs font-medium text-muted-foreground">原因</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.crawler} className="border-t border-border/60">
              <td className="min-w-[220px] px-3 py-2 font-mono text-xs text-foreground">{row.crawler}</td>
              <td className="whitespace-nowrap px-3 py-2 text-xs text-foreground">{row.recommendation}</td>
              <td className="min-w-[260px] px-3 py-2 text-xs text-muted-foreground">{row.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function AdminDiscoveryPage() {
  const [windowDays, setWindowDays] = useState(7)
  const [data, setData] = useState<DiscoverySummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadDiscovery = useCallback(async (silent = false, force = false) => {
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
      })
      if (force) qs.set("force", "true")
      const resp = await fetch(`/api/admin/traffic/discovery?${qs.toString()}`, {
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
      setData((await resp.json()) as DiscoverySummary)
      setForbidden(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载发现优化数据失败")
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [windowDays])

  useEffect(() => {
    void loadDiscovery()
  }, [loadDiscovery])

  if (forbidden) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center px-4">
        <div className="rounded-lg border border-[color:var(--cinnabar)]/25 bg-[color:var(--cinnabar)]/5 px-6 py-5 text-center">
          <p className="text-base font-semibold text-[color:var(--cinnabar)]">仅管理员可访问</p>
          <p className="mt-2 text-sm text-muted-foreground">当前账号没有查看发现优化的权限。</p>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <Search className="h-5 w-5 text-primary" />
            <h1 className="text-xl font-bold text-foreground">发现优化</h1>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            第四期监控页，面向搜索引擎和 AI 发现：观察 crawler、robots、sitemap、公开页面覆盖和真实推荐入口。
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
            onClick={() => void loadDiscovery(true, true)}
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
          正在读取搜索和 AI 发现数据...
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
              label="Crawler 请求"
              value={fmtInt(data.totals.crawler_requests)}
              sub={`${fmtInt(data.totals.crawler_page_fetches)} 次页面抓取`}
              icon={<Bot className="h-4 w-4" />}
            />
            <StatCard
              label="搜索引擎"
              value={fmtInt(data.totals.search_engine_requests)}
              sub="Google / Bing / 百度等"
              icon={<Globe2 className="h-4 w-4" />}
            />
            <StatCard
              label="AI crawler"
              value={fmtInt(data.totals.ai_crawler_requests)}
              sub="ChatGPT / Claude / Perplexity 等"
              icon={<Sparkles className="h-4 w-4" />}
              tone={data.totals.ai_crawler_requests ? "default" : "warn"}
            />
            <StatCard
              label="被拦截 crawler"
              value={fmtInt(data.totals.blocked_crawler_requests)}
              sub={`${fmtInt(data.totals.crawler_error_requests)} 次 crawler 错误`}
              icon={<AlertTriangle className="h-4 w-4" />}
              tone={data.totals.blocked_crawler_requests ? "danger" : "default"}
            />
            <StatCard
              label="robots / sitemap"
              value={`${fmtInt(data.totals.robots_successes)} / ${fmtInt(data.totals.sitemap_successes)}`}
              sub="成功读取次数"
              icon={<FileSearch className="h-4 w-4" />}
              tone={data.totals.sitemap_successes ? "default" : "warn"}
            />
            <StatCard
              label="真实入口"
              value={fmtInt(data.totals.search_referrals + data.totals.ai_referrals)}
              sub={`${fmtInt(data.totals.search_referrals)} 搜索，${fmtInt(data.totals.ai_referrals)} AI`}
              icon={<ExternalLink className="h-4 w-4" />}
            />
          </div>

          <Panel title="发现健康检查">
            <CheckList checks={data.checks} />
          </Panel>

          <div className="grid gap-4 xl:grid-cols-[1fr_0.9fr]">
            <Panel title="每日发现趋势">
              <DailyTrend rows={data.daily} />
            </Panel>
            <Panel title="Crawler 家族">
              <RankList rows={data.crawler_families} empty="当前窗口暂无 crawler" />
            </Panel>
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="Crawler 抓取路径">
              <RankList rows={data.crawler_paths} keyClassName="font-mono text-xs" empty="暂无抓取路径" />
            </Panel>
            <Panel title="公开页面覆盖">
              <RankList rows={data.public_paths} keyClassName="font-mono text-xs" empty="暂无公开页面抓取" />
            </Panel>
            <Panel title="Crawler 状态码">
              <RankList rows={data.crawler_statuses} empty="暂无状态码" />
            </Panel>
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="Crawler 地区">
              <RankList rows={data.crawler_countries} empty="暂无 crawler 地区" />
            </Panel>
            <Panel title="搜索来源">
              <RankList rows={data.search_referrers} keyClassName="font-mono text-xs" empty="暂无搜索来源入口" />
            </Panel>
            <Panel title="AI 来源">
              <RankList rows={data.ai_referrers} keyClassName="font-mono text-xs" empty="暂无 AI 来源入口" />
            </Panel>
          </div>

          {data.blocked_paths.length ? (
            <Panel title="被拦截的 crawler 路径">
              <RankList rows={data.blocked_paths} keyClassName="font-mono text-xs" />
            </Panel>
          ) : null}

          <Panel title="Crawler 放行建议">
            <GuidanceTable rows={data.allowlist_guidance} />
          </Panel>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="下一步建议">
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
