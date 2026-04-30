"use client"

import { Suspense, useCallback, useEffect, useRef, useState } from "react"
import Link from "next/link"
import { useRouter, useSearchParams } from "next/navigation"
import { toast } from "sonner"
import {
  Plus,
  Play,
  AlertTriangle,
  Clock,
  Trash2,
  ExternalLink,
  ChevronDown,
  ChevronRight,
  XCircle,
  RefreshCw,
  Pencil,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { StatusBadge } from "@/components/status-badge"
import { NewTranslationDialog } from "@/components/workspace/NewTranslationDialog"
import { ResultMediaCard } from "@/components/workspace/ResultMediaCard"
import { selectActiveTaskJob } from "@/features/jobs/selectors"
import {
  getJobDisplayTitle,
  getStageLabel,
  getUserFacingProgressMessage,
} from "@/features/jobs/presentation"
import { computeExpiryInfo, expiryColorClass, expiryLabel } from "@/features/jobs/expiry"
import { listJobs, renameJob } from "@/lib/api/jobs"
import { cancelJob, deleteJob } from "@/lib/api/reviews"
import { ACTIVE_JOB_STATUSES, type JobSummary, type JobStatus } from "@/types/jobs"

// Feature flag gating the post-edit workflow UI (plan D29). Both the frontend
// entry points and the backend endpoints are gated; this flag defaults to
// disabled so Phase 0 ships without exposing any Phase 1 surface to end users.
const POST_EDIT_ENABLED = process.env.NEXT_PUBLIC_ENABLE_POST_EDIT === "1"

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const POLL_INTERVAL = 4_000
const MAX_AUTO_EXPAND = 3

// editing is active (user-held session); list page must keep polling so the
// UI reflects idle auto-cancel, admin force-cancel, and commit transitions.
const POLL_STATUSES: readonly JobStatus[] = ACTIVE_JOB_STATUSES

// Legacy days-remaining helper. Kept only for any stray callers that have not
// migrated to computeExpiryInfo; new code should use the tiered helper in
// @/features/jobs/expiry instead (plan D12). Can be removed once no other
// call sites reference it.
function daysRemaining(updatedAt: string): number {
  const elapsed = Date.now() - new Date(updatedAt).getTime()
  return Math.max(0, 7 - Math.floor(elapsed / (1000 * 60 * 60 * 24)))
}
void daysRemaining  // keep reference to avoid unused-import-style lint noise

function timeLabel(iso: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(iso))
}

function cardTimestamp(job: JobSummary): string {
  if ((job.status === "succeeded" || job.status === "purged") && job.completedAt) {
    return job.completedAt
  }
  return job.updatedAt
}

/** Pick at most MAX_AUTO_EXPAND jobs to auto-expand. */
function computeDefaultExpanded(jobs: JobSummary[]): Set<string> {
  // Expand all jobs by default. Lazy loading inside ResultMediaCard
  // (native <img loading="lazy">) ensures posters only fetch on scroll.
  return new Set(jobs.map((j) => j.id))
}

// ---------------------------------------------------------------------------
// Page component
// ---------------------------------------------------------------------------

export default function MyProjectsPage() {
  return (
    <Suspense>
      <ProjectsContent />
    </Suspense>
  )
}

