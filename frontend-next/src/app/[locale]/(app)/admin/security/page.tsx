"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Activity,
  AlertTriangle,
  Ban,
  KeyRound,
  ListChecks,
  RefreshCw,
  ShieldAlert,
  Smartphone,
  Terminal,
} from "lucide-react"

type RankedRow = {
  key: string
  label: string
  count: number
  share: number
}

type SecurityAlert = {
  key: string
  title: string
  severity: "notice" | "warning" | "critical" | "ok"
  count: number
  threshold: number
  detail: string
  recommendation: string
  evidence: string[]
}

type WafCandidate = {
  path: string
  count: number
  action: "block" | "review" | string
  reason: string
}

type DailySecurityRow = {
  date: string
  scanner_requests: number
  auth_errors: number
  server_errors: number
  automation_requests: number
  sms_sent: number
}

type SecuritySummary = {
  available: boolean
  cached: boolean
  error: string | null
  level: "ok" | "notice" | "warning" | "critical"
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
    scanner_requests: number
    automation_requests: number
    suspicious_path_requests: number
    unique_scanner_ips: number
    auth_error_requests: number
    api_error_requests: number
    server_error_requests: number
    malformed_rows: number
    sms_sent: number
    sms_consumed: number
    sms_expired_unused: number
    sms_distinct_ips: number
    sms_distinct_phones: number
  }
  alerts: SecurityAlert[]
  waf_candidates: WafCandidate[]
  scanner_paths: RankedRow[]
  scanner_countries: RankedRow[]
  scanner_ips: RankedRow[]
  automation_user_agents: RankedRow[]
  auth_error_paths: RankedRow[]
  api_error_paths: RankedRow[]
  server_error_paths: RankedRow[]
  sms_ips: RankedRow[]
  sms_phones: RankedRow[]
  daily: DailySecurityRow[]
  methodology: string[]
}

const WINDOW_OPTIONS = [1, 7, 14, 30]
const integerFormat = new Intl.NumberFormat("zh-CN")

const SEVERITY_LABELS: Record<SecurityAlert["severity"], string> = {
  ok: "正常",
  notice: "关注",
  warning: "告警",
  critical: "高危",
}

const LEVEL_COPY: Record<SecuritySummary["level"], { label: string; sub: string }> = {
  ok: { label: "正常", sub: "当前窗口未触发安全告警" },
  notice: { label: "关注", sub: "有低频异常，建议保留趋势观察" },
  warning: { label: "告警", sub: "存在需要处理的异常模式" },
  critical: { label: "高危", sub: "建议优先查看告警和 WAF 候选规则" },
}

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

function levelClass(level: SecuritySummary["level"] | SecurityAlert["severity"]) {
  if (level === "critical") {
    return "border-[color:var(--cinnabar)]/35 bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)]"
  }
  if (level === "warning") {
    return "border-[color:var(--ochre)]/35 bg-[color:var(--ochre)]/10 text-[color:var(--ochre)]"
  }
  if (level === "notice") {
    return "border-primary/30 bg-primary/10 text-primary"
  }
  return "border-[color:var(--bamboo)]/30 bg-[color:var(--bamboo)]/10 text-[color:var(--bamboo)]"
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
            <span className={`min-w-0 truncate text-foreground ${keyClassName}`} title={row.label || row.key}>
              {row.label || row.key}
            </span>
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

function AlertList({ alerts }: { alerts: SecurityAlert[] }) {
  if (!alerts.length) {
    return (
      <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">
        当前窗口没有触发告警
      </div>
    )
  }
  return (
    <div className="space-y-3">
      {alerts.map((alert) => (
        <article key={alert.key} className={`rounded-lg border p-3 ${levelClass(alert.severity)}`}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold">{alert.title}</div>
            <span className="rounded-full border border-current/25 px-2 py-0.5 text-xs">
              {SEVERITY_LABELS[alert.severity]} · {fmtInt(alert.count)}
            </span>
          </div>
          <p className="mt-2 text-sm text-foreground/80">{alert.detail}</p>
          <p className="mt-2 text-sm text-muted-foreground">{alert.recommendation}</p>
          {alert.evidence.length ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {alert.evidence.slice(0, 5).map((item) => (
                <span key={item} className="max-w-full truncate rounded-md border border-current/20 bg-background/50 px-2 py-0.5 font-mono text-[11px]">
                  {item}
                </span>
              ))}
            </div>
          ) : null}
        </article>
      ))}
    </div>
  )
}

