"use client"

/**
 * Admin Pan Backup Dashboard (Phase 7b §T7b.2).
 *
 * Connect-state card + quota ring + recent backups preview. Entry point
 * for admin to:
 *   - OAuth into their personal Baidu Pan account
 *   - See remaining quota at a glance
 *   - Disconnect (revokes credentials + tokens)
 *   - Jump to the full backup records list
 *
 * Layout principles:
 *   - Mobile-first 1-col, ≥sm splits into "status card | quota ring".
 *   - The status card is the source of truth; everything else hides /
 *     shows based on `status.connected` + `status.status`.
 *   - "Connect" is a native <form action="..." method="POST"> so the
 *     browser handles the cross-origin 302 to Baidu. Fetch can't.
 */

import { useCallback, useEffect, useState } from "react"
import Link from "next/link"
import {
  AlertTriangle,
  CheckCircle2,
  HardDrive,
  Link2,
  Loader2,
  Power,
  RefreshCw,
  XCircle,
} from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { buttonVariants } from "@/components/ui/button-variants"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  BACKUP_STATUS_LABEL,
  BACKUP_STATUS_TONE,
  type BackupRecord,
  CONNECT_URL,
  disconnectPan,
  formatBytesGB,
  formatTimestamp,
  getPanStatus,
  listBackups,
  type PanStatus,
} from "@/lib/api/pan"

// ===========================================================================
// Page
// ===========================================================================

export default function PanDashboardPage() {
  const [status, setStatus] = useState<PanStatus | null>(null)
  const [recentBackups, setRecentBackups] = useState<BackupRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [disconnecting, setDisconnecting] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const s = await getPanStatus()
      setStatus(s)
      if (s.connected && s.status === "active") {
        try {
          const list = await listBackups({ limit: 5 })
          // Backend key is `items` not `backups` (see pan.ts comment).
          // Defensive `?? []` guards against future shape regressions.
          setRecentBackups(list.items ?? [])
        } catch (e) {
          // Status loaded but list failed — keep status visible, just
          // show empty timeline.
          console.warn("listBackups failed", e)
          setRecentBackups([])
        }
      } else {
        setRecentBackups([])
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const handleDisconnect = async () => {
    if (
      !confirm(
        "确认断开百度网盘授权?\n\n断开后:\n• 所有 access_token / refresh_token 将被立即清除\n• 已有的备份文件留在网盘不受影响\n• 后续需要恢复任务时需重新连接并重新授权",
      )
    )
      return
    setDisconnecting(true)
    try {
      await disconnectPan()
      toast.success("已断开网盘授权")
      await refresh()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "断开失败")
    } finally {
      setDisconnecting(false)
    }
  }

  return (
    <div className="space-y-6 p-4 sm:p-6 max-w-6xl mx-auto">
      <Header onRefresh={refresh} loading={loading} />

      {error && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="flex items-center gap-3 py-4">
            <AlertTriangle className="size-5 text-destructive" />
            <span className="text-destructive">{error}</span>
          </CardContent>
        </Card>
      )}

      {loading && !status && <SkeletonCards />}

      {status && (
        <>
          <div className="grid gap-4 lg:grid-cols-2">
            <StatusCard
              status={status}
              onDisconnect={handleDisconnect}
              disconnecting={disconnecting}
            />
            {status.connected && status.status === "active" && (
              <QuotaCard quota={status.quota} quotaError={status.quota_error} />
            )}
          </div>

          {status.connected && status.status === "active" && (
            <RecentBackupsCard backups={recentBackups} />
          )}
        </>
      )}
    </div>
  )
}

// ===========================================================================
// Header
// ===========================================================================

function Header({
  onRefresh,
  loading,
}: {
  onRefresh: () => void
  loading: boolean
}) {
  return (
    <div className="flex items-center justify-between">
      <div>
        <h1 className="text-2xl font-semibold">网盘备份管理</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          归档 / 恢复任务工程到百度网盘 · 仅管理员可见
        </p>
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={onRefresh}
        disabled={loading}
      >
        {loading ? (
          <Loader2 className="size-4 animate-spin" />
        ) : (
          <RefreshCw className="size-4" />
        )}
        刷新
      </Button>
    </div>
  )
}

// ===========================================================================
// Status card — top-left, the source of truth
// ===========================================================================