function ProjectsContent() {
  const router = useRouter()
  const searchParams = useSearchParams()

  const [jobs, setJobs] = useState<JobSummary[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [cancellingId, setCancellingId] = useState<string | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [dialogInitialUrl, setDialogInitialUrl] = useState<string | undefined>(undefined)
  // Rename dialog state (plan §6.5 / D16). Tracks the target job + its
  // pre-fill title so the Modal can open over any card without re-sorting.
  const [renamingJob, setRenamingJob] = useState<JobSummary | null>(null)
  const [renameSubmitting, setRenameSubmitting] = useState(false)

  const initialExpandDone = useRef(false)
  const prevJobIdsRef = useRef<Set<string>>(new Set())

  // ---- Data loading ----

  const loadJobs = useCallback(async () => {
    try {
      const data = await listJobs()
      setJobs(data)
      setLoadError(null)
      if (!initialExpandDone.current) {
        // First load: apply full default expand rules
        initialExpandDone.current = true
        setExpanded(computeDefaultExpanded(data))
      } else {
        // Subsequent loads: only auto-expand truly NEW active jobs
        // Uses ref to avoid stale closure over `jobs` state
        setExpanded(prev => {
          const next = new Set(prev)
          for (const j of data) {
            if (
              ACTIVE_JOB_STATUSES.includes(j.status) &&
              !prevJobIdsRef.current.has(j.id)
            ) {
              next.add(j.id)
            }
          }
          return next
        })
      }
      // Update ref for next comparison
      prevJobIdsRef.current = new Set(data.map(j => j.id))
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "加载项目列表失败")
    } finally {
      setIsLoading(false)
    }
  }, [])

  // Initial load
  useEffect(() => {
    void loadJobs()
  }, [loadJobs])

  // Polling
  useEffect(() => {
    const needsPoll = jobs.some((j) => POLL_STATUSES.includes(j.status))
    if (!needsPoll) return
    const timer = setInterval(() => void loadJobs(), POLL_INTERVAL)
    return () => clearInterval(timer)
  }, [jobs, loadJobs])

  // ?new=1 auto-open
  useEffect(() => {
    if (searchParams.get("new") === "1") {
      setDialogOpen(true)
    }
  }, [searchParams])

  // ---- Handlers ----

  const handleDialogChange = useCallback(
    (open: boolean) => {
      setDialogOpen(open)
      if (!open) {
        // Remove ?new=1 from URL
        const params = new URLSearchParams(searchParams.toString())
        params.delete("new")
        const qs = params.toString()
        router.replace(qs ? `/projects?${qs}` : "/projects")
        void loadJobs()
      }
    },
    [router, searchParams, loadJobs],
  )

  const handleJobCreated = useCallback(() => {
    void loadJobs()
  }, [loadJobs])

  const toggleExpand = useCallback((jobId: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(jobId)) next.delete(jobId)
      else next.add(jobId)
      return next
    })
  }, [])

  const handleDelete = useCallback(
    async (job: JobSummary) => {
      if (!window.confirm("确定删除这个项目？")) return
      setDeletingId(job.id)
      try {
        await deleteJob(job.id)
        setJobs((prev) => prev.filter((j) => j.id !== job.id))
        toast.success("项目已删除")
      } catch {
        toast.error("删除失败，请稍后重试")
      } finally {
        setDeletingId(null)
      }
    },
    [],
  )

  const handleCancel = useCallback(
    async (job: JobSummary) => {
      setCancellingId(job.id)
      try {
        await cancelJob(job.id)
        toast.success("任务已取消")
        void loadJobs()
      } catch {
        toast.error("取消失败，请稍后重试")
      } finally {
        setCancellingId(null)
      }
    },
    [loadJobs],
  )

  const handleReCreate = useCallback((job: JobSummary) => {
    // Pre-fill URL for youtube sources; local_video can't be recovered
    const url = job.sourceType === "youtube_url" ? (job.sourceRef ?? undefined) : undefined
    setDialogInitialUrl(url)
    setDialogOpen(true)
  }, [])

  const handleRenameOpen = useCallback((job: JobSummary) => {
    setRenamingJob(job)
  }, [])

  const handleRenameConfirm = useCallback(
    async (newName: string) => {
      if (!renamingJob) return
      setRenameSubmitting(true)
      try {
        const updated = await renameJob(renamingJob.id, newName)
        // Patch the one row in-place; avoid refetching the whole list
        // just for a cosmetic change.
        setJobs((prev) => prev.map((j) => (j.id === updated.id ? updated : j)))
        toast.success(
          updated.title === newName.trim()
            ? "任务已重命名"
            : `任务已重命名为 "${updated.title}"`, // collision suffix applied
        )
        setRenamingJob(null)
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "重命名失败")
      } finally {
        setRenameSubmitting(false)
      }
    },
    [renamingJob],
  )

  // ---- Derived state ----

  const activeTask = selectActiveTaskJob(jobs)
  const sorted = [...jobs].sort(
    (a, b) =>
      new Date(cardTimestamp(b)).getTime() - new Date(cardTimestamp(a)).getTime(),
  )

  // ---- Loading state ----

  if (isLoading) {
    return (
      <div className="space-y-6">
        <PageHeader
          activeTask={null}
          onNewClick={() => { setDialogInitialUrl(undefined); setDialogOpen(true) }}
        />
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="h-20 animate-pulse rounded-xl bg-muted/30"
            />
          ))}
        </div>
      </div>
    )
  }

  // ---- Error state ----

  if (loadError && jobs.length === 0) {
    return (
      <div className="space-y-6">
        <PageHeader
          activeTask={null}
          onNewClick={() => { setDialogInitialUrl(undefined); setDialogOpen(true) }}
        />
        <div className="max-w-md mx-auto mt-12 rounded-2xl border border-red-200 bg-red-50 dark:border-red-500/20 dark:bg-red-500/5 p-8 text-center space-y-4">
          <p className="text-sm font-medium text-red-600 dark:text-red-400">{loadError}</p>
          <Button variant="outline" size="sm" onClick={() => { setIsLoading(true); loadJobs() }}>
            重试
          </Button>
        </div>
        <NewTranslationDialog
          open={dialogOpen}
          onOpenChange={(v) => { setDialogOpen(v); if (!v) setDialogInitialUrl(undefined) }}
          onJobCreated={() => loadJobs()}
          initialSourceUrl={dialogInitialUrl}
        />
      </div>
    )
  }

  // ---- Empty state ----

  if (jobs.length === 0) {
    return (
      <div className="space-y-6">
        <PageHeader
          activeTask={null}
          onNewClick={() => { setDialogInitialUrl(undefined); setDialogOpen(true) }}
        />
        <div className="max-w-md mx-auto mt-12 rounded-2xl border border-border bg-card p-8 text-center space-y-4">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted">
            <Play className="h-6 w-6 text-muted-foreground" />
          </div>
          <h2 className="text-xl font-semibold text-foreground">
            还没有翻译任务
          </h2>
          <p className="text-sm text-muted-foreground">
            创建第一个翻译任务，开始体验视频翻译。
          </p>
          <Button onClick={() => setDialogOpen(true)} className="mt-2">
            <Plus className="h-4 w-4 mr-1" />
            新建翻译
          </Button>
        </div>

        <NewTranslationDialog
          open={dialogOpen}
          onOpenChange={handleDialogChange}
          onJobCreated={handleJobCreated}
        />
      </div>
    )
  }

  // ---- Main list ----

  return (
    <div className="space-y-6">
      <PageHeader
        activeTask={activeTask}
        onNewClick={() => { setDialogInitialUrl(undefined); setDialogOpen(true) }}
      />

      {/* Retention notice */}
      <div className="rounded-xl border border-amber-500/20 bg-amber-500/5 px-4 py-3 flex items-center gap-3">
        <Clock className="h-5 w-5 text-amber-400 shrink-0" />
        <p className="text-sm text-amber-400/90">
          每个项目最长保留 <strong>7 天</strong>，过期后自动删除。请及时下载结果文件。
        </p>
      </div>

      <div className="grid gap-3 grid-cols-1 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
        {sorted.map((job) => {
          // D19: 副本卡片标题下显示 "· 派生自 <源名>"。源 job 多半在同一
          // 用户的 list 里 — 直接从 `jobs` 里查一次即可；O(n) per card 对
          // <100 条任务没压力。源已被 7d TTL 清理时 sourceJob 为 undefined,
          // UI 会回退为 "· 副本"（仅标记派生，不挂悬空的源名）。
          const sourceJob = job.copyOfJobId
            ? jobs.find((j) => j.id === job.copyOfJobId)
            : null
          const sourceTitle = sourceJob ? getJobDisplayTitle(sourceJob) : null
          return (
            <ProjectCard
              key={job.id}
              job={job}
              sourceTitle={sourceTitle}
              isExpanded={expanded.has(job.id)}
              onToggle={() => toggleExpand(job.id)}
              onDelete={() => void handleDelete(job)}
              onCancel={() => void handleCancel(job)}
              onReCreate={() => handleReCreate(job)}
              onRename={() => handleRenameOpen(job)}
              isDeleting={deletingId === job.id}
              isCancelling={cancellingId === job.id}
            />
          )
        })}
      </div>

      <NewTranslationDialog
        open={dialogOpen}
        onOpenChange={handleDialogChange}
        onJobCreated={handleJobCreated}
        initialSourceUrl={dialogInitialUrl}
      />

      <RenameJobDialog
        job={renamingJob}
        submitting={renameSubmitting}
        onConfirm={handleRenameConfirm}
        onCancel={() => setRenamingJob(null)}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// PageHeader
// ---------------------------------------------------------------------------

function PageHeader({
  activeTask,
  onNewClick,
}: {
  activeTask: JobSummary | null
  onNewClick: () => void
}) {
  const router = useRouter()
  const hasWaiting = activeTask?.status === "waiting_for_review"

  return (
    <div className="flex items-center justify-between gap-3">
      <h1 className="text-2xl font-bold font-heading text-foreground">
        视频翻译
      </h1>

      <div className="flex items-center gap-2">
        {activeTask && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => router.push(`/workspace/${activeTask.id}`)}
            className="relative"
          >
            <ExternalLink className="h-3.5 w-3.5 mr-1" />
            当前任务
            {hasWaiting && (
              <span className="absolute -top-1 -right-1 h-2.5 w-2.5 rounded-full bg-red-500" />
            )}
          </Button>
        )}
        <Button size="sm" onClick={onNewClick}>
          <Plus className="h-3.5 w-3.5 mr-1" />
          新建翻译
        </Button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// ProjectCard
// ---------------------------------------------------------------------------

function ProjectCard({
  job,
  sourceTitle,
  isExpanded,
  onToggle,
  onDelete,
  onCancel,
  onReCreate,
  onRename,
  isDeleting,
  isCancelling,
}: {
  job: JobSummary
  /** D19: 显示"派生自 <源名>"时的源 job 标题。null = 非副本或源已被清理。 */
  sourceTitle: string | null
  isExpanded: boolean
  onToggle: () => void
  onDelete: () => void
  onCancel: () => void
  onReCreate: () => void
  onRename: () => void
  isDeleting: boolean
  isCancelling: boolean
}) {
  const router = useRouter()
  const expiry = computeExpiryInfo(job)
  const showEditShortcut =
    POST_EDIT_ENABLED && job.serviceMode === "studio" && job.status === "succeeded"

  return (
    <Card size="sm" className="overflow-visible">
      {/* Card header — clickable to expand/collapse */}
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-muted/30"
      >
        {isExpanded ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
        )}

        <div className="min-w-0 flex-1 space-y-0.5">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-foreground truncate">
              {getJobDisplayTitle(job)}
            </span>
            <StatusBadge status={job.status} editGeneration={job.editGeneration ?? 0} />
          </div>
          {/* D19: 副本派生关系小字标识。不可点（plan 明确：仅标记，不做 tree
           *   视图）；源已被 7d TTL 清走时 fallback 成通用 "· 副本"。 */}
          {job.copyOfJobId && (
            <div className="text-[11px] text-muted-foreground truncate">
              · 派生自 {sourceTitle ?? "已清理的任务"}
            </div>
          )}
          <div className="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
            <span>{timeLabel(cardTimestamp(job))}</span>
            <span className={expiryColorClass(expiry.tier)}>
              {expiryLabel(expiry)}
            </span>
          </div>
        </div>

        {/* Collapse-level actions — note: 修改 button used to live here too,
            but the user reported the top row was too crowded. It now renders
            inside <ResultMediaCard /> at the right end of the download row
            for succeeded jobs. See ExpandedContent below. */}
        <div
          className="flex items-center gap-1.5 shrink-0"
          onClick={(e) => e.stopPropagation()}
        >
          <CardActions
            job={job}
            collapsed
            onDelete={onDelete}
            onCancel={onCancel}
            onReCreate={onReCreate}
            onRename={onRename}
            isDeleting={isDeleting}
            isCancelling={isCancelling}
          />
        </div>
      </button>

      {/* Expanded content */}
      {isExpanded && (
        <CardContent className="pt-0 pb-4">
          <ExpandedContent
            job={job}
            onCancel={onCancel}
            onReCreate={onReCreate}
            isCancelling={isCancelling}
          />
        </CardContent>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// ExpandedContent — different for each status
// ---------------------------------------------------------------------------

function ExpandedContent({
  job,
  onCancel,
  onReCreate,
  isCancelling,
}: {
  job: JobSummary
  onCancel: () => void
  onReCreate: () => void
  isCancelling: boolean
}) {
  // Edit shortcut is gated on the same predicate that the collapsed-row
  // shortcut used (Studio + succeeded + post-edit feature flag). Passing
  // undefined when ineligible keeps ResultMediaCard from rendering the button.
  const editShortcutHref =
    POST_EDIT_ENABLED && job.serviceMode === "studio" && job.status === "succeeded"
      ? `/workspace/${job.id}/edit`
      : undefined

  switch (job.status) {
    case "succeeded":
      return (
        <ResultMediaCard
          jobId={job.id}
          serviceMode={job.serviceMode}
          editHref={editShortcutHref}
        />
      )

    case "running":
      return (
        <div className="space-y-3">
          <div className="rounded-lg bg-muted/30 px-4 py-3 space-y-1">
            <div className="flex items-center gap-2 text-sm">
              <span className="text-muted-foreground">阶段:</span>
              <span className="font-medium text-foreground">
                {getStageLabel(job.currentStage)}
              </span>
            </div>
            {job.progressMessage && (
              <p className="text-xs text-muted-foreground">
                {getUserFacingProgressMessage(job.progressMessage) ??
                  job.progressMessage}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            <a
              href={`/workspace/${job.id}`}
              className="inline-flex items-center gap-1 rounded-lg bg-primary px-2.5 py-1.5 text-[0.8rem] font-medium text-primary-foreground transition hover:bg-primary/80"
            >
              <ExternalLink className="h-3.5 w-3.5" />
              进入工作台
            </a>
            <Button
              variant="ghost"
              size="sm"
              onClick={onCancel}
              disabled={isCancelling}
            >
              <XCircle className="h-3.5 w-3.5 mr-1" />
              {isCancelling ? "取消中..." : "取消"}
            </Button>
          </div>
        </div>
      )

    case "waiting_for_review":
      return (
        <div className="space-y-3">
          {/* Ochre soft banner — same family as the StatusBadge ochre pill,
              consistent with the ink palette. Was bg-amber-500/10 hardcoded. */}
          <div
            className="flex items-center gap-2 rounded-lg px-4 py-3 text-sm"
            style={{
              backgroundColor: "color-mix(in oklab, var(--ochre) 12%, transparent)",
              color: "var(--ochre)",
              border: "1px solid color-mix(in oklab, var(--ochre) 30%, transparent)",
            }}
          >
            <AlertTriangle className="h-4 w-4 shrink-0" />
            需要审核
          </div>
          <Link href={`/workspace/${job.id}`}>
            <Button size="sm">
              <ExternalLink className="h-3.5 w-3.5 mr-1" />
              进入工作台
            </Button>
          </Link>
        </div>
      )

    case "editing":
      // Plan D18 + §11.6. "editing" is a user-owned session — user either
      // resumes ("继续修改") or abandons via the edit page. We do NOT offer
      // a card-level cancel button: cancelling editing is destructive (drops
      // user draft) and must route through the edit page's own二次确认 flow.
      return (
        <div className="space-y-3">
          {/* Ochre soft banner matches the StatusBadge ochre pill for editing.
              Was bg-violet-500/10 hardcoded. */}
          <div
            className="flex items-center gap-2 rounded-lg px-4 py-3 text-sm"
            style={{
              backgroundColor: "color-mix(in oklab, var(--ochre) 12%, transparent)",
              color: "var(--ochre)",
              border: "1px solid color-mix(in oklab, var(--ochre) 30%, transparent)",
            }}
          >
            <RefreshCw className="h-4 w-4 shrink-0" />
            你正在修改此任务
            {(job.editGeneration ?? 0) > 0 && (
              <span className="text-xs opacity-75">
                · 已完成 {job.editGeneration} 次修改
              </span>
            )}
          </div>
          <Link href={`/workspace/${job.id}/edit`}>
            <Button size="sm">
              <ExternalLink className="h-3.5 w-3.5 mr-1" />
              继续修改
            </Button>
          </Link>
        </div>
      )

    case "failed":
      return (
        <div className="space-y-3">
          {/* Cinnabar soft banner matches the StatusBadge failed pill.
              Was bg-red-500/10 hardcoded. */}
          <div
            className="flex items-center gap-2 rounded-lg px-4 py-3 text-sm"
            style={{
              backgroundColor: "color-mix(in oklab, var(--cinnabar) 12%, transparent)",
              color: "var(--cinnabar)",
              border: "1px solid color-mix(in oklab, var(--cinnabar) 30%, transparent)",
            }}
          >
            <AlertTriangle className="h-4 w-4 shrink-0" />
            {getUserFacingProgressMessage(job.progressMessage) ?? "处理失败"}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={onReCreate}>
              <RefreshCw className="h-3.5 w-3.5 mr-1" />
              重新创建
            </Button>
          </div>
        </div>
      )

    case "queued":
      return (
        <div className="space-y-3">
          <div className="flex items-center gap-2 rounded-lg bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
            <Clock className="h-4 w-4 shrink-0" />
            排队中...
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={onCancel}
            disabled={isCancelling}
          >
            <XCircle className="h-3.5 w-3.5 mr-1" />
            {isCancelling ? "取消中..." : "取消"}
          </Button>
        </div>
      )

    case "cancelled":
      return (
        <div className="flex items-center gap-2 rounded-lg bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
          <XCircle className="h-4 w-4 shrink-0" />
          已取消
        </div>
      )

    default:
      return null
  }
}

// ---------------------------------------------------------------------------
// CardActions — shown in the card header row
// ---------------------------------------------------------------------------

function CardActions({
  job,
  collapsed,
  onDelete,
  onCancel,
  onReCreate,
  onRename,
  isDeleting,
  isCancelling,
}: {
  job: JobSummary
  collapsed: boolean
  onDelete: () => void
  onCancel: () => void
  onReCreate: () => void
  onRename: () => void
  isDeleting: boolean
  isCancelling: boolean
}) {
  const router = useRouter()

  // For collapsed cards, show contextual action + delete
  if (collapsed) {
    // Rename is always available — a user may rename at any stage. Shown
    // as an inline pencil icon next to the delete button; we skipped the
    // full "..." dropdown for visual simplicity (§6.5 allows either).
    const renameBtn = (
      <Button
        variant="ghost"
        size="icon-xs"
        onClick={onRename}
        title="重命名"
      >
        <Pencil className="h-3.5 w-3.5 text-muted-foreground hover:text-primary" />
      </Button>
    )

    switch (job.status) {
      case "succeeded":
      case "cancelled":
      case "failed":
      case "purged":
        return (
          <>
            {renameBtn}
            <Button
              variant="ghost"
              size="icon-xs"
              onClick={onDelete}
              disabled={isDeleting}
              title="删除"
            >
              <Trash2 className="h-3.5 w-3.5 text-muted-foreground hover:text-red-400" />
            </Button>
          </>
        )

      case "running":
      case "queued":
      case "waiting_for_review":
      case "editing":
        // Rename still works for in-flight jobs — users often name things
        // mid-process once the video title comes in. Delete stays hidden
        // (the cancel CTA lives in ExpandedContent).
        return renameBtn

      default:
        return null
    }
  }

  return null
}

// ---------------------------------------------------------------------------
// RenameJobDialog — plan §6.5 rename modal
// ---------------------------------------------------------------------------
//
// Server-side validation is authoritative (gateway rejects empty / too-long /
// forbidden-chars with 400). We mirror the cheapest checks here for inline
// feedback, but never enforce stricter rules than the backend does.
//
// Display-width upper bound is 29 (24 title + 5 collision suffix budget);
// we tell the user "约 12 中文字符" which matches the design spec §6.1.

const FORBIDDEN_RENAME_CHARS = /[<>"/\\\x00]/

function RenameJobDialog({
  job,
  submitting,
  onConfirm,
  onCancel,
}: {
  job: JobSummary | null
  submitting: boolean
  onConfirm: (newName: string) => void
  onCancel: () => void
}) {
  return (
    <Dialog open={!!job} onOpenChange={(open) => { if (!open) onCancel() }}>
      {job && (
        <RenameJobDialogForm
          key={job.id}
          job={job}
          submitting={submitting}
          onConfirm={onConfirm}
          onCancel={onCancel}
        />
      )}
    </Dialog>
  )
}

function RenameJobDialogForm({
  job,
  submitting,
  onConfirm,
  onCancel,
}: {
  job: JobSummary
  submitting: boolean
  onConfirm: (newName: string) => void
  onCancel: () => void
}) {
  const [value, setValue] = useState(() => getJobDisplayTitle(job))

  const trimmed = value.trim()
  const tooLong = trimmed.length > 60
  const hasBadChar = FORBIDDEN_RENAME_CHARS.test(trimmed)
  const canSubmit = !submitting && !!trimmed && !tooLong && !hasBadChar

  return (
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>重命名任务</DialogTitle>
          <DialogDescription>
            最多约 12 个中文字符。与已有任务重名时会自动追加随机后缀。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Label htmlFor="rename-input">新任务名</Label>
          <Input
            id="rename-input"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            disabled={submitting}
            onKeyDown={(e) => {
              if (e.key === "Enter" && canSubmit) {
                e.preventDefault()
                onConfirm(trimmed)
              }
            }}
            autoFocus
            maxLength={80}
          />
          {hasBadChar && (
            <p className="text-xs text-red-500">
              不能包含 {'<'} {'>'} 引号 / \ 或空字符
            </p>
          )}
          {tooLong && (
            <p className="text-xs text-red-500">名称过长（超过 60 个字符）</p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={submitting}>
            取消
          </Button>
          <Button
            onClick={() => canSubmit && onConfirm(trimmed)}
            disabled={!canSubmit}
          >
            {submitting ? "保存中…" : "保存"}
          </Button>
        </DialogFooter>
      </DialogContent>
  )
}