function WafTable({ rows }: { rows: WafCandidate[] }) {
  if (!rows.length) {
    return (
      <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">
        暂无 WAF 候选规则
      </div>
    )
  }
  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="min-w-full text-sm">
        <thead className="bg-muted/40">
          <tr>
            <th className="whitespace-nowrap px-3 py-2 text-left text-xs font-medium text-muted-foreground">路径</th>
            <th className="whitespace-nowrap px-3 py-2 text-left text-xs font-medium text-muted-foreground">请求</th>
            <th className="whitespace-nowrap px-3 py-2 text-left text-xs font-medium text-muted-foreground">动作</th>
            <th className="whitespace-nowrap px-3 py-2 text-left text-xs font-medium text-muted-foreground">原因</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.path} className="border-t border-border/60">
              <td className="max-w-[260px] truncate px-3 py-2 font-mono text-xs text-foreground" title={row.path}>
                {row.path}
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-xs tabular-nums text-foreground">{fmtInt(row.count)}</td>
              <td className="whitespace-nowrap px-3 py-2">
                <span className={`rounded-full border px-2 py-0.5 text-xs ${row.action === "block" ? levelClass("warning") : levelClass("notice")}`}>
                  {row.action === "block" ? "建议拦截" : "建议复核"}
                </span>
              </td>
              <td className="min-w-[220px] px-3 py-2 text-xs text-muted-foreground">{row.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DailySecurityTrend({ rows }: { rows: DailySecurityRow[] }) {
  if (!rows.length) {
    return <div className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted-foreground">暂无趋势数据</div>
  }
  const max = Math.max(
    ...rows.map((row) => Math.max(row.scanner_requests, row.auth_errors, row.server_errors, row.automation_requests, row.sms_sent)),
    1,
  )
  return (
    <div className="space-y-3">
      {rows.map((row) => (
        <div key={row.date} className="grid grid-cols-[72px_1fr_92px] items-center gap-3 text-xs">
          <span className="text-muted-foreground">{row.date.slice(5)}</span>
          <div className="grid gap-1">
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-[color:var(--cinnabar)]/75" style={{ width: `${Math.max(0, (row.scanner_requests / max) * 100)}%` }} />
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-[color:var(--ochre)]/75" style={{ width: `${Math.max(0, (row.auth_errors / max) * 100)}%` }} />
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-primary/70" style={{ width: `${Math.max(0, (row.sms_sent / max) * 100)}%` }} />
            </div>
          </div>
          <span className="text-right tabular-nums text-muted-foreground">
            {fmtInt(row.scanner_requests)} / {fmtInt(row.auth_errors)} / {fmtInt(row.sms_sent)}
          </span>
        </div>
      ))}
      <div className="flex flex-wrap gap-3 pt-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-[color:var(--cinnabar)]/75" />扫描</span>
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-[color:var(--ochre)]/75" />认证失败</span>
        <span className="inline-flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-primary/70" />短信</span>
      </div>
    </div>
  )
}

export default function AdminSecurityPage() {
  const [windowDays, setWindowDays] = useState(7)
  const [data, setData] = useState<SecuritySummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadSecurity = useCallback(async (silent = false, force = false) => {
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
      const resp = await fetch(`/api/admin/traffic/security?${qs.toString()}`, {
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
      setData((await resp.json()) as SecuritySummary)
      setForbidden(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载安全监控数据失败")
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [windowDays])

  useEffect(() => {
    void loadSecurity()
  }, [loadSecurity])

  const levelCopy = data ? LEVEL_COPY[data.level] : LEVEL_COPY.ok
  const smsFailureRate = useMemo(() => {
    if (!data?.totals.sms_sent) return null
    return data.totals.sms_expired_unused / data.totals.sms_sent
  }, [data])

  if (forbidden) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center px-4">
        <div className="rounded-lg border border-[color:var(--cinnabar)]/25 bg-[color:var(--cinnabar)]/5 px-6 py-5 text-center">
          <p className="text-base font-semibold text-[color:var(--cinnabar)]">仅管理员可访问</p>
          <p className="mt-2 text-sm text-muted-foreground">当前账号没有查看安全监控的权限。</p>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <ShieldAlert className="h-5 w-5 text-primary" />
            <h1 className="text-xl font-bold text-foreground">安全监控</h1>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            第三期监控页，聚合攻击扫描、认证失败、5xx、工具流量和短信验证码异常，并给出 WAF 候选规则。
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
            onClick={() => void loadSecurity(true, true)}
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
          正在读取安全监控数据...
        </div>
      ) : data ? (
        <>
          {!data.available && (
            <div className="rounded-lg border border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/5 px-4 py-3 text-sm text-[color:var(--ochre)]">
              {data.error}
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
            <div className={`rounded-lg border p-4 ${levelClass(data.level)}`}>
              <div className="flex items-center justify-between gap-3">
                <div className="text-xs text-muted-foreground">风险等级</div>
                <ShieldAlert className="h-4 w-4" />
              </div>
              <div className="mt-2 text-2xl font-bold">{levelCopy.label}</div>
              <div className="mt-1 text-xs text-muted-foreground">{levelCopy.sub}</div>
            </div>
            <StatCard
              label="扫描请求"
              value={fmtInt(data.totals.scanner_requests)}
              sub={`${fmtInt(data.totals.unique_scanner_ips)} 个脱敏来源`}
              icon={<Ban className="h-4 w-4" />}
              tone={data.totals.scanner_requests > 0 ? "danger" : "default"}
            />
            <StatCard
              label="认证失败"
              value={fmtInt(data.totals.auth_error_requests)}
              sub={`${fmtInt(data.totals.api_error_requests)} 个 API 错误请求`}
              icon={<KeyRound className="h-4 w-4" />}
              tone={data.totals.auth_error_requests >= 10 ? "warn" : "default"}
            />
            <StatCard
              label="5xx"
              value={fmtInt(data.totals.server_error_requests)}
              sub={data.totals.malformed_rows ? `异常日志 ${fmtInt(data.totals.malformed_rows)} 行` : "服务端错误"}
              icon={<AlertTriangle className="h-4 w-4" />}
              tone={data.totals.server_error_requests > 0 ? "warn" : "default"}
            />
            <StatCard
              label="短信验证"
              value={fmtInt(data.totals.sms_sent)}
              sub={`${fmtInt(data.totals.sms_consumed)} 成功，${fmtInt(data.totals.sms_expired_unused)} 过期未用`}
              icon={<Smartphone className="h-4 w-4" />}
              tone={(smsFailureRate ?? 0) > 0.5 ? "warn" : "default"}
            />
            <StatCard
              label="WAF 候选"
              value={fmtInt(data.waf_candidates.length)}
              sub={`${fmtInt(data.totals.suspicious_path_requests)} 个可疑路径请求`}
              icon={<ListChecks className="h-4 w-4" />}
              tone={data.waf_candidates.length ? "warn" : "default"}
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
            <Panel title="当前告警">
              <AlertList alerts={data.alerts} />
            </Panel>
            <Panel title="每日安全趋势">
              <DailySecurityTrend rows={data.daily} />
            </Panel>
          </div>

          <Panel title="WAF 候选规则">
            <WafTable rows={data.waf_candidates} />
          </Panel>

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="扫描路径">
              <RankList rows={data.scanner_paths} keyClassName="font-mono text-xs" empty="当前窗口没有明显扫描路径" />
            </Panel>
            <Panel title="扫描来源 IP">
              <RankList rows={data.scanner_ips} keyClassName="font-mono text-xs" empty="暂无扫描来源" />
            </Panel>
            <Panel title="扫描地区">
              <RankList rows={data.scanner_countries} empty="暂无扫描地区" />
            </Panel>
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Panel title="认证失败路径">
              <RankList rows={data.auth_error_paths} keyClassName="font-mono text-xs" empty="暂无认证失败路径" />
            </Panel>
            <Panel title="5xx 路径">
              <RankList rows={data.server_error_paths} keyClassName="font-mono text-xs" empty="暂无 5xx 路径" />
            </Panel>
            <Panel title="工具 User-Agent">
              <RankList rows={data.automation_user_agents} keyClassName="font-mono text-xs" empty="暂无工具流量" />
            </Panel>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="短信来源 IP">
              <RankList rows={data.sms_ips} keyClassName="font-mono text-xs" empty="当前窗口暂无短信请求" />
            </Panel>
            <Panel title="短信手机号">
              <RankList rows={data.sms_phones} keyClassName="font-mono text-xs" empty="当前窗口暂无短信手机号" />
            </Panel>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
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
            <Panel title="后续可接入">
              <ul className="space-y-2 text-sm text-muted-foreground">
                <li className="flex gap-2">
                  <Terminal className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>Cloudflare WAF 事件和速率限制命中记录，用来确认规则是否误伤真实用户。</span>
                </li>
                <li className="flex gap-2">
                  <Activity className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>容器错误日志摘要和慢请求排行，用来把 5xx 从“看到问题”推进到“定位原因”。</span>
                </li>
                <li className="flex gap-2">
                  <Smartphone className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>短信发送失败原因、验证码服务商回执和单用户冷却策略，减少真实用户验证失败。</span>
                </li>
              </ul>
            </Panel>
          </div>
        </>
      ) : null}
    </div>
  )
}
