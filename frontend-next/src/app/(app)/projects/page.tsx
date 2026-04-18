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
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
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
import { listJobs } from "@/lib/api/jobs"
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

  // ---- Derived state ----

  const activeTask = selectActiveTaskJob(jobs)
  const sorted = [...jobs].sort(
    (a, b) =>
      new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime(),
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
        {sorted.map((job) => (
          <ProjectCard
            key={job.id}
            job={job}
            isExpanded={expanded.has(job.id)}
            onToggle={() => toggleExpand(job.id)}
            onDelete={() => void handleDelete(job)}
            onCancel={() => void handleCancel(job)}
            onReCreate={() => handleReCreate(job)}
            isDeleting={deletingId === job.id}
            isCancelling={cancellingId === job.id}
          />
        ))}
      </div>

      <NewTranslationDialog
        open={dialogOpen}
        onOpenChange={handleDialogChange}
        onJobCreated={handleJobCreated}
        initialSourceUrl={dialogInitialUrl}
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
  isExpanded,
  onToggle,
  onDelete,
  onCancel,
  onReCreate,
  isDeleting,
  isCancelling,
}: {
  job: JobSummary
  isExpanded: boolean
  onToggle: () => void
  onDelete: () => void
  onCancel: () => void
  onReCreate: () => void
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
          <div className="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
            <span>{timeLabel(job.updatedAt)}</span>
            <span className={expiryColorClass(expiry.tier)}>
              {expiryLabel(expiry)}
            </span>
          </div>
        </div>

        {/* Collapse-level actions */}
        <div
          className="flex items-center gap-1.5 shrink-0"
          onClick={(e) => e.stopPropagation()}
        >
          {showEditShortcut && (
            // D43: 任务卡右上角"修改"直达按钮。仅 Studio + succeeded + feature flag
            // 启用时渲染；Phase 1 T1-3 落地 /workspace/{id}/edit 路由后即可跳转。
            <Link
              href={`/workspace/${job.id}/edit`}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-background/60 px-2 py-1 text-xs text-foreground transition hover:bg-accent/40"
              title="修改此任务"
              onClick={(e) => e.stopPropagation()}
            >
              <RefreshCw className="h-3 w-3" />
              修改
            </Link>
          )}
          <CardActions
            job={job}
            collapsed
            onDelete={onDelete}
            onCancel={onCancel}
            onReCreate={onReCreate}
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
  switch (job.status) {
    case "succeeded":
      return <ResultMediaCard jobId={job.id} serviceMode={job.serviceMode} />

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
          <div className="flex items-center gap-2 rounded-lg bg-amber-500/10 px-4 py-3 text-sm text-amber-400">
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
          <div className="flex items-center gap-2 rounded-lg bg-violet-500/10 px-4 py-3 text-sm text-violet-400">
            <RefreshCw className="h-4 w-4 shrink-0" />
            你正在修改此任务
            {(job.editGeneration ?? 0) > 0 && (
              <span className="text-xs text-violet-300/80">
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
          <div className="flex items-center gap-2 rounded-lg bg-red-500/10 px-4 py-3 text-sm text-red-400">
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
  isDeleting,
  isCancelling,
}: {
  job: JobSummary
  collapsed: boolean
  onDelete: () => void
  onCancel: () => void
  onReCreate: () => void
  isDeleting: boolean
  isCancelling: boolean
}) {
  const router = useRouter()

  // For collapsed cards, show contextual action + delete
  if (collapsed) {
    switch (job.status) {
      case "succeeded":
      case "cancelled":
      case "failed":
        return (
          <Button
            variant="ghost"
            size="icon-xs"
            onClick={onDelete}
            disabled={isDeleting}
            title="删除"
          >
            <Trash2 className="h-3.5 w-3.5 text-muted-foreground hover:text-red-400" />
          </Button>
        )

      case "running":
      case "queued":
      case "waiting_for_review":
      case "editing":
        // Actions are shown in the expanded area (see ExpandedContent).
        // editing explicitly routes its own confirm flow through the edit
        // page — no collapsed-level action here.
        return null

      default:
        return null
    }
  }

  return null
}
