"use client"

/**
 * Admin Pan Backup — Full Records List (Phase 7b §T7b.3).
 *
 * Lists every BackupRecord row with filters (status / job_id substring)
 * and per-row actions:
 *   - 恢复 (POST /api/admin/pan/restores)  — only for 'uploaded' rows
 *     not belonging to a job that's currently restoring.
 *   - 删除 (DELETE /api/admin/pan/backups/{id})  — 412-aware: when this
 *     is the only recoverable copy, prompt the user to confirm.
 *   - 看 Manifest — opens a dialog showing the tar's file inventory.
 *
 * Pagination intentionally simple (load more button rather than page
 * numbers); the typical pan_backup_records table will stay < 200 rows
 * for years.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import Link from "next/link"
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Eye,
  Filter,
  Loader2,
  Plus,
  RefreshCw,
  RotateCcw,
  Trash2,
} from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  BACKUP_STATUS_LABEL,
  BACKUP_STATUS_TONE,
  type BackupManifest,
  type BackupRecord,
  type BackupRecordStatus,
  deleteBackup,
  enqueueBackup,
  enqueueRestore,
  formatBytesGB,
  formatTimestamp,
  getBackupManifest,
  listBackups,
} from "@/lib/api/pan"

const PAGE_SIZE = 50

const STATUS_FILTER_OPTIONS: Array<{
  value: BackupRecordStatus | "all"
  label: string
}> = [
  { value: "all", label: "全部状态" },
  { value: "uploaded", label: "已备份" },
  { value: "uploading", label: "上传中" },
  { value: "restoring", label: "恢复中" },
  { value: "restored", label: "已恢复" },
  { value: "failed", label: "失败" },
  { value: "deleted", label: "已删除" },
]

// ===========================================================================
// Page
// ===========================================================================

export default function PanBackupsPage() {
  const [rows, setRows] = useState<BackupRecord[]>([])
  const [total, setTotal] = useState(0)
  const [statusFilter, setStatusFilter] =
    useState<BackupRecordStatus | "all">("all")
  const [jobIdFilter, setJobIdFilter] = useState("")
  const [jobIdQuery, setJobIdQuery] = useState("") // debounced
  const [page, setPage] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [manifestOpen, setManifestOpen] = useState(false)
  const [manifestBackup, setManifestBackup] = useState<BackupRecord | null>(null)
  const [manifestData, setManifestData] = useState<BackupManifest | null>(null)
  const [manifestLoading, setManifestLoading] = useState(false)
  const [newBackupOpen, setNewBackupOpen] = useState(false)

  // Debounce job_id filter typing.
  useEffect(() => {
    const t = setTimeout(() => setJobIdQuery(jobIdFilter.trim()), 300)
    return () => clearTimeout(t)
  }, [jobIdFilter])

  const fetchPage = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await listBackups({
        status: statusFilter === "all" ? undefined : statusFilter,
        job_id: jobIdQuery || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      })
      // Backend key is `items` not `backups` (see pan.ts comment).
      const items = r.items ?? []
      setRows(items)
      setTotal(r.total ?? items.length)
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败")
    } finally {
      setLoading(false)
    }
  }, [statusFilter, jobIdQuery, page])

  useEffect(() => {
    void fetchPage()
  }, [fetchPage])

  // Filter changes reset to page 0.
  useEffect(() => {
    setPage(0)
  }, [statusFilter, jobIdQuery])

  const handleRestore = async (row: BackupRecord) => {
    if (
      !confirm(
        `确认从网盘恢复任务 ${row.job_id}?\n\n` +
          `这会:\n• 下载 ${formatBytesGB(row.size_bytes)} 的 tar 包到本地\n` +
          `• 安全解压回原项目目录\n• 任务状态从"已归档"变回"已完成"`,
      )
    )
      return
    try {
      const r = await enqueueRestore(row.job_id)
      toast.success(`恢复任务已入队 (task_id=${r.task_id.slice(0, 8)}…)`)
      await fetchPage()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "恢复入队失败")
    }
  }

  const handleDelete = async (row: BackupRecord) => {
    if (!confirm(`确认删除备份记录 ${row.id.slice(0, 8)}…?`)) return
    try {
      await deleteBackup(row.id)
      toast.success("备份记录已删除")
      await fetchPage()
    } catch (e) {
      const err = e as Error & { status?: number; payload?: unknown }
      // 412: only recoverable copy — ask for explicit confirm.
      if (err.status === 412) {
        if (
          !confirm(
            "⚠️ 这是该任务**唯一**可恢复副本!\n\n" +
              "确认删除后,该任务将永久无法从网盘恢复 (本地数据已被先前的归档流程清掉)。\n\n" +
              "确定继续吗?",
          )
        )
          return
        try {
          await deleteBackup(row.id, { confirm: true })
          toast.success("备份记录已强制删除")
          await fetchPage()
        } catch (e2) {
          toast.error(e2 instanceof Error ? e2.message : "强制删除失败")
        }
      } else {
        toast.error(err.message || "删除失败")
      }
    }
  }

  const handleNewBackup = async (jobId: string) => {
    try {
      const r = await enqueueBackup(jobId)
      toast.success(`备份任务已入队 (task_id=${r.task_id.slice(0, 8)}…)`)
      await fetchPage()
    } catch (e) {
      const err = e as Error & { status?: number }
      toast.error(err.message || "备份入队失败")
      throw err // keep dialog open so user can fix job_id
    }
  }

  const openManifest = async (row: BackupRecord) => {
    setManifestBackup(row)
    setManifestData(null)
    setManifestOpen(true)
    setManifestLoading(true)
    try {
      const m = await getBackupManifest(row.id)
      setManifestData(m)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "读取 manifest 失败")
      setManifestOpen(false)
    } finally {
      setManifestLoading(false)
    }
  }

  const totalPages = useMemo(
    () => Math.max(1, Math.ceil(total / PAGE_SIZE)),
    [total],
  )

  return (
    <div className="space-y-6 p-4 sm:p-6 max-w-7xl mx-auto">
      <Header
        onRefresh={fetchPage}
        loading={loading}
        onNewBackup={() => setNewBackupOpen(true)}
      />

      <FilterBar
        statusFilter={statusFilter}
        onStatusChange={setStatusFilter}
        jobIdFilter={jobIdFilter}
        onJobIdChange={setJobIdFilter}
      />

      {error && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="flex items-center gap-3 py-4">
            <AlertTriangle className="size-5 text-destructive" />
            <span className="text-destructive">{error}</span>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>
            备份记录 ({total} 条
            {(statusFilter !== "all" || jobIdQuery) && " · 已过滤"})
          </CardTitle>
          <CardDescription>
            点击「恢复」将下载 tar 包并解压回原任务目录。删除唯一副本需二次确认。
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {loading && rows.length === 0 ? (
            <TableSkeleton />
          ) : rows.length === 0 ? (
            <EmptyState />
          ) : (
            <BackupTable
              rows={rows}
              onRestore={handleRestore}
              onDelete={handleDelete}
              onManifest={openManifest}
            />
          )}
        </CardContent>
      </Card>

      {totalPages > 1 && (
        <Pagination
          page={page}
          totalPages={totalPages}
          onPageChange={setPage}
          disabled={loading}
        />
      )}

      <ManifestDialog
        open={manifestOpen}
        onOpenChange={setManifestOpen}
        backup={manifestBackup}
        manifest={manifestData}
        loading={manifestLoading}
      />

      <NewBackupDialog
        open={newBackupOpen}
        onOpenChange={setNewBackupOpen}
        onSubmit={handleNewBackup}
      />
    </div>
  )
}

// ===========================================================================
// Sub-components
// ===========================================================================

function Header({
  onRefresh,
  loading,
  onNewBackup,
}: {
  onRefresh: () => void
  loading: boolean
  onNewBackup: () => void
}) {
  return (
    <div className="flex items-center justify-between">
      <div>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Link href="/admin/pan/dashboard" className="hover:text-foreground">
            ← 仪表盘
          </Link>
        </div>
        <h1 className="mt-1 text-2xl font-semibold">网盘备份记录</h1>
      </div>
      <div className="flex items-center gap-2">
        <Button onClick={onNewBackup} size="sm">
          <Plus className="size-4" />
          新建备份
        </Button>
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
    </div>
  )
}

function NewBackupDialog({
  open,
  onOpenChange,
  onSubmit,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  onSubmit: (jobId: string) => Promise<void>
}) {
  const [jobId, setJobId] = useState("")
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const trimmed = jobId.trim()
    if (!trimmed) {
      toast.error("请输入 job_id")
      return
    }
    setSubmitting(true)
    try {
      await onSubmit(trimmed)
      setJobId("")
      onOpenChange(false)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>新建网盘备份</DialogTitle>
          <DialogDescription>
            输入要归档的任务 ID。任务必须是当前管理员所属 + 状态为
            <code className="mx-1 rounded bg-muted px-1 py-0.5 text-xs">
              succeeded
            </code>
            。任务工程会被打包为 tar.gz 上传到你的百度网盘
            <code className="mx-1 rounded bg-muted px-1 py-0.5 text-xs">
              /apps/AIVideoTrans/backups/
            </code>
            目录,完成后任务状态变为「已归档」且本地原文件被删除。
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label
              htmlFor="job_id_input"
              className="mb-1 block text-sm font-medium"
            >
              Job ID
            </label>
            <Input
              id="job_id_input"
              value={jobId}
              onChange={(e) => setJobId(e.target.value)}
              placeholder="例如: j_4a2b8e..."
              autoFocus
              disabled={submitting}
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={submitting}
            >
              取消
            </Button>
            <Button type="submit" disabled={submitting || !jobId.trim()}>
              {submitting && <Loader2 className="size-4 animate-spin" />}
              入队备份
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function FilterBar({
  statusFilter,
  onStatusChange,
  jobIdFilter,
  onJobIdChange,
}: {
  statusFilter: BackupRecordStatus | "all"
  onStatusChange: (v: BackupRecordStatus | "all") => void
  jobIdFilter: string
  onJobIdChange: (v: string) => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <div className="flex items-center gap-2">
        <Filter className="size-4 text-muted-foreground" />
        <span className="text-sm text-muted-foreground">过滤:</span>
      </div>
      <Select
        value={statusFilter}
        onValueChange={(v) => onStatusChange(v as BackupRecordStatus | "all")}
      >
        <SelectTrigger className="w-[140px]">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {STATUS_FILTER_OPTIONS.map((opt) => (
            <SelectItem key={opt.value} value={opt.value}>
              {opt.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Input
        placeholder="按 job_id 搜索 (部分匹配)"
        value={jobIdFilter}
        onChange={(e) => onJobIdChange(e.target.value)}
        className="w-[260px]"
      />
    </div>
  )
}

function BackupTable({
  rows,
  onRestore,
  onDelete,
  onManifest,
}: {
  rows: BackupRecord[]
  onRestore: (r: BackupRecord) => void
  onDelete: (r: BackupRecord) => void
  onManifest: (r: BackupRecord) => void
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="border-b text-xs uppercase text-muted-foreground">
          <tr>
            <th className="px-4 py-2 text-left font-medium">Job</th>
            <th className="px-4 py-2 text-left font-medium">状态</th>
            <th className="px-4 py-2 text-right font-medium">大小</th>
            <th className="px-4 py-2 text-left font-medium">完成时间</th>
            <th className="px-4 py-2 text-left font-medium">远端路径</th>
            <th className="px-4 py-2 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <BackupRow
              key={row.id}
              row={row}
              onRestore={onRestore}
              onDelete={onDelete}
              onManifest={onManifest}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function BackupRow({
  row,
  onRestore,
  onDelete,
  onManifest,
}: {
  row: BackupRecord
  onRestore: (r: BackupRecord) => void
  onDelete: (r: BackupRecord) => void
  onManifest: (r: BackupRecord) => void
}) {
  const status = (row.status ?? "uploaded") as keyof typeof BACKUP_STATUS_TONE
  const tone = BACKUP_STATUS_TONE[status] ?? "muted"
  const label = BACKUP_STATUS_LABEL[status] ?? row.status

  const badgeClass =
    tone === "success"
      ? "bg-green-500/10 text-green-700 dark:text-green-400"
      : tone === "danger"
        ? "bg-destructive/10 text-destructive"
        : tone === "active"
          ? "bg-amber-500/10 text-amber-700 dark:text-amber-400"
          : tone === "info"
            ? "bg-primary/10 text-primary"
            : "bg-muted text-muted-foreground"

  // Action availability:
  // - Restore: only when uploaded (the canonical post-backup state).
  // - Delete: anything that's NOT currently in flight (uploading / restoring).
  // - Manifest: always except deleted.
  const canRestore = row.status === "uploaded"
  const inFlight = row.status === "uploading" || row.status === "restoring"
  const canDelete = !inFlight && row.status !== "deleted"
  const canManifest = row.status !== "deleted"

  return (
    <tr
      className={`border-b last:border-b-0 hover:bg-muted/30 ${
        row.status === "deleted" ? "opacity-50" : ""
      }`}
    >
      <td className="px-4 py-3">
        <Link
          href={`/workspace/${row.job_id}`}
          className="font-mono text-xs hover:underline"
        >
          {row.job_id}
        </Link>
        <div className="text-xs text-muted-foreground">
          gen {row.job_edit_generation}
        </div>
      </td>
      <td className="px-4 py-3">
        <span
          className={`inline-block rounded px-2 py-0.5 text-xs ${badgeClass}`}
        >
          {label}
        </span>
        {row.error_message && (
          <div
            className="mt-1 max-w-xs truncate text-xs text-destructive"
            title={row.error_message}
          >
            {row.error_message}
          </div>
        )}
      </td>
      <td className="px-4 py-3 text-right font-mono text-xs">
        {formatBytesGB(row.size_bytes)}
      </td>
      <td className="px-4 py-3 text-xs">
        {formatTimestamp(row.completed_at || row.created_at)}
      </td>
      <td className="px-4 py-3 text-xs">
        <code
          className="block max-w-[280px] truncate text-muted-foreground"
          title={row.remote_path}
        >
          {row.remote_path || "—"}
        </code>
      </td>
      <td className="px-4 py-3">
        <div className="flex items-center justify-end gap-1">
          {canManifest && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onManifest(row)}
              title="查看 manifest"
            >
              <Eye className="size-4" />
            </Button>
          )}
          {canRestore && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => onRestore(row)}
              title="从网盘恢复到本地"
            >
              <RotateCcw className="size-4" />
              恢复
            </Button>
          )}
          {canDelete && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onDelete(row)}
              title="删除该备份"
            >
              <Trash2 className="size-4 text-destructive" />
            </Button>
          )}
        </div>
      </td>
    </tr>
  )
}

function Pagination({
  page,
  totalPages,
  onPageChange,
  disabled,
}: {
  page: number
  totalPages: number
  onPageChange: (p: number) => void
  disabled?: boolean
}) {
  return (
    <div className="flex items-center justify-end gap-2">
      <Button
        variant="outline"
        size="sm"
        onClick={() => onPageChange(Math.max(0, page - 1))}
        disabled={disabled || page === 0}
      >
        <ArrowLeft className="size-4" />
        上一页
      </Button>
      <span className="text-sm text-muted-foreground">
        第 {page + 1} / {totalPages} 页
      </span>
      <Button
        variant="outline"
        size="sm"
        onClick={() => onPageChange(Math.min(totalPages - 1, page + 1))}
        disabled={disabled || page >= totalPages - 1}
      >
        下一页
        <ArrowRight className="size-4" />
      </Button>
    </div>
  )
}

function ManifestDialog({
  open,
  onOpenChange,
  backup,
  manifest,
  loading,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  backup: BackupRecord | null
  manifest: BackupManifest | null
  loading: boolean
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[80vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            Manifest · {backup?.job_id ?? "—"}
          </DialogTitle>
          <DialogDescription>
            备份包内 manifest.json 的解析结果。文件列表由 tar 内部为准
            (PG 行可能被人工编辑过)。
          </DialogDescription>
        </DialogHeader>
        {loading && (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="size-6 animate-spin text-muted-foreground" />
          </div>
        )}
        {manifest && (
          <div className="space-y-4">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <div>
                <dt className="text-xs text-muted-foreground">format ver</dt>
                <dd>{manifest.backup_format_version}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">edit_gen</dt>
                <dd>{manifest.edit_generation}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">user_id</dt>
                <dd className="truncate font-mono text-xs">
                  {manifest.user_id}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">备份时间</dt>
                <dd>{formatTimestamp(manifest.created_at)}</dd>
              </div>
            </dl>
            <div>
              <h4 className="mb-2 text-sm font-medium">
                文件清单 ({manifest.file_inventory?.length || 0})
              </h4>
              <div className="max-h-80 overflow-y-auto rounded border">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 border-b bg-card">
                    <tr>
                      <th className="px-3 py-1.5 text-left font-medium">
                        路径
                      </th>
                      <th className="px-3 py-1.5 text-right font-medium">
                        大小
                      </th>
                      <th className="px-3 py-1.5 text-left font-medium">
                        sha256
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {(manifest.file_inventory ?? []).map((f, i) => (
                      <tr key={i} className="border-b last:border-b-0">
                        <td className="px-3 py-1.5 font-mono">{f.relpath}</td>
                        <td className="px-3 py-1.5 text-right font-mono">
                          {(f.size / 1024).toFixed(0)} KB
                        </td>
                        <td className="px-3 py-1.5 font-mono text-muted-foreground">
                          {f.sha256.slice(0, 16)}…
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
            {manifest.r2_artifacts?.length > 0 && (
              <div>
                <h4 className="mb-2 text-sm font-medium">
                  R2 制品快照 ({manifest.r2_artifacts.length})
                </h4>
                <ul className="space-y-1 text-xs">
                  {manifest.r2_artifacts.map((a, i) => (
                    <li key={i} className="font-mono text-muted-foreground">
                      [{a.artifact_key}] {a.r2_key}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-center text-sm text-muted-foreground">
      <p>没有符合条件的备份记录。</p>
      <p className="text-xs">
        清除过滤条件后再次查看,或前往任务列表手动触发一次备份。
      </p>
    </div>
  )
}

function TableSkeleton() {
  return (
    <div className="divide-y">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="flex items-center gap-4 px-4 py-3">
          <div className="h-4 w-32 animate-pulse rounded bg-muted" />
          <div className="h-4 w-16 animate-pulse rounded bg-muted/60" />
          <div className="h-4 w-20 animate-pulse rounded bg-muted/60" />
          <div className="ml-auto h-8 w-20 animate-pulse rounded bg-muted/40" />
        </div>
      ))}
    </div>
  )
}
