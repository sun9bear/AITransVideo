"use client"

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useTranslations } from "next-intl"
import { useIntlLocale } from "@/lib/intl-locale"
import { Link } from "@/i18n/navigation"
import { useSearchParams } from "next/navigation"
import { useRouter } from "@/i18n/navigation"
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
  Cloud,
  X,
  Loader2,
} from "lucide-react"
import { enqueueBackupBatch } from "@/lib/api/pan"
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
import { SmartPreviewResultCard } from "@/components/workspace/SmartPreviewResultCard"
import { selectActiveTaskJob } from "@/features/jobs/selectors"
import {
  getJobDisplayTitle,
  getStageLabel,
  getUserFacingProgressMessage,
} from "@/features/jobs/presentation"
import { computeExpiryInfo, expiryColorClass, expiryLabel } from "@/features/jobs/expiry"
import { getEntitlements } from "@/lib/api/entitlements"
import { listJobsPage, renameJob } from "@/lib/api/jobs"
import { cancelJob, deleteJob } from "@/lib/api/reviews"
import { useApiErrorMessage } from "@/lib/api/error-localization"
import { usePollingTask } from "@/lib/react/usePollingTask"
import { ACTIVE_JOB_STATUSES, type JobSummary, type JobStatus } from "@/types/jobs"

// Feature flag gating the post-edit workflow UI (plan D29). Both the frontend
// entry points and the backend endpoints are gated; this flag defaults to
// disabled so Phase 0 ships without exposing any Phase 1 surface to end users.
const POST_EDIT_ENABLED = process.env.NEXT_PUBLIC_ENABLE_POST_EDIT === "1"

// Service modes the post-edit flow accepts. Backend source of truth lives in
// ``src/services/smart/state.py::EDITABLE_SERVICE_MODES`` and
// ``src/services/jobs/editing.py::enter_editing`` (which also runs a
// secondary ``is_editable_smart_state`` check for smart jobs — that one is
// handled server-side, so the frontend just needs to surface the entry).
// Master plan §6.2 ("Smart 成功交付后应允许进入 Studio post-edit 二次精修")
// is the source for adding "smart" here on 2026-05-16.
const EDITABLE_SERVICE_MODES: ReadonlySet<JobSummary["serviceMode"]> = new Set([
  "studio",
  "smart",
])

