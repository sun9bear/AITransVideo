"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  AlertTriangle,
  Database,
  HardDrive,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { useConfirmDialog } from "@/components/ui/confirm-dialog"

type DiskCandidate = {
  category: string
  job_id: string
  user_id: string
  path: string
  size_bytes: number
  size_gib: number
  mtime: string
  title: string
  status?: string
  current_stage?: string
  role_snapshot?: string
  expires_at?: string
  expired?: boolean
}

type DiskOverview = {
  scanned_at: string
  project_root: string
  filesystem: {
    path: string
    total_gib: number
    used_gib: number
    free_gib: number
    use_percent: number
  }
  mount: {
    available: boolean
    source?: string
    fstype?: string
    size?: string
    used?: string
    avail?: string
    target?: string
  }
  summary: Record<string, number>
  categories: {
    orphan_dirs: DiskCandidate[]
    expired_dirs: DiskCandidate[]
    protected_expired_dirs: DiskCandidate[]
    failed_dirs: DiskCandidate[]
    active_largest_dirs: DiskCandidate[]
  }
  largest_files: Array<{ size_gib: number; size_bytes: number; path: string }>
  resize_hint: {
    enabled: boolean
    feature_enabled?: boolean
    can_resize?: boolean
    needs_resize?: boolean
    reason: string
    commands: string[]
    device?: string
    mount_source?: string
    device_visible?: boolean
    resize2fs_available?: boolean
    tune2fs_available?: boolean
    device_gib?: number
    filesystem_gib?: number
  }
}

function formatGiB(value?: number) {
  return `${(value ?? 0).toFixed(2)}G`
}

function formatDate(iso?: string) {
  if (!iso) return "-"
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(iso))
  } catch {
    return iso
  }
}

function apiErrorMessage(data: unknown, fallback: string) {
  const payload = data as { detail?: unknown; message?: unknown } | null
  const detail = payload?.detail
  if (typeof detail === "string") return detail
  if (
    detail &&
    typeof detail === "object" &&
    "message" in detail &&
    typeof detail.message === "string"
  ) {
    return detail.message
  }
  if (typeof payload?.message === "string") return payload.message
  return fallback
}

function StatCard({
  label,
  value,
  detail,
  icon: Icon,
}: {
  label: string
  value: string
  detail?: string
  icon: typeof HardDrive
}) {
  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm text-muted-foreground">{label}</span>
        <Icon className="h-4 w-4 text-muted-foreground" />
      </div>
      <div className="mt-3 text-2xl font-semibold tracking-normal">{value}</div>
      {detail && <div className="mt-1 text-xs text-muted-foreground">{detail}</div>}
    </div>
  )
}

