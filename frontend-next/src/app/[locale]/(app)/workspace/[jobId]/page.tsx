"use client"

import { useState, useRef, useEffect } from 'react'
import { useTranslations } from 'next-intl'
import { Link, useRouter } from "@/i18n/navigation"
import { useParams } from "next/navigation"
import { toast } from 'sonner'
import { RefreshCw } from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { useConfirmDialog } from '@/components/ui/confirm-dialog'
import { StatusBadge } from '@/components/status-badge'
import { StageProgress } from '@/components/stage-progress'
import { LogViewer } from '@/components/log-viewer'
import { ResultDownloadList } from '@/components/result-download-list'
import { ResultMediaCard } from '@/components/workspace/ResultMediaCard'
import { SmartPreviewResultCard } from '@/components/workspace/SmartPreviewResultCard'
import { TranslationReviewPanel, VoiceReviewPanel, VoiceSelectionPanel, SmartAutoDecisionPanel } from '@/components/workspace'
import {
  getErrorCategory,
  getErrorSummaryMessage,
  getJobDisplayTitle,
  getJobSecondaryLabel,
  getStageLabel,
  getUserFacingProgressMessage,
} from '@/features/jobs/presentation'
import { buildStageProgress } from '@/features/jobs/stageMetadata'
import { getErrorMessage } from '@/lib/api/errors'
import {
  getJob,
  getJobLogs,
  getProjectArtifacts,
  getProjectResultSummary,
} from '@/lib/api/jobs'
import { approveTranslationConfigReview, cancelJob } from '@/lib/api/reviews'
import { getEntitlements } from '@/lib/api/entitlements'
import { usePollingTask } from '@/lib/react/usePollingTask'
import {
  type JobLogEntry,
  type JobSummary,
  type PublicStage,
  type ResultDownloadItem,
} from '@/types/jobs'

function sendBrowserNotification(status: string, title: string) {
  if (typeof window === 'undefined' || !('Notification' in window)) return
  if (Notification.permission !== 'granted') return

  const messages: Record<string, { title: string; body: string }> = {
    succeeded: { title: '任务完成', body: `${title} 已完成，点击查看结果` },
    failed: { title: '任务失败', body: `${title} 处理失败，点击查看详情` },
    cancelled: { title: '任务已取消', body: `${title} 已被取消` },
  }
  const msg = messages[status]
  if (msg) {
    try { new Notification(msg.title, { body: msg.body }) } catch { /* ignore */ }
  }
}