function isJobEditEligible(job: JobSummary): boolean {
  return (
    POST_EDIT_ENABLED &&
    // 智能版预览任务不可进编辑（后端 is_editable_smart_state 对 smart_preview_mode
    // 恒 403）。在 eligibility 单点排除，防未来任何 caller 误据此开出编辑入口。
    !job.smartPreviewMode &&
    EDITABLE_SERVICE_MODES.has(job.serviceMode) &&
    job.status === "succeeded"
  )
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const POLL_INTERVAL = 4_000
const MAX_AUTO_EXPAND = 3
const JOB_PAGE_SIZE = 20

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

function timeLabel(iso: string, formatLocale: string) {
  return new Intl.DateTimeFormat(formatLocale, {
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

function mergeJobPages(current: JobSummary[], incoming: JobSummary[]): JobSummary[] {
  const replacements = new Map(incoming.map((job) => [job.id, job]))
  const seen = new Set(current.map((job) => job.id))
  const merged = current.map((job) => replacements.get(job.id) ?? job)
  for (const job of incoming) {
    if (!seen.has(job.id)) {
      merged.push(job)
    }
  }
  return merged
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
  const t = useTranslations("app")
  const tp = useTranslations("appProjects")
  const localizeError = useApiErrorMessage()
  const router = useRouter()
  const searchParams = useSearchParams()

  const [jobs, setJobs] = useState<JobSummary[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isLoadingMore, setIsLoadingMore] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [totalJobs, setTotalJobs] = useState<number | null>(null)
  const [hasMoreJobs, setHasMoreJobs] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [cancellingId, setCancellingId] = useState<string | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [dialogInitialUrl, setDialogInitialUrl] = useState<string | undefined>(undefined)
  const [isAdminView, setIsAdminView] = useState(false)
  // Rename dialog state (plan §6.5 / D16). Tracks the target job + its
  // pre-fill title so the Modal can open over any card without re-sorting.
  const [renamingJob, setRenamingJob] = useState<JobSummary | null>(null)
  const [renameSubmitting, setRenameSubmitting] = useState(false)

  // Bulk pan-backup selection (admin only — UI gated below by isAdminView
  // and per-card checkboxes only show on status='succeeded' rows).
  const [selectedBackupIds, setSelectedBackupIds] = useState<Set<string>>(
    () => new Set(),
  )
  const [bulkBackingUp, setBulkBackingUp] = useState(false)

  const initialExpandDone = useRef(false)
  const prevJobIdsRef = useRef<Set<string>>(new Set())
  const jobsRef = useRef<JobSummary[]>([])
  const loadedCountRef = useRef(0)
  const loadingMoreRef = useRef(false)
  const loadMoreSentinelRef = useRef<HTMLDivElement | null>(null)

  // ---- Data loading ----

  const commitJobs = useCallback((nextJobs: JobSummary[]) => {
    jobsRef.current = nextJobs
    loadedCountRef.current = nextJobs.length
    prevJobIdsRef.current = new Set(nextJobs.map((j) => j.id))
    setJobs(nextJobs)
  }, [])

  const loadMoreJobs = useCallback(async () => {
    if (!hasMoreJobs || loadingMoreRef.current) return
    loadingMoreRef.current = true
    setIsLoadingMore(true)
    try {
      const page = await listJobsPage({
        limit: JOB_PAGE_SIZE,
        offset: loadedCountRef.current,
      })
      const merged = mergeJobPages(jobsRef.current, page.jobs)
      commitJobs(merged)
      setTotalJobs(page.total)
      setHasMoreJobs(page.hasMore)
      setLoadError(null)
      setExpanded((prev) => {
        const next = new Set(prev)
        for (const job of page.jobs) {
          next.add(job.id)
        }
        return next
      })
    } catch (err) {
      setLoadError(err instanceof Error ? localizeError(err) : tp("toast.loadMoreFailed"))
    } finally {
      loadingMoreRef.current = false
      setIsLoadingMore(false)
    }
  }, [commitJobs, hasMoreJobs, localizeError, tp])

  const updateJobs = useCallback(
    (updater: (current: JobSummary[]) => JobSummary[]) => {
      commitJobs(updater(jobsRef.current))
    },
    [commitJobs],
  )

  const loadJobs = useCallback(async () => {
    try {
      const limit = Math.max(loadedCountRef.current || JOB_PAGE_SIZE, JOB_PAGE_SIZE)
      const page = await listJobsPage({ limit, offset: 0 })
      const data = page.jobs
      const previousJobIds = prevJobIdsRef.current
      commitJobs(data)
      setTotalJobs(page.total)
      setHasMoreJobs(page.hasMore)
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
              !previousJobIds.has(j.id)
            ) {
              next.add(j.id)
            }
          }
          return next
        })
      }
    } catch (err) {
      setLoadError(err instanceof Error ? localizeError(err) : tp("toast.loadListFailed"))
    } finally {
      setIsLoading(false)
    }
  }, [commitJobs, localizeError, tp])

  // Initial load
  useEffect(() => {
    void loadJobs()
  }, [loadJobs])

  useEffect(() => {
    let cancelled = false
    getEntitlements()
      .then((entitlements) => {
        if (!cancelled) setIsAdminView(entitlements.role === "admin")
      })
      .catch(() => {
        if (!cancelled) setIsAdminView(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const needsProjectPoll = useMemo(
    () => jobs.some((j) => POLL_STATUSES.includes(j.status)),
    [jobs],
  )
  usePollingTask(loadJobs, {
    enabled: needsProjectPoll,
    immediate: false,
    intervalMs: POLL_INTERVAL,
  })

  useEffect(() => {
    const node = loadMoreSentinelRef.current
    if (!node || !hasMoreJobs) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          void loadMoreJobs()
        }
      },
      { rootMargin: "320px" },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [hasMoreJobs, loadMoreJobs])

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
      if (!window.confirm(tp("toast.deleteConfirm"))) return
      setDeletingId(job.id)
      try {
        await deleteJob(job.id)
        updateJobs((prev) => prev.filter((j) => j.id !== job.id))
        toast.success(tp("toast.deleteSuccess"))
      } catch {
        toast.error(tp("toast.deleteFailed"))
      } finally {
        setDeletingId(null)
      }
    },
    [updateJobs, tp],
  )

  const handleCancel = useCallback(
    async (job: JobSummary) => {
      setCancellingId(job.id)
      try {
        await cancelJob(job.id)
        toast.success(tp("toast.cancelSuccess"))
        void loadJobs()
      } catch {
        toast.error(tp("toast.cancelFailed"))
      } finally {
        setCancellingId(null)
      }
    },
    [loadJobs, tp],
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
        updateJobs((prev) => prev.map((j) => (j.id === updated.id ? updated : j)))
        toast.success(
          updated.title === newName.trim()
            ? tp("toast.renameSuccess")
            : tp("toast.renameSuccessCollision", { title: updated.title }), // collision suffix applied
        )
        setRenamingJob(null)
      } catch (err) {
        toast.error(err instanceof Error ? localizeError(err) : tp("toast.renameFailed"))
      } finally {
        setRenameSubmitting(false)
      }
    },
    [renamingJob, updateJobs, localizeError, tp],
  )

  // ---- Bulk pan-backup helpers (admin only) ----
  // Backend: POST /api/admin/pan/backups/batch (Phase 7a).
  // Eligibility: only status='succeeded' rows. Selection auto-prunes when
  // a job is deleted or its status moves out of 'succeeded' (polling).

  const eligibleBackupIds = useMemo(
    () => jobs.filter((j) => j.status === "succeeded").map((j) => j.id),
    [jobs],
  )

  useEffect(() => {
    setSelectedBackupIds((prev) => {
      if (prev.size === 0) return prev
      const allowed = new Set(eligibleBackupIds)
      const next = new Set<string>()
      for (const id of prev) if (allowed.has(id)) next.add(id)
      return next.size === prev.size ? prev : next
    })
  }, [eligibleBackupIds])

  const toggleBackupSelect = useCallback((jobId: string) => {
    setSelectedBackupIds((prev) => {
      const next = new Set(prev)
      if (next.has(jobId)) next.delete(jobId)
      else next.add(jobId)
      return next
    })
  }, [])

  const clearBackupSelection = useCallback(
    () => setSelectedBackupIds(new Set()),
    [],
  )

  const handleBulkBackup = useCallback(async () => {
    if (selectedBackupIds.size === 0) return
    const ids = Array.from(selectedBackupIds)
    if (
      !window.confirm(tp("toast.bulkBackupConfirm", { count: ids.length }))
    )
      return
    setBulkBackingUp(true)
    try {
      const r = await enqueueBackupBatch(ids)
      const okN = r.succeeded?.length ?? 0
      const failN = r.failed?.length ?? 0
      if (failN === 0) {
        toast.success(tp("toast.bulkBackupEnqueued", { count: okN }))
      } else if (okN === 0) {
        toast.error(
          tp("toast.bulkBackupAllFailed", {
            count: failN,
            reason: r.failed[0]?.reason ?? tp("toast.bulkBackupUnknownError"),
          }),
        )
      } else {
        toast.warning(
          tp("toast.bulkBackupPartial", {
            okCount: okN,
            failCount: failN,
            ids: r.failed
              .slice(0, 2)
              .map((f) => f.job_id.slice(0, 8))
              .join(", "),
            more: failN > 2 ? "…" : "",
          }),
        )
      }
      if (okN > 0) clearBackupSelection()
    } catch (e) {
      toast.error(e instanceof Error ? localizeError(e) : tp("toast.bulkBackupEnqueueFailed"))
    } finally {
      setBulkBackingUp(false)
    }
  }, [selectedBackupIds, clearBackupSelection, localizeError, tp])

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
            {tp("error.retry")}
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
            {tp("empty.title")}
          </h2>
          <p className="text-sm text-muted-foreground">
            {tp("empty.description")}
          </p>
          <Button onClick={() => setDialogOpen(true)} className="mt-2">
            <Plus className="h-4 w-4 mr-1" />
            {tp("empty.newTranslation")}
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
          {isAdminView
            ? tp.rich("retention.admin", { strong: (chunks) => <strong>{chunks}</strong> })
            : tp.rich("retention.user", { strong: (chunks) => <strong>{chunks}</strong> })}
        </p>
      </div>

      {/* Admin-only bulk pan backup toolbar — shows when ≥ 1 succeeded job
          is selected. Sticky so it stays visible while scrolling cards.
          Not rendered for regular users (isAdminView=false) because the
          backend rejects /api/admin/pan/* without role=admin. */}
      {isAdminView && selectedBackupIds.size > 0 && (
        <div className="sticky top-0 z-10 flex flex-wrap items-center gap-3 rounded-xl border border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/5 px-4 py-3 shadow-sm">
          <span className="text-sm text-foreground">
            {tp.rich("backup.selectedCount", {
              count: selectedBackupIds.size,
              strong: (chunks) => <strong>{chunks}</strong>,
            })}
          </span>
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              disabled={bulkBackingUp}
              onClick={handleBulkBackup}
              className="flex items-center gap-1.5 rounded-lg bg-[color:var(--cinnabar)] px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              {bulkBackingUp ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Cloud className="h-3.5 w-3.5" />
              )}
              {tp("backup.backupToPan")}
            </button>
            <button
              type="button"
              onClick={clearBackupSelection}
              disabled={bulkBackingUp}
              className="flex items-center gap-1 rounded-lg border border-border px-2 py-1.5 text-xs text-muted-foreground hover:bg-muted/40 disabled:opacity-50"
              title={tp("backup.clearSelection")}
            >
              <X className="h-3.5 w-3.5" />
              {tp("backup.clear")}
            </button>
          </div>
        </div>
      )}

      <div className="grid gap-3 grid-cols-1 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
        {sorted.map((job) => {
          // D19: 副本卡片标题下显示 "· 派生自 <源名>"。源 job 多半在同一
          // 用户的 list 里 — 直接从 `jobs` 里查一次即可；O(n) per card 对
          // <100 条任务没压力。源已被 7d TTL 清理时 sourceJob 为 undefined,
          // UI 会回退为 "· 副本"（仅标记派生，不挂悬空的源名）。
          const sourceJob = job.copyOfJobId
            ? jobs.find((j) => j.id === job.copyOfJobId)
            : null
          const sourceTitle = sourceJob ? getJobDisplayTitle(t, sourceJob) : null
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
              isAdminView={isAdminView}
              backupSelectable={isAdminView && job.status === "succeeded"}
              isBackupSelected={selectedBackupIds.has(job.id)}
              onToggleBackupSelect={toggleBackupSelect}
            />
          )
        })}
      </div>

      {loadError && jobs.length > 0 && (
        <p className="text-center text-sm text-red-500">{loadError}</p>
      )}

      <div ref={loadMoreSentinelRef} className="flex justify-center py-4">
        {isLoadingMore ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <RefreshCw className="h-4 w-4 animate-spin" />
            <span>{tp("loadMore.loading")}</span>
          </div>
        ) : hasMoreJobs ? (
          <Button variant="outline" size="sm" onClick={() => void loadMoreJobs()}>
            {tp("loadMore.button")}
          </Button>
        ) : totalJobs !== null ? (
          <p className="text-xs text-muted-foreground">
            {tp("loadMore.shown", { shown: jobs.length, total: totalJobs })}
          </p>
        ) : null}
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
  const tp = useTranslations("appProjects")
  const router = useRouter()
  const hasWaiting = activeTask?.status === "waiting_for_review"

  return (
    <div className="flex items-center justify-between gap-3">
      <h1 className="text-2xl font-bold font-heading text-foreground">
        {tp("header.title")}
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
            {tp("header.currentTask")}
            {hasWaiting && (
              <span className="absolute -top-1 -right-1 h-2.5 w-2.5 rounded-full bg-red-500" />
            )}
          </Button>
        )}
        <Button size="sm" onClick={onNewClick}>
          <Plus className="h-3.5 w-3.5 mr-1" />
          {tp("header.newTranslation")}
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
  isAdminView,
  backupSelectable,
  isBackupSelected,
  onToggleBackupSelect,
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
  isAdminView: boolean
  /** True when admin + job.status='succeeded'. False hides the checkbox entirely. */
  backupSelectable: boolean
  isBackupSelected: boolean
  onToggleBackupSelect: (jobId: string) => void
}) {
  const t = useTranslations("app")
  const tp = useTranslations("appProjects")
  const router = useRouter()
  const formatLocale = useIntlLocale()
  const expiry = computeExpiryInfo(job)
  const isNonExpiring = isAdminView || job.roleSnapshot === "admin"
  const expiryText = isNonExpiring ? tp("card.neverExpires") : expiryLabel(t, expiry)
  const showEditShortcut = isJobEditEligible(job)

  return (
    <Card
      size="sm"
      className={`overflow-visible ${
        isBackupSelected
          ? "ring-2 ring-[color:var(--cinnabar)]/40 bg-[color:var(--cinnabar)]/5"
          : ""
      }`}
    >
      {/* Card header — clickable to expand/collapse */}
      <div className="flex items-center gap-3 px-4 py-3">
        {backupSelectable && (
          <label
            className="flex shrink-0 cursor-pointer items-center"
            onClick={(e) => e.stopPropagation()}
            title={tp("card.backupCheckboxTitle")}
          >
            <input
              type="checkbox"
              aria-label={tp("card.backupCheckboxAria", { title: getJobDisplayTitle(t, job) })}
              checked={isBackupSelected}
              onChange={() => onToggleBackupSelect(job.id)}
              className="h-4 w-4 cursor-pointer accent-[color:var(--cinnabar)]"
            />
          </label>
        )}
        <button
          type="button"
          onClick={onToggle}
          className="flex flex-1 items-center gap-3 text-left transition hover:bg-muted/30 -mx-2 px-2 py-1 rounded"
        >
          {isExpanded ? (
            <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
          )}

        <div className="min-w-0 flex-1 space-y-0.5">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-foreground truncate">
              {getJobDisplayTitle(t, job)}
            </span>
            <StatusBadge status={job.status} editGeneration={job.editGeneration ?? 0} />
          </div>
          {/* D19: 副本派生关系小字标识。不可点（plan 明确：仅标记，不做 tree
           *   视图）；源已被 7d TTL 清走时 fallback 成通用 "· 副本"。 */}
          {job.copyOfJobId && (
            <div className="text-[11px] text-muted-foreground truncate">
              {tp("card.derivedFrom", { source: sourceTitle ?? tp("card.derivedSourcePurged") })}
            </div>
          )}
          <div className="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
            <span>{timeLabel(cardTimestamp(job), formatLocale)}</span>
            {expiryText ? (
              <span className={isNonExpiring ? "text-[color:var(--bamboo)]" : expiryColorClass(expiry.tier)}>
                {expiryText}
              </span>
            ) : null}
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
      </div>

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
  const t = useTranslations("app")
  const tp = useTranslations("appProjects")
  const editShortcutHref = isJobEditEligible(job)
    ? `/workspace/${job.id}/edit`
    : undefined

  switch (job.status) {
    case "succeeded":
      // 智能版预览：stream-only teaser + 转完整 CTA，取代普通 ResultMediaCard
      // （预览任务后端 403 全部下载 / 素材 / 剪映 / 修改 → 普通卡会渲染坏播放器
      // 与无效下载按钮）。不传 editHref（预览不可进编辑）。
      if (job.smartPreviewMode) {
        return <SmartPreviewResultCard job={job} />
      }
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
              <span className="text-muted-foreground">{tp("expanded.stageLabel")}</span>
              <span className="font-medium text-foreground">
                {getStageLabel(t, job.currentStage)}
              </span>
            </div>
            {job.progressMessage && (
              <p className="text-xs text-muted-foreground">
                {getUserFacingProgressMessage(t, job.progressMessage) ??
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
              {tp("expanded.enterWorkspace")}
            </a>
            <Button
              variant="ghost"
              size="sm"
              onClick={onCancel}
              disabled={isCancelling}
            >
              <XCircle className="h-3.5 w-3.5 mr-1" />
              {isCancelling ? tp("expanded.cancelling") : tp("expanded.cancel")}
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
            {tp("expanded.needsReview")}
          </div>
          <Link href={`/workspace/${job.id}`}>
            <Button size="sm">
              <ExternalLink className="h-3.5 w-3.5 mr-1" />
              {tp("expanded.enterWorkspace")}
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
            {tp("expanded.editingInProgress")}
            {(job.editGeneration ?? 0) > 0 && (
              <span className="text-xs opacity-75">
                {tp("expanded.editsCompleted", { count: job.editGeneration ?? 0 })}
              </span>
            )}
          </div>
          <Link href={`/workspace/${job.id}/edit`}>
            <Button size="sm">
              <ExternalLink className="h-3.5 w-3.5 mr-1" />
              {tp("expanded.continueEditing")}
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
            {getUserFacingProgressMessage(t, job.progressMessage) ?? tp("expanded.processingFailed")}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={onReCreate}>
              <RefreshCw className="h-3.5 w-3.5 mr-1" />
              {tp("expanded.recreate")}
            </Button>
          </div>
        </div>
      )

    case "queued":
      return (
        <div className="space-y-3">
          <div className="flex items-center gap-2 rounded-lg bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
            <Clock className="h-4 w-4 shrink-0" />
            {tp("expanded.queued")}
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={onCancel}
            disabled={isCancelling}
          >
            <XCircle className="h-3.5 w-3.5 mr-1" />
            {isCancelling ? tp("expanded.cancelling") : tp("expanded.cancel")}
          </Button>
        </div>
      )

    case "cancelled":
      return (
        <div className="flex items-center gap-2 rounded-lg bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
          <XCircle className="h-4 w-4 shrink-0" />
          {tp("expanded.cancelled")}
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
  const tp = useTranslations("appProjects")
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
        title={tp("card.rename")}
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
              title={tp("card.delete")}
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
  const t = useTranslations("app")
  const tp = useTranslations("appProjects")
  const [value, setValue] = useState(() => getJobDisplayTitle(t, job))

  const trimmed = value.trim()
  const tooLong = trimmed.length > 60
  const hasBadChar = FORBIDDEN_RENAME_CHARS.test(trimmed)
  const canSubmit = !submitting && !!trimmed && !tooLong && !hasBadChar

  return (
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{tp("rename.title")}</DialogTitle>
          <DialogDescription>
            {tp("rename.description")}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Label htmlFor="rename-input">{tp("rename.label")}</Label>
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
              {tp("rename.badChar", { lt: "<", gt: ">" })}
            </p>
          )}
          {tooLong && (
            <p className="text-xs text-red-500">{tp("rename.tooLong")}</p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={submitting}>
            {tp("rename.cancel")}
          </Button>
          <Button
            onClick={() => canSubmit && onConfirm(trimmed)}
            disabled={!canSubmit}
          >
            {submitting ? tp("rename.saving") : tp("rename.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
  )
}