function CandidateTable({
  title,
  rows,
  empty,
  selectable = false,
  selected,
  onToggle,
}: {
  title: string
  rows: DiskCandidate[]
  empty: string
  selectable?: boolean
  selected?: Set<string>
  onToggle?: (jobId: string) => void
}) {
  return (
    <section className="space-y-3">
      <div className="flex items-end justify-between gap-3">
        <h2 className="text-lg font-semibold tracking-normal">{title}</h2>
        <span className="text-sm text-muted-foreground">
          {rows.length} 项 / {formatGiB(rows.reduce((sum, row) => sum + row.size_gib, 0))}
        </span>
      </div>
      <div className="overflow-hidden rounded-lg border bg-card">
        {rows.length === 0 ? (
          <div className="p-4 text-sm text-muted-foreground">{empty}</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[980px] text-sm">
              <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
                <tr>
                  {selectable && <th className="w-12 px-4 py-3" />}
                  <th className="px-4 py-3">大小</th>
                  <th className="px-4 py-3">任务</th>
                  <th className="px-4 py-3">状态</th>
                  <th className="px-4 py-3">时间</th>
                  <th className="px-4 py-3">路径</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {rows.map((row) => (
                  <tr key={`${row.category}-${row.job_id}`} className="align-top">
                    {selectable && (
                      <td className="px-4 py-3">
                        <input
                          aria-label={`选择 ${row.job_id}`}
                          checked={selected?.has(row.job_id) ?? false}
                          className="h-4 w-4"
                          type="checkbox"
                          onChange={() => onToggle?.(row.job_id)}
                        />
                      </td>
                    )}
                    <td className="whitespace-nowrap px-4 py-3 font-medium">
                      {formatGiB(row.size_gib)}
                    </td>
                    <td className="px-4 py-3">
                      <div className="font-medium">{row.title || row.job_id}</div>
                      <div className="mt-1 font-mono text-xs text-muted-foreground">
                        {row.job_id}
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-4 py-3">
                      {row.status || row.category}
                      {row.role_snapshot === "admin" && (
                        <span className="ml-2 rounded border border-[color:var(--ochre)]/30 px-2 py-0.5 text-xs text-[color:var(--ochre)]">
                          admin
                        </span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-muted-foreground">
                      {formatDate(row.expires_at || row.mtime)}
                    </td>
                    <td className="max-w-[360px] truncate px-4 py-3 font-mono text-xs text-muted-foreground">
                      {row.path}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}

export default function AdminDiskPage() {
  const [overview, setOverview] = useState<DiskOverview | null>(null)
  const [loading, setLoading] = useState(true)
  const [acting, setActing] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [selectedOrphans, setSelectedOrphans] = useState<Set<string>>(new Set())
  const { confirm, confirmDialog } = useConfirmDialog()

  const loadOverview = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetch("/api/admin/disk/overview", { credentials: "include" })
      if (res.status === 403) {
        setForbidden(true)
        return
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = (await res.json()) as DiskOverview
      setOverview(data)
      setForbidden(false)
      setSelectedOrphans(new Set(data.categories.orphan_dirs.map((row) => row.job_id)))
    } catch (err) {
      toast.error("加载磁盘信息失败")
      console.error(err)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadOverview()
  }, [loadOverview])

  const selectedOrphanIds = useMemo(
    () => Array.from(selectedOrphans),
    [selectedOrphans],
  )

  const toggleOrphan = useCallback((jobId: string) => {
    setSelectedOrphans((current) => {
      const next = new Set(current)
      if (next.has(jobId)) next.delete(jobId)
      else next.add(jobId)
      return next
    })
  }, [])

  const cleanupOrphans = useCallback(async () => {
    if (selectedOrphanIds.length === 0) return
    const confirmed = await confirm({
      title: "清理孤儿目录",
      description: `确认清理 ${selectedOrphanIds.length} 个孤儿任务目录？`,
      destructive: true,
    })
    if (!confirmed) return
    setActing("orphans")
    try {
      const res = await fetch("/api/admin/disk/cleanup-orphans", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_ids: selectedOrphanIds, dry_run: false }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail?.message || data?.detail || `HTTP ${res.status}`)
      toast.success(`已清理 ${formatGiB(data.freed_gib)} 孤儿目录`)
      await loadOverview()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "清理失败")
    } finally {
      setActing(null)
    }
  }, [confirm, loadOverview, selectedOrphanIds])

  const cleanupExpired = useCallback(async () => {
    if (!overview?.categories.expired_dirs.length) return
    const confirmed = await confirm({
      title: "执行过期清理",
      description: "确认执行过期任务清理？该操作会按后台保留策略更新 DB 并删除项目目录。",
      destructive: true,
    })
    if (!confirmed) return
    setActing("expired")
    try {
      const res = await fetch("/api/admin/disk/cleanup-expired", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dry_run: false }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`)
      toast.success(`过期清理完成：${data.purged_count ?? 0} 个任务`)
      await loadOverview()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "过期清理失败")
    } finally {
      setActing(null)
    }
  }, [confirm, loadOverview, overview?.categories.expired_dirs.length])

  const resizeFilesystem = useCallback(async () => {
    if (!overview?.resize_hint.enabled) return
    const device = overview.resize_hint.device || "/dev/sdb"
    const confirmed = await confirm({
      title: "扩展文件系统",
      description: `确认对 ${device} 执行 resize2fs？该操作只会在检测到块设备大于 ext4 文件系统时执行。`,
    })
    if (!confirmed) return
    setActing("resize")
    try {
      const res = await fetch("/api/admin/disk/resize-filesystem", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dry_run: false, confirm: true }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(apiErrorMessage(data, `HTTP ${res.status}`))
      toast.success(data?.after?.reason || "文件系统扩展完成")
      await loadOverview()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "扩展文件系统失败")
    } finally {
      setActing(null)
    }
  }, [confirm, loadOverview, overview?.resize_hint.device, overview?.resize_hint.enabled])

  if (forbidden) {
    return (
      <main className="p-6">
        <div className="rounded-lg border bg-card p-6 text-sm text-muted-foreground">
          需要管理员权限。
        </div>
      </main>
    )
  }

  if (loading && !overview) {
    return (
      <main className="flex min-h-[360px] items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </main>
    )
  }

  return (
    <main className="space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-normal">磁盘管理</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            扫描时间：{formatDate(overview?.scanned_at)}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            disabled={loading}
            variant="outline"
            onClick={() => void loadOverview()}
          >
            <RefreshCw className="mr-2 h-4 w-4" />
            刷新
          </Button>
          <Button
            disabled={selectedOrphanIds.length === 0 || acting === "orphans"}
            variant="destructive"
            onClick={() => void cleanupOrphans()}
          >
            {acting === "orphans" ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Trash2 className="mr-2 h-4 w-4" />
            )}
            清理孤儿目录
          </Button>
          <Button
            disabled={!overview?.categories.expired_dirs.length || acting === "expired"}
            variant="outline"
            onClick={() => void cleanupExpired()}
          >
            {acting === "expired" ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Database className="mr-2 h-4 w-4" />
            )}
            执行过期清理
          </Button>
        </div>
      </div>

      {overview && (
        <>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <StatCard
              detail={`${overview.filesystem.used_gib.toFixed(2)}G / ${overview.filesystem.total_gib.toFixed(2)}G`}
              icon={HardDrive}
              label="数据盘使用率"
              value={`${overview.filesystem.use_percent.toFixed(1)}%`}
            />
            <StatCard
              detail={overview.project_root}
              icon={Database}
              label="可用空间"
              value={formatGiB(overview.filesystem.free_gib)}
            />
            <StatCard
              detail={`${overview.summary.disk_job_dir_count} 个任务目录`}
              icon={AlertTriangle}
              label="孤儿目录"
              value={formatGiB(overview.summary.orphan_dirs_gib)}
            />
            <StatCard
              detail={`${overview.summary.expired_dirs_count} 个已过期任务`}
              icon={ShieldCheck}
              label="过期可清理"
              value={formatGiB(overview.summary.expired_dirs_gib)}
            />
          </div>

          <CandidateTable
            empty="没有发现 DB 已删除但磁盘仍存在的任务目录。"
            rows={overview.categories.orphan_dirs}
            selectable
            selected={selectedOrphans}
            title="已删除任务残留"
            onToggle={toggleOrphan}
          />

          <CandidateTable
            empty="没有符合后台保留策略的过期任务目录。"
            rows={overview.categories.expired_dirs}
            title="DB 仍在但已过期"
          />

          <CandidateTable
            empty="没有管理员保护的过期任务。"
            rows={overview.categories.protected_expired_dirs}
            title="管理员保护任务"
          />

          <CandidateTable
            empty="没有失败任务残留。"
            rows={overview.categories.failed_dirs}
            title="失败任务残留"
          />

          <CandidateTable
            empty="没有活跃任务目录。"
            rows={overview.categories.active_largest_dirs}
            title="活跃任务大目录"
          />

          <section className="space-y-3">
            <h2 className="text-lg font-semibold tracking-normal">最大文件</h2>
            <div className="overflow-hidden rounded-lg border bg-card">
              {overview.largest_files.length === 0 ? (
                <div className="p-4 text-sm text-muted-foreground">没有超过阈值的大文件。</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[760px] text-sm">
                    <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
                      <tr>
                        <th className="px-4 py-3">大小</th>
                        <th className="px-4 py-3">路径</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y">
                      {overview.largest_files.map((file) => (
                        <tr key={file.path}>
                          <td className="whitespace-nowrap px-4 py-3 font-medium">
                            {formatGiB(file.size_gib)}
                          </td>
                          <td className="max-w-[720px] truncate px-4 py-3 font-mono text-xs text-muted-foreground">
                            {file.path}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </section>

          <section className="rounded-lg border bg-card p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-sm font-medium">
                <HardDrive className="h-4 w-4" />
                扩容状态
              </div>
              <Button
                disabled={!overview.resize_hint.enabled || acting === "resize"}
                size="sm"
                variant={overview.resize_hint.enabled ? "default" : "outline"}
                onClick={() => void resizeFilesystem()}
              >
                {acting === "resize" ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <HardDrive className="mr-2 h-4 w-4" />
                )}
                扩展文件系统
              </Button>
            </div>
            <div className="mt-3 grid gap-2 text-sm text-muted-foreground md:grid-cols-2">
              <div>挂载源：{overview.mount.source || "-"}</div>
              <div>文件系统：{overview.mount.fstype || "-"}</div>
              <div>挂载大小：{overview.mount.size || "-"}</div>
              <div>挂载可用：{overview.mount.avail || "-"}</div>
              <div>配置设备：{overview.resize_hint.device || "-"}</div>
              <div>后端可见设备：{overview.resize_hint.device_visible ? "是" : "否"}</div>
              <div>块设备容量：{formatGiB(overview.resize_hint.device_gib)}</div>
              <div>ext4 容量：{formatGiB(overview.resize_hint.filesystem_gib)}</div>
            </div>
            <div className="mt-3 rounded border bg-muted/40 p-3 text-sm text-muted-foreground">
              {overview.resize_hint.reason}
            </div>
            <div className="mt-3 rounded border bg-muted/40 p-3 font-mono text-xs text-muted-foreground">
              {overview.resize_hint.commands.join("  |  ")}
            </div>
          </section>
        </>
      )}

      {confirmDialog}
    </main>
  )
}
