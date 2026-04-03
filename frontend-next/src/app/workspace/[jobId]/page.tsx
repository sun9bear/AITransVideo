"use client"

import { useState, useRef, useEffect } from 'react'
import Link from 'next/link'
import { useParams, useRouter } from 'next/navigation'

import { EmptyState } from '@/components/empty-state'
import { StatusBadge } from '@/components/status-badge'
import { StageProgress } from '@/components/stage-progress'
import { LogViewer } from '@/components/log-viewer'
import { ResultDownloadList } from '@/components/result-download-list'
import { TranslationReviewPanel, VoiceReviewPanel } from '@/components/workspace'
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
import { cancelJob } from '@/lib/api/reviews'
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
  const params = useParams()
  const router = useRouter()
  const jobId = ((params.jobId as string) ?? '').trim()

  const [job, setJob] = useState<JobSummary | null>(null)
  const [logs, setLogs] = useState<JobLogEntry[]>([])
  const [downloads, setDownloads] = useState<ResultDownloadItem[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)
  const [isCancelling, setIsCancelling] = useState(false)
  const [webUiStage, setWebUiStage] = useState<string | null>(null)
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
        sendBrowserNotification(nextJob.status, getJobDisplayTitle(nextJob))
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

  // Called by review panels after approval to refresh job state
  const handleAdvanced = () => {
    void loadJob(true)
  }

  const handleCancel = async () => {
    if (!window.confirm('确定要取消当前任务吗？取消后可以创建新的翻译任务。')) return
    setIsCancelling(true)
    try {
      await cancelJob(jobId)
      router.push('/translations/new')
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
    return <EmptyState actionLabel="新建翻译" actionTo="/translations/new" description="找不到该任务。" title="任务不存在" />
  }

  const isWaitingForReview = job.status === 'waiting_for_review'
  const isProcessing = job.status === 'running' || job.status === 'queued'
  const isSucceeded = job.status === 'succeeded'
  const isFailed = job.status === 'failed'
  const displayTitle = getJobDisplayTitle(job)
  const secondaryLabel = getJobSecondaryLabel(job)
  const availableDownloadCount = downloads.filter((i) => i.available).length

  // Use Web UI's active stage when available (more accurate than Job API's currentStage)
  const effectiveReviewStage = webUiStage ?? job.currentStage
  // Use effective stage for progress bar and labels
  const effectiveStage = (isWaitingForReview && effectiveReviewStage) ? effectiveReviewStage as PublicStage : job.currentStage
  const stageItems = buildStageProgress(job.status, effectiveStage)

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
            <StatusBadge status={job.status} />
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
          </div>
        </div>

        {/* Stage progress bar */}
        <div className="mt-4 -mx-1">
          <StageProgress items={stageItems} />
        </div>

        {/* Progress message */}
        <div className="mt-3 text-sm text-muted-foreground">
          {isWaitingForReview ? (
            <span className="text-amber-600 dark:text-amber-400 font-medium">
              当前需要处理：{getStageLabel(effectiveStage)}
            </span>
          ) : isProcessing ? (
            <span>{getUserFacingProgressMessage(job.progressMessage) ?? '任务正在处理中…'}</span>
          ) : isSucceeded ? (
            <span className="text-emerald-600 dark:text-emerald-400 font-medium">任务已完成</span>
          ) : isFailed ? (
            <span className="text-red-600 dark:text-red-400 font-medium">任务处理失败</span>
          ) : null}
        </div>
      </section>

      {/* ===== Dynamic Content Area ===== */}

      {/* Processing state */}
      {isProcessing ? (
        <section className="surface-card p-8 text-center">
          <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-3 border-primary border-t-transparent" />
          <h3 className="text-lg font-semibold text-foreground">
            正在处理 · {getStageLabel(effectiveStage)}
          </h3>
          <p className="mt-2 text-sm text-muted-foreground">
            {getUserFacingProgressMessage(job.progressMessage) ?? '任务正在后台处理，页面会自动刷新…'}
          </p>
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
            {getErrorCategory(job.errorSummary).label}
          </h3>
          <p className="mt-2 text-sm text-red-600 dark:text-red-400/80">
            {getErrorSummaryMessage(job.errorSummary)}
          </p>
          <div className="mt-4 rounded-2xl border border-amber-200 bg-amber-50 p-3 dark:border-amber-500/20 dark:bg-amber-500/5">
            <p className="text-sm font-medium text-foreground/80">建议</p>
            <p className="text-sm text-muted-foreground">{getErrorCategory(job.errorSummary).suggestion}</p>
          </div>
          <div className="mt-4 flex gap-2">
            <Link className="secondary-button" href="/translations/new">重新创建任务</Link>
          </div>
        </section>
      ) : null}

      {/* Completed: Downloads */}
      {availableDownloadCount > 0 ? <ResultDownloadList items={downloads} /> : null}

      {/* Logs */}
      <LogViewer
        description="最近关键进展。"
        emptyMessage="当前还没有关键进展。"
        entries={logs}
        initialVisibleCount={5}
        title="关键进展"
      />
    </div>
  )
}