function StatusCard({
  status,
  onDisconnect,
  disconnecting,
}: {
  status: PanStatus
  onDisconnect: () => void
  disconnecting: boolean
}) {
  // Three visual states: disconnected / active / revoked.
  if (!status.connected) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <XCircle className="size-5 text-muted-foreground" />
            未连接百度网盘
          </CardTitle>
          <CardDescription>
            首次使用需走一次 OAuth 授权,我们仅请求 basic + netdisk 两个权限。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <form action={CONNECT_URL} method="POST">
            <Button type="submit" className="w-full sm:w-auto">
              <Link2 className="size-4" />
              连接百度网盘
            </Button>
          </form>
          <p className="text-xs text-muted-foreground">
            点击连接 → 跳转百度授权页 → 同意后自动跳回本页面。
          </p>
        </CardContent>
      </Card>
    )
  }

  if (status.status === "revoked") {
    return (
      <Card className="border-destructive/40">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-destructive">
            <AlertTriangle className="size-5" />
            授权已失效
          </CardTitle>
          <CardDescription>
            后台 token 续期失败 3 次后凭据已被标记为已撤销。
            可能原因:百度账号在网盘端主动取消了应用授权,或长时间无活动导致 refresh_token 过期。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <form action={CONNECT_URL} method="POST">
            <Button type="submit" variant="destructive" className="w-full sm:w-auto">
              <Link2 className="size-4" />
              重新连接
            </Button>
          </form>
          {status.last_refreshed_at && (
            <p className="text-xs text-muted-foreground">
              上次成功续期: {formatTimestamp(status.last_refreshed_at)}
            </p>
          )}
        </CardContent>
      </Card>
    )
  }

  // active
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-green-600">
          <CheckCircle2 className="size-5" />
          已连接百度网盘
        </CardTitle>
        <CardDescription>
          授权范围: {status.scope || "basic, netdisk"}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-muted-foreground">首次连接</dt>
            <dd>{formatTimestamp(status.connected_at)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">上次续期</dt>
            <dd>{formatTimestamp(status.last_refreshed_at)}</dd>
          </div>
        </dl>
        <div className="flex flex-wrap gap-2 pt-2">
          <Link
            href="/admin/pan/backups"
            className={buttonVariants({ variant: "default", size: "sm" })}
          >
            查看全部备份
          </Link>
          <Button
            variant="outline"
            size="sm"
            onClick={onDisconnect}
            disabled={disconnecting}
          >
            {disconnecting ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Power className="size-4" />
            )}
            断开授权
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

// ===========================================================================
// Quota card — SVG ring
// ===========================================================================

function QuotaCard({
  quota,
  quotaError,
}: {
  quota: PanStatus["quota"]
  quotaError?: string
}) {
  if (quotaError || !quota) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <HardDrive className="size-5 text-muted-foreground" />
            配额暂不可用
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            {quotaError ||
              "无法读取百度网盘配额。可能是网络抖动或百度接口暂时不可用,刷新可重试。"}
          </p>
        </CardContent>
      </Card>
    )
  }

  const total = quota.total || 0
  const used = quota.used || 0
  const free = quota.free ?? Math.max(0, total - used)
  const pct = total > 0 ? Math.min(100, (used / total) * 100) : 0
  const isWarn = pct >= 80
  const isCrit = pct >= 95

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <HardDrive className="size-5" />
          网盘配额
        </CardTitle>
        <CardDescription>用量 ≥ 80% 时建议清理或扩容</CardDescription>
      </CardHeader>
      <CardContent className="flex items-center gap-6">
        <QuotaRing pct={pct} isWarn={isWarn} isCrit={isCrit} />
        <dl className="space-y-1 text-sm">
          <div>
            <dt className="text-muted-foreground">已用</dt>
            <dd className="text-base font-medium">{formatBytesGB(used)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">剩余</dt>
            <dd className="text-base font-medium">{formatBytesGB(free)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">总量</dt>
            <dd className="text-base font-medium">{formatBytesGB(total)}</dd>
          </div>
        </dl>
      </CardContent>
    </Card>
  )
}