export default function WorkspacePage() {
  const t = useTranslations('app')
  const params = useParams()
  const router = useRouter()
  const jobId = ((params.jobId as string) ?? '').trim()

  const [job, setJob] = useState<JobSummary | null>(null)
  const [logs, setLogs] = useState<JobLogEntry[]>([])
  const [downloads, setDownloads] = useState<ResultDownloadItem[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)
  const [isCancelling, setIsCancelling] = useState(false)
  const { confirm, confirmDialog } = useConfirmDialog()
  const [webUiStage, setWebUiStage] = useState<string | null>(null)
  // Plan §10.3: "关键进展" log panel + raw progressMessage text are admin-only.
  // Non-admin users see only the stage label + a generic "处理中" fallback so
  // provider names / UUIDs / raw error codes never leak into the UI. This is
  // belt-and-braces with the server-side D25 redactor on /logs.
  const [isAdmin, setIsAdmin] = useState(false)
  useEffect(() => {
    let cancelled = false
    getEntitlements()
      .then((ent) => { if (!cancelled) setIsAdmin(ent.ui?.show_admin_badge === true) })
      .catch(() => { /* keep default: non-admin */ })
    return () => { cancelled = true }
  }, [])
  const prevStatusRef = useRef<string | null>(null)

  const loadJob = async (silent = false) => {
    if (!jobId) return
    if (!silent) setIsLoading(true)
    try {
      const [nextJob, nextLogs, nextDownloads] = await Promise.all([
        getJob(jobId),
        getJobLogs(jobId),
        getProjectArtifacts(jobId).catch(() => []),
      ])
      setJob(nextJob)
      // 检测状态变化，发送浏览器通知
      if (prevStatusRef.current &&
          prevStatusRef.current !== nextJob.status &&
          (nextJob.status === 'succeeded' || nextJob.status === 'failed' || nextJob.status === 'cancelled')) {
        sendBrowserNotification(nextJob.status, getJobDisplayTitle(t, nextJob))
        // Auto-redirect to projects page after successful completion.
        // 智能版预览任务例外：teaser + 转完整 CTA 就在本工作区页，跳转到 /projects
        // 会丢失试看上下文 → 留在原页。
        if (nextJob.status === 'succeeded' && !nextJob.smartPreviewMode) {
          toast.success('任务已完成，即将跳转到视频翻译主页...')
          setTimeout(() => {
            router.push('/projects')
          }, 2000)
        }
      }
      prevStatusRef.current = nextJob.status
      setLogs(nextLogs)
      setDownloads(nextDownloads)
      // Derive review stage from the job's own review_gate (job-scoped, no global /api/state)
      const gateStage = typeof nextJob.reviewGate?.stage === 'string' ? nextJob.reviewGate.stage : null
      const derivedStage = gateStage ?? nextJob.currentStage ?? null
      if (derivedStage) setWebUiStage(derivedStage)
      setPageError(null)
    } catch (error) {
      setPageError(getErrorMessage(error))
    } finally {
      setIsLoading(false)
    }
  }

  usePollingTask(() => loadJob(!isLoading), { intervalMs: 4000 })

  useEffect(() => {
    if (typeof window !== 'undefined' && 'Notification' in window && Notification.permission === 'default') {
      // 延迟 5 秒请求，避免页面加载时弹出
      const timer = setTimeout(() => { void Notification.requestPermission() }, 5000)
      return () => clearTimeout(timer)
    }
  }, [])

  // Auto-approve translation_config_review with default config (no dedicated UI yet).
  // Guarded by a ref so we only fire once per job, and only when the job is actually
  // waiting at this specific stage.
  const autoApprovedRef = useRef<Record<string, boolean>>({})
  useEffect(() => {
    if (!job) return
    if (job.status !== 'waiting_for_review') return
    const stage = job.reviewGate?.stage ?? job.currentStage
    if (stage !== 'translation_config_review') return
    if (autoApprovedRef.current[job.id]) return
    autoApprovedRef.current[job.id] = true
    ;(async () => {
      try {
        await approveTranslationConfigReview(job.id)
        void loadJob(true)
      } catch (error) {
        setPageError(getErrorMessage(error))
      }
    })()
  }, [job])

  // Called by review panels after approval to refresh job state
  const handleAdvanced = () => {
    void loadJob(true)
  }

  const handleCancel = async () => {
    const confirmed = await confirm({
      title: '取消任务',
      description: '确定要取消当前任务吗？取消后可以创建新的翻译任务。',
      destructive: true,
    })
    if (!confirmed) return
    setIsCancelling(true)
    try {
      await cancelJob(jobId)
      router.push('/projects?new=1')
    } catch (error) {
      setPageError(getErrorMessage(error))
      setIsCancelling(false)
    }
  }

  if (!jobId) {
    return <EmptyState actionLabel="返回当前任务" actionTo="/tasks/current" description="缺少任务标识。" title="无法打开工作区" />
  }
  if (isLoading && !job && !pageError) {
    return <EmptyState description="正在加载工作区…" title="加载中" />
  }
  if (pageError && !job) {
    return <EmptyState actionLabel="返回当前任务" actionTo="/tasks/current" description={pageError} title="无法加载工作区" />
  }
  if (!job) {
    return <EmptyState actionLabel="新建翻译" actionTo="/projects?new=1" description="找不到该任务。" title="任务不存在" />
  }

  const isWaitingForReview = job.status === 'waiting_for_review'
  const isProcessing = job.status === 'running' || job.status === 'queued'
  // Intentionally distinct from isProcessing: editing is a user-held session,
  // not a pipeline state. See docs/internal/status-touchpoints-2026-04-18.md §0.
  const isEditing = job.status === 'editing'
  const isSucceeded = job.status === 'succeeded'
  const isFailed = job.status === 'failed'
  const editGeneration = job.editGeneration ?? 0
  const displayTitle = getJobDisplayTitle(t, job)
  const secondaryLabel = getJobSecondaryLabel(t, job)
  const availableDownloadCount = downloads.filter((i) => i.available).length

  // Use Web UI's active stage when available (more accurate than Job API's currentStage)
  const effectiveReviewStage = webUiStage ?? job.currentStage
  // Use effective stage for progress bar and labels
  const effectiveStage = (isWaitingForReview && effectiveReviewStage) ? effectiveReviewStage as PublicStage : job.currentStage
  const stageItems = buildStageProgress(t, job.status, effectiveStage)

  return (
    <div className="space-y-6">
      {/* ===== Fixed Header: Job Info ===== */}
      <section className="surface-card p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-2 min-w-0">
            <p className="eyebrow">工作区</p>
            <h1 className="text-2xl font-bold text-foreground truncate">{displayTitle}</h1>
            <p className="text-sm text-muted-foreground">{secondaryLabel}</p>
          </div>
          <div className="flex items-center gap-3 shrink-0">
            <StatusBadge status={job.status} editGeneration={editGeneration} />
            <Link className="rounded-lg border border-border bg-muted/30 px-4 py-2 text-xs text-muted-foreground transition hover:bg-muted/50" href={`/projects/${jobId}`}>项目详情</Link>
            {(isWaitingForReview || isProcessing) ? (
              <button
                className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2 text-xs font-medium text-red-400 transition hover:bg-red-500/20 hover:border-red-500/50 disabled:opacity-50"
                disabled={isCancelling}
                onClick={() => { void handleCancel() }}
                type="button"
              >
                {isCancelling ? '取消中…' : '取消任务'}
              </button>
            ) : null}
            {/*
              editing state has its own "放弃修改" / "确认修改" buttons on the
              /workspace/{id}/edit page (plan §7.6). We do NOT surface a
              cancel-task button here — cancelling an editing session is
              destructive (drops the user's draft) and the confirmation flow
              must happen on the edit page itself.
            */}
          </div>
        </div>

        {/* Stage progress bar */}
        <div className="mt-4 -mx-1">
          <StageProgress items={stageItems} />
        </div>

        {/* Progress message — plan §10.1: the "正在处理 · XXX" heading
         *  already surfaces in the big processing card below, so the
         *  processing branch here is intentionally silent to avoid
         *  displaying the same progressMessage twice. Other statuses
         *  (review / editing / succeeded / failed) keep their concise
         *  one-liner here because they don't render a dedicated card. */}
        <div className="mt-3 text-sm text-muted-foreground">
          {isWaitingForReview ? (
            <span className="font-medium" style={{ color: "var(--ochre)" }}>
              当前需要处理：{getStageLabel(t, effectiveStage)}
            </span>
          ) : isEditing ? (
            <span className="font-medium" style={{ color: "var(--ochre)" }}>
              此任务处于修改中
              {editGeneration > 0 ? `（已完成 ${editGeneration} 次修改）` : ''}
            </span>
          ) : isSucceeded ? (
            <span className="font-medium" style={{ color: "var(--bamboo)" }}>任务已完成</span>
          ) : isFailed ? (
            <span className="font-medium" style={{ color: "var(--cinnabar)" }}>任务处理失败</span>
          ) : null}
        </div>
      </section>

      {/* ===== Dynamic Content Area ===== */}

      {/* Processing state — plan §10.4: non-admin users see a clean
       *  "stage + generic reassurance" card. The raw progressMessage
       *  (may contain provider names / retry-with-504 / UUIDs) is
       *  admin-only. Admins still get the live message for debugging;
       *  a more comprehensive D25 server-side redactor would let us
       *  show something tighter to everyone, but until then the safe
       *  default is a fixed fallback line. */}
      {isProcessing ? (
        <section className="surface-card p-8 text-center">
          <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-3 border-primary border-t-transparent" />
          <h3 className="text-lg font-semibold text-foreground">
            {editGeneration > 0
              ? `正在重合成 · 第 ${editGeneration} 次修改`
              : `正在处理 · ${getStageLabel(t, effectiveStage)}`}
          </h3>
          <p className="mt-2 text-sm text-muted-foreground">
            {isAdmin
              ? (getUserFacingProgressMessage(t, job.progressMessage) ?? '任务正在后台处理，页面会自动刷新…')
              : '任务正在后台处理，页面会自动刷新…'}
          </p>
        </section>
      ) : null}

      {/* Editing state (plan §10.4 / D18). Distinct from "正在处理" — editing
          is a user-held session: the pipeline is idle waiting for the user to
          return to the edit page and commit or abandon the draft. The real
          edit surface lives at /workspace/{id}/edit (Phase 1 T1-3); this
          card only offers the resume CTA. */}
      {isEditing ? (
        <section
          className="surface-card p-8 text-center"
          style={{
            border: "1px solid color-mix(in oklab, var(--ochre) 28%, transparent)",
            backgroundColor: "color-mix(in oklab, var(--ochre) 8%, transparent)",
          }}
        >
          <RefreshCw className="mx-auto mb-4 h-10 w-10" style={{ color: "var(--ochre)" }} />
          <h3 className="text-lg font-semibold text-foreground">
            此任务正在修改中
          </h3>
          <p className="mt-2 text-sm text-muted-foreground">
            {editGeneration > 0
              ? `已完成 ${editGeneration} 次修改。点击下方按钮继续本轮编辑，或在修改页放弃草稿。`
              : '点击下方按钮继续本轮编辑，或在修改页放弃草稿。'}
          </p>
          <div className="mt-4 flex justify-center">
            <Link
              href={`/workspace/${jobId}/edit`}
              className="inline-flex items-center gap-1 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:bg-primary/90"
            >
              <RefreshCw className="h-4 w-4" />
              继续修改
            </Link>
          </div>
        </section>
      ) : null}

      {/* Unified review panel — combines speaker, voice, and translation review */}
      {isWaitingForReview && effectiveReviewStage === 'translation_review' ? (
        <TranslationReviewPanel jobId={jobId} onAdvanced={handleAdvanced} />
      ) : null}

      {/* Voice review panel — only for volcengine + studio (panel detects via snapshot) */}
      {isWaitingForReview && effectiveReviewStage === 'voice_review' ? (
        <VoiceReviewPanel jobId={jobId} onAdvanced={handleAdvanced} />
      ) : null}

      {/* Voice selection review — Studio mode: per-speaker voice selection + clone */}
      {isWaitingForReview && effectiveReviewStage === 'voice_selection_review' ? (
        <VoiceSelectionPanel jobId={jobId} onAdvanced={handleAdvanced} />
      ) : null}

      {/* Auto-processing stages (speaker/translation_config are now automatic) */}
      {isWaitingForReview && (effectiveReviewStage === 'speaker_review' || effectiveReviewStage === 'translation_config_review') ? (
        <section className="surface-card p-8 text-center">
          <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-3 border-primary border-t-transparent" />
          <h3 className="text-lg font-semibold text-foreground">正在自动处理</h3>
          <p className="mt-2 text-sm text-muted-foreground">
            {effectiveReviewStage === 'speaker_review' ? '说话人识别中，即将自动进入下一步…' :
             '翻译配置已自动确认，正在继续处理…'}
          </p>
        </section>
      ) : null}

      {/* Error state */}
      {isFailed ? (
        <section className="surface-card p-6 border border-red-200 dark:border-red-500/20">
          <h3 className="text-lg font-semibold text-red-700 dark:text-red-400">
            {getErrorCategory(t, job.errorSummary).label}
          </h3>
          <p className="mt-2 text-sm text-red-600 dark:text-red-400/80">
            {getErrorSummaryMessage(t, job.errorSummary)}
          </p>
          <div className="mt-4 rounded-2xl border border-amber-200 bg-amber-50 p-3 dark:border-amber-500/20 dark:bg-amber-500/5">
            <p className="text-sm font-medium text-foreground/80">建议</p>
            <p className="text-sm text-muted-foreground">{getErrorCategory(t, job.errorSummary).suggestion}</p>
          </div>
          <div className="mt-4 flex gap-2">
            <Link className="secondary-button" href="/projects?new=1">重新创建任务</Link>
          </div>
        </section>
      ) : null}

      {/* 智能版预览：succeeded 时直接在工作区页放 stream-only teaser + 转完整 CTA。
        * 取代下方 SmartAutoDecisionPanel / 下载区 / "前往主页" 横幅（预览任务无下载，
        * 决策摘要对 teaser 也不适用）。后端对预览任务 403 全部下载 / 导出。 */}
      {isSucceeded && job.smartPreviewMode ? (
        <SmartPreviewResultCard job={job} />
      ) : null}

      {/* PR#3C-P3-c: Smart auto-decision summary panel for smart jobs.
        * Conditional on job.serviceMode === "smart" — the panel does
        * its own /smart-quality-report fetch and self-hides on
        * service_mode_not_smart (defense-in-depth in case the prop
        * lags), so non-smart jobs never see it. Positioned BEFORE
        * the download list per decision log §3 (between
        * media/completion banner and downloads).
        * 预览任务排除（smartPreviewMode）——teaser 不需要决策摘要。 */}
      {job.serviceMode === 'smart' && !job.smartPreviewMode ? (
        <SmartAutoDecisionPanel jobId={jobId} />
      ) : null}

      {/* Completed: Downloads only (playback + preview is on /projects main page).
        * 预览任务 stream-only，无可下载条目，整块跳过。 */}
      {!job.smartPreviewMode && availableDownloadCount > 0 ? (
        <>
          {isSucceeded && (
            <div className="rounded-lg border border-border bg-muted/30 p-4 text-sm text-muted-foreground">
              任务已完成，请前往<Link href="/projects" className="text-primary underline ml-1">视频翻译主页</Link>查看和播放结果。
            </div>
          )}
          <ResultDownloadList items={downloads} serviceMode={job.serviceMode} />
        </>
      ) : null}

      {/* Logs — admin-only. Plan §10.3: non-admin users don't need to see
       *  provider names / retry-with-504 / raw stack traces; those are
       *  debugging aids for the ops role. Hiding the whole panel keeps
       *  the workspace view concise (the processing card + stage dots
       *  are the primary signal). */}
      {isAdmin ? (
        <LogViewer
          description="最近关键进展。"
          emptyMessage="当前还没有关键进展。"
          entries={logs}
          initialVisibleCount={5}
          title="关键进展"
        />
      ) : null}

      {confirmDialog}
    </div>
  )
}
