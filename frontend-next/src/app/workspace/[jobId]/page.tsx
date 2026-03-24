"use client"

import { useState } from 'react'
import Link from 'next/link'
import { useParams, useRouter } from 'next/navigation'

import { EmptyState } from '@/components/empty-state'
import { StatusBadge } from '@/components/status-badge'
import { StageProgress } from '@/components/stage-progress'
import { LogViewer } from '@/components/log-viewer'
import { ResultDownloadList } from '@/components/result-download-list'
import {
  SpeakerReviewPanel,
  TranslationReviewPanel,
  TranslationConfigPanel,
  VoiceReviewPanel,
} from '@/components/workspace'
import {
  getErrorCategory,
  getErrorSummaryMessage,
  getJobDisplayTitle,
  getJobSecondaryLabel,
  getStageLabel,
  getUserFacingProgressMessage,
} from '@/features/jobs/presentation'
import { buildStageProgress } from '@/features/jobs/stageMetadata'
import { ApiError } from '@/lib/api/client'
import {
  getJob,
  getJobLogs,
  getProjectArtifacts,
  getProjectResultSummary,
} from '@/lib/api/jobs'
import { cancelCurrentJob } from '@/lib/api/reviews'
import { usePollingTask } from '@/lib/react/usePollingTask'
import {
  type JobLogEntry,
  type JobSummary,
  type ResultDownloadItem,
} from '@/types/jobs'

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
      setLogs(nextLogs)
      setDownloads(nextDownloads)
      setPageError(null)
    } catch (error) {
      setPageError(getErrorMessage(error))
    } finally {
      setIsLoading(false)
    }
  }

  usePollingTask(() => loadJob(!isLoading), { intervalMs: 4000 })

  // Called by review panels after approval to refresh job state
  const handleAdvanced = () => {
    void loadJob(true)
  }

  const handleCancel = async () => {
    if (!window.confirm('确定要取消当前任务吗？取消后可以创建新的翻译任务。')) return
    setIsCancelling(true)
    try {
      await cancelCurrentJob()
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
    return <EmptyState description="正在加载工作区..." title="加载中" />
  }
  if (pageError && !job) {
    return <EmptyState actionLabel="返回当前任务" actionTo="/tasks/current" description={pageError} title="无法加载工作区" />
  }
  if (!job) {
    return <EmptyState actionLabel="新建翻译" actionTo="/translations/new" description="找不到该任务。" title="任务不存在" />
  }

  const stageItems = buildStageProgress(job.status, job.currentStage)
  const isWaitingForReview = job.status === 'waiting_for_review'
  const isProcessing = job.status === 'running' || job.status === 'queued'
  const isSucceeded = job.status === 'succeeded'
  const isFailed = job.status === 'failed'
  const displayTitle = getJobDisplayTitle(job)
  const secondaryLabel = getJobSecondaryLabel(job)
  const availableDownloadCount = downloads.filter((i) => i.available).length

  return (
    <div className="space-y-6">
      {/* ===== Fixed Header: Job Info ===== */}
      <section className="surface-card p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-2 min-w-0">
            <p className="eyebrow">工作区</p>
            <h1 className="text-2xl font-bold text-ink-950 dark:text-white/95 truncate">{displayTitle}</h1>
            <p className="text-sm text-muted-foreground">{secondaryLabel}</p>
          </div>
          <div className="flex items-center gap-3 shrink-0">
            <StatusBadge status={job.status} />
            <Link className="rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-xs text-white/60 transition hover:bg-white/10" href={`/projects/${jobId}`}>项目详情</Link>
            {(isWaitingForReview || isProcessing) ? (
              <button
                className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2 text-xs font-medium text-red-400 transition hover:bg-red-500/20 hover:border-red-500/50 disabled:opacity-50"
                disabled={isCancelling}
                onClick={() => { void handleCancel() }}
                type="button"
              >
                {isCancelling ? '取消中...' : '取消任务'}
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
              当前需要处理：{getStageLabel(job.currentStage)}
            </span>
          ) : isProcessing ? (
            <span>{getUserFacingProgressMessage(job.progressMessage) ?? '任务正在处理中...'}</span>
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
          <h3 className="text-lg font-semibold text-ink-950 dark:text-white/90">
            正在处理 · {getStageLabel(job.currentStage)}
          </h3>
          <p className="mt-2 text-sm text-muted-foreground">
            {getUserFacingProgressMessage(job.progressMessage) ?? '任务正在后台处理，页面会自动刷新...'}
          </p>
        </section>
      ) : null}

      {/* Review panels */}
      {isWaitingForReview && job.currentStage === 'speaker_review' ? (
        <SpeakerReviewPanel jobId={jobId} onAdvanced={handleAdvanced} />
      ) : null}

      {isWaitingForReview && job.currentStage === 'voice_review' ? (
        <VoiceReviewPanel jobId={jobId} onAdvanced={handleAdvanced} />
      ) : null}

      {isWaitingForReview && job.currentStage === 'translation_config_review' ? (
        <TranslationConfigPanel jobId={jobId} onAdvanced={handleAdvanced} />
      ) : null}

      {isWaitingForReview && job.currentStage === 'translation_review' ? (
        <TranslationReviewPanel jobId={jobId} onAdvanced={handleAdvanced} />
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
            <p className="text-sm font-medium text-ink-950 dark:text-white/80">建议</p>
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

function getErrorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '请求失败，请稍后重试。'
}