function QuotaRing({
  pct,
  isWarn,
  isCrit,
}: {
  pct: number
  isWarn: boolean
  isCrit: boolean
}) {
  // SVG ring — 120px radius, 12px stroke. Active arc length depends on
  // circumference + pct. Color tracks warn/crit thresholds.
  const size = 120
  const stroke = 12
  const radius = (size - stroke) / 2
  const circumference = 2 * Math.PI * radius
  const offset = circumference - (pct / 100) * circumference
  const color = isCrit
    ? "text-destructive"
    : isWarn
      ? "text-amber-500"
      : "text-primary"

  return (
    <div className="relative shrink-0">
      <svg width={size} height={size} className="-rotate-90 transform">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          stroke="currentColor"
          strokeWidth={stroke}
          fill="transparent"
          className="text-muted/30"
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          stroke="currentColor"
          strokeWidth={stroke}
          fill="transparent"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          className={`${color} transition-all duration-500`}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className={`text-2xl font-semibold ${color}`}>
          {pct.toFixed(1)}%
        </span>
        <span className="text-xs text-muted-foreground">已用</span>
      </div>
    </div>
  )
}

// ===========================================================================
// Recent backups card — vertical timeline
// ===========================================================================

function RecentBackupsCard({ backups }: { backups: BackupRecord[] }) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>最近备份</CardTitle>
          <CardDescription>显示最新 5 条记录</CardDescription>
        </div>
        <Link
          href="/admin/pan/backups"
          className={buttonVariants({ variant: "ghost", size: "sm" })}
        >
          查看全部 →
        </Link>
      </CardHeader>
      <CardContent>
        {backups.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            还没有任何备份。在任务列表中选择已完成的任务,点击「备份到网盘」开始。
          </p>
        ) : (
          <ol className="relative space-y-4 border-l border-foreground/10 pl-6">
            {backups.map((b) => (
              <BackupTimelineEntry key={b.id} backup={b} />
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  )
}

function BackupTimelineEntry({ backup }: { backup: BackupRecord }) {
  const status = (backup.status ?? "uploaded") as keyof typeof BACKUP_STATUS_TONE
  const tone = BACKUP_STATUS_TONE[status] ?? "muted"
  const label = BACKUP_STATUS_LABEL[status] ?? backup.status

  const toneClass = {
    success: "bg-green-500 ring-green-500/20",
    danger: "bg-destructive ring-destructive/20",
    active: "bg-amber-500 ring-amber-500/20",
    info: "bg-primary ring-primary/20",
    muted: "bg-muted-foreground ring-muted-foreground/20",
  }[tone]

  return (
    <li className="relative">
      <span
        className={`absolute -left-[31px] mt-1 size-3 rounded-full ring-4 ${toneClass}`}
      />
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {backup.job_display_name ? (
          <span
            className="text-sm font-medium text-foreground"
            title={backup.job_id}
          >
            {backup.job_display_name}
          </span>
        ) : (
          <code className="text-sm font-medium">{backup.job_id}</code>
        )}
        <span
          className={`rounded px-1.5 py-0.5 text-xs ${
            tone === "success"
              ? "bg-green-500/10 text-green-700 dark:text-green-400"
              : tone === "danger"
                ? "bg-destructive/10 text-destructive"
                : tone === "active"
                  ? "bg-amber-500/10 text-amber-700 dark:text-amber-400"
                  : tone === "info"
                    ? "bg-primary/10 text-primary"
                    : "bg-muted text-muted-foreground"
          }`}
        >
          {label}
        </span>
        <span className="text-xs text-muted-foreground">
          {formatBytesGB(backup.size_bytes)} ·{" "}
          {formatTimestamp(backup.completed_at || backup.created_at)}
        </span>
      </div>
      {backup.error_message && (
        <p className="mt-1 text-xs text-destructive">{backup.error_message}</p>
      )}
    </li>
  )
}

// ===========================================================================
// Loading skeleton
// ===========================================================================

function SkeletonCards() {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {[0, 1].map((i) => (
        <Card key={i}>
          <CardHeader>
            <div className="h-5 w-32 animate-pulse rounded bg-muted" />
            <div className="mt-2 h-4 w-48 animate-pulse rounded bg-muted/60" />
          </CardHeader>
          <CardContent>
            <div className="h-20 animate-pulse rounded bg-muted/40" />
          </CardContent>
        </Card>
      ))}
    </div>
  )
}
