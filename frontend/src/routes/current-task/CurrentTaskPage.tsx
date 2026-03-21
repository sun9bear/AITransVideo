import { useState } from 'react'
import { Link } from 'react-router-dom'

import { ConfigSummaryCard } from '@/components/ConfigSummaryCard'
import { EmptyState } from '@/components/EmptyState'
import { LogViewer } from '@/components/LogViewer'
import { ResultDownloadList } from '@/components/ResultDownloadList'
import { StageProgress } from '@/components/StageProgress'
import { StatusBadge } from '@/components/StatusBadge'
import {
  getErrorCategory,
  getErrorSummaryMessage,
  getJobDisplayTitle,
  getJobSecondaryLabel,
  getReviewActionForJob,
  getReviewPrompt,
  getStageLabel,
  getUserFacingProgressMessage,
} from '@/features/jobs/presentation'
import { selectCurrentTaskJob } from '@/features/jobs/selectors'
import { buildStageProgress } from '@/features/jobs/stageMetadata'
import { ApiError } from '@/lib/api/client'
import {
  getJobLogs,
  getProjectArtifacts,
  getProjectResultSummary,
  listJobs,
} from '@/lib/api/jobs'
import { usePollingTask } from '@/lib/react/usePollingTask'
import {
  JOB_STATUS_LABELS,
  type JobLogEntry,
  type JobSummary,
  type ProjectResultSummary,
  type ResultDownloadItem,
} from '@/types/jobs'

const primaryResultKeys = new Set([
  'publish.dubbed_video',
  'editor.dubbed_audio_complete',
  'editor.subtitles',
])

export function CurrentTaskPage() {
  const [job, setJob] = useState<JobSummary | null>(null)
  const [logs, setLogs] = useState<JobLogEntry[]>([])
  const [resultSummary, setResultSummary] = useState<ProjectResultSummary | null>(null)
  const [downloads, setDownloads] = useState<ResultDownloadItem[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)

  const loadCurrentTask = async (silent = false) => {
    if (!silent) {
      setIsLoading(true)
    }

    try {
      const jobs = await listJobs()
      const selectedJob = selectCurrentTaskJob(jobs)

      if (!selectedJob) {
        setJob(null)
        setLogs([])
        setResultSummary(null)
        setDownloads([])
        setPageError(null)
        return
      }

      const [nextLogs, nextResultSummary, nextDownloads] = await Promise.all([
        getJobLogs(selectedJob.id),
        getProjectResultSummary(selectedJob.id),
        getProjectArtifacts(selectedJob.id).catch(() => []),
      ])

      setJob(selectedJob)
      setLogs(nextLogs)
      setResultSummary(nextResultSummary)
      setDownloads(nextDownloads)
      setPageError(null)
    } catch (error) {
      setPageError(getErrorMessage(error))
    } finally {
      setIsLoading(false)
    }
  }

  usePollingTask(() => loadCurrentTask(!isLoading), {
    intervalMs: 4000,
  })

  if (isLoading && !job && !pageError) {
    return (
      <EmptyState
        description="正在加载当前任务、结果摘要和关键进展。"
        title="当前任务加载中"
      />
    )
  }

  if (pageError && !job) {
    return (
      <EmptyState
        actionLabel="新建翻译"
        actionTo="/translations/new"
        description={pageError}
        title="无法读取当前任务"
      />
    )
  }

  if (!job) {
    return (
      <EmptyState
        actionLabel="新建翻译"
        actionTo="/translations/new"
        description="当前还没有任务记录。先创建一个 YouTube 视频翻译任务，这里会集中显示状态、下一步和结果入口。"
        title="当前没有任务"
      />
    )
  }

  const reviewPrompt = getReviewPrompt(job.reviewGate, job.currentStage)
  const reviewAction = getReviewActionForJob(job.reviewGate, job.currentStage, job.id)
  const stageItems = buildStageProgress(job.status, job.currentStage)
  const isWaitingForReview = job.status === 'waiting_for_review'
  const isSucceeded = job.status === 'succeeded'
  const isFailed = job.status === 'failed'
  const displayTitle = getJobDisplayTitle(job)
  const secondaryLabel = getJobSecondaryLabel(job)
  const progressMessage = isWaitingForReview
    ? reviewPrompt.message
    : getUserFacingProgressMessage(job.progressMessage) ?? '任务正在继续处理，请稍候。'
  const availableDownloadCount = downloads.filter((item) => item.available).length
  const primaryAvailableCount = downloads.filter(
    (item) => item.available && primaryResultKeys.has(item.key),
  ).length
  const secondaryAvailableCount = Math.max(availableDownloadCount - primaryAvailableCount, 0)
  const hasResultRecord = Boolean(
    resultSummary &&
      (resultSummary.artifactCount > 0 || resultSummary.outputs.some((output) => output.exists)),
  )

  const resultHeadline = getResultHeadline({
    hasResultRecord,
    isFailed,
    isSucceeded,
    isWaitingForReview,
    primaryAvailableCount,
  })
  const resultDescription = getResultDescription({
    availableDownloadCount,
    hasResultRecord,
    isFailed,
    isSucceeded,
    isWaitingForReview,
    primaryAvailableCount,
    resultSummary,
    errorSummary: job.errorSummary,
  })

  return (
    <div className="space-y-6">
      <section className="surface-card p-6 lg:p-8">
        <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-3">
            <p className="eyebrow">当前任务</p>
            <h2 className="section-title">{displayTitle}</h2>
            <p className="text-sm font-medium text-ink-900/62">{secondaryLabel}</p>
            <p className="muted-copy max-w-3xl">{progressMessage}</p>
          </div>
          <StatusBadge status={job.status} />
        </div>

        <div className="mt-6 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <SummaryTile label="当前状态" value={JOB_STATUS_LABELS[job.status]} />
          <SummaryTile label="当前阶段" value={getStageLabel(job.currentStage)} />
          <SummaryTile label="下一步" value={getNextActionLabel(job, primaryAvailableCount)} />
          <SummaryTile label="最近更新" value={formatDateTime(job.updatedAt)} />
        </div>

        <div className="mt-6 flex flex-wrap gap-3">
          {isWaitingForReview && reviewAction.nativeRoute ? (
            <Link className="primary-button" to={reviewAction.nativeRoute}>
              {reviewAction.label}
            </Link>
          ) : (
            <Link
              className="primary-button"
              to={`/projects/${job.id}${availableDownloadCount > 0 ? '#result-downloads' : ''}`}
            >
              {availableDownloadCount > 0 ? '查看结果与下载' : '查看项目详情'}
            </Link>
          )}

          {!isWaitingForReview ? (
            <Link className="secondary-button" to="/translations/new">
              新建翻译
            </Link>
          ) : null}

          <Link className="secondary-button" to={`/projects/${job.id}`}>
            项目详情
          </Link>
        </div>
      </section>

      {pageError ? (
        <section className="notice-panel border border-coral-500/20 bg-coral-500/8">
          <p className="text-sm font-semibold text-coral-700">刷新当前任务失败</p>
          <p className="mt-2 text-sm text-coral-700/85">{pageError}</p>
        </section>
      ) : null}

      {isWaitingForReview ? (
        <section className="sticky top-4 z-20 overflow-hidden rounded-[32px] border border-coral-500/18 bg-white/92 p-5 shadow-[0_24px_70px_-38px_rgba(25,37,47,0.55)] backdrop-blur">
          <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(120deg,rgba(215,116,71,0.12),transparent_42%,rgba(45,139,115,0.12))]" />
          <div className="relative flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="space-y-2">
              <p className="eyebrow text-coral-700">请先处理审核</p>
              <h3 className="text-2xl font-semibold text-ink-950">{reviewPrompt.title}</h3>
              <p className="text-sm leading-6 text-ink-900/78">{reviewPrompt.message}</p>
              <p className="text-sm font-medium text-ink-900/62">{reviewAction.description}</p>
            </div>

            <div className="flex flex-wrap gap-3">
              {reviewAction.nativeRoute ? (
                <Link className="primary-button" to={reviewAction.nativeRoute}>
                  {reviewAction.label}
                </Link>
              ) : (
                <span className="inline-flex rounded-full bg-ink-950/8 px-4 py-2 text-sm font-semibold text-ink-900/55">
                  当前审核入口准备中
                </span>
              )}
              <Link className="secondary-button" to={`/projects/${job.id}`}>
                项目详情
              </Link>
            </div>
          </div>
        </section>
      ) : null}

      <div className="relative overflow-hidden rounded-[32px]">
        {isWaitingForReview ? (
          <div className="pointer-events-none absolute inset-0 z-10 rounded-[32px] bg-ink-950/12" />
        ) : null}

        <div className={isWaitingForReview ? 'pointer-events-none opacity-40' : ''}>
          <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(280px,0.38fr)]">
            <div className="space-y-6">
              <StageProgress items={stageItems} />

              <section className="surface-card p-5">
                <p className="eyebrow">结果概览</p>
                <h3 className="mt-2 text-xl font-semibold text-ink-950">{resultHeadline}</h3>
                <p className="mt-2 text-sm leading-6 text-ink-900/70">{resultDescription}</p>

                <div className="mt-5 grid gap-4 md:grid-cols-3">
                  <SummaryTile
                    label="主结果"
                    value={primaryAvailableCount > 0 ? `${primaryAvailableCount} 项可下载` : '尚未就绪'}
                  />
                  <SummaryTile
                    label="补充结果"
                    value={
                      secondaryAvailableCount > 0
                        ? `${secondaryAvailableCount} 项可查看`
                        : hasResultRecord
                          ? '已有补充产物记录'
                          : '当前没有补充结果'
                    }
                  />
                  <SummaryTile
                    label="结果入口"
                    value={availableDownloadCount > 0 ? '进入项目详情查看与下载' : '先关注当前阶段与下一步'}
                  />
                </div>

                <div className="mt-5 flex flex-wrap gap-3">
                  <Link
                    className="primary-button"
                    to={`/projects/${job.id}${availableDownloadCount > 0 ? '#result-downloads' : ''}`}
                  >
                    {availableDownloadCount > 0 ? '查看结果与下载' : '查看项目详情'}
                  </Link>
                  {!isWaitingForReview ? (
                    <Link className="secondary-button" to="/translations/new">
                      新建翻译
                    </Link>
                  ) : null}
                </div>
              </section>

              {availableDownloadCount > 0 ? <ResultDownloadList items={downloads} /> : null}

              {isFailed ? (
                <section className="surface-card p-5">
                  <p className="eyebrow">处理失败</p>
                  <h3 className="mt-2 text-xl font-semibold text-coral-700">
                    {getErrorCategory(job.errorSummary).label}
                  </h3>
                  <p className="mt-2 text-sm leading-6 text-coral-700/85">
                    {getErrorSummaryMessage(job.errorSummary)}
                  </p>
                  <div className="mt-3 rounded-2xl border border-amber-500/15 bg-amber-500/5 px-4 py-3">
                    <p className="text-sm font-medium text-ink-950">建议</p>
                    <p className="mt-1 text-sm text-ink-900/70">
                      {getErrorCategory(job.errorSummary).suggestion}
                    </p>
                  </div>
                  <div className="mt-4 flex flex-wrap gap-3">
                    <Link className="secondary-button" to={`/projects/${job.id}`}>
                      查看项目详情
                    </Link>
                    <Link className="secondary-button" to="/translations/new">
                      重新创建任务
                    </Link>
                  </div>
                </section>
              ) : null}

              <LogViewer
                description="日志区只保留最近 5 条关键进展，避免它压过当前状态、结果入口和下一步操作。"
                emptyMessage="当前还没有关键进展可显示。"
                entries={logs}
                initialVisibleCount={5}
                title="关键进展"
              />
            </div>

            <div className="space-y-6">
              <ConfigSummaryCard
                description="输入区只保留理解当前任务所需的少量上下文，不让技术字段抢走结果入口。"
                items={[
                  {
                    label: '来源',
                    value: secondaryLabel,
                  },
                  {
                    label: '说话人数',
                    value: job.speakers,
                  },
                  {
                    label: '音色 A / B',
                    value: `${job.voiceA ?? '-'} / ${job.voiceB ?? '-'}`,
                  },
                  {
                    label: '输出目标',
                    value: job.outputTarget,
                  },
                ]}
                title="任务输入"
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function SummaryTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-3xl border border-ink-950/8 bg-sand-50/75 p-4">
      <p className="eyebrow">{label}</p>
      <p className="mt-2 text-sm font-semibold text-ink-950">{value}</p>
    </div>
  )
}

function getNextActionLabel(job: JobSummary, primaryAvailableCount: number) {
  if (job.status === 'waiting_for_review') {
    return '先完成当前审核'
  }

  if (job.status === 'succeeded' || primaryAvailableCount > 0) {
    return '查看结果与下载'
  }

  if (job.status === 'failed') {
    return '查看失败说明'
  }

  return '等待处理完成'
}

function getResultHeadline({
  hasResultRecord,
  isFailed,
  isSucceeded,
  isWaitingForReview,
  primaryAvailableCount,
}: {
  hasResultRecord: boolean
  isFailed: boolean
  isSucceeded: boolean
  isWaitingForReview: boolean
  primaryAvailableCount: number
}) {
  if (primaryAvailableCount > 0) {
    return '主结果已经可以查看与下载'
  }

  if (isFailed) {
    return '这次任务没有生成可用结果'
  }

  if (isWaitingForReview) {
    return '先完成当前审核，结果会继续向前推进'
  }

  if (isSucceeded && hasResultRecord) {
    return '结果已经生成，下载入口正在整理'
  }

  if (hasResultRecord) {
    return '结果正在持续生成中'
  }

  return '当前还没有可查看的结果'
}

function getResultDescription({
  availableDownloadCount,
  errorSummary,
  hasResultRecord,
  isFailed,
  isSucceeded,
  isWaitingForReview,
  primaryAvailableCount,
  resultSummary,
}: {
  availableDownloadCount: number
  errorSummary: JobSummary['errorSummary']
  hasResultRecord: boolean
  isFailed: boolean
  isSucceeded: boolean
  isWaitingForReview: boolean
  primaryAvailableCount: number
  resultSummary: ProjectResultSummary | null
}) {
  if (primaryAvailableCount > 0) {
    return `当前已有 ${availableDownloadCount} 项可下载结果，其中 ${primaryAvailableCount} 项属于主结果。建议直接进入项目详情统一查看和下载。`
  }

  if (isFailed) {
    return getErrorSummaryMessage(errorSummary)
  }

  if (isWaitingForReview) {
    return '当前任务还在等待审核，最终结果会在审核通过后继续生成。先处理审核，再回到项目详情查看结果。'
  }

  if (isSucceeded && hasResultRecord) {
    return `当前已经生成 ${resultSummary?.artifactCount ?? 0} 项结果记录，但主下载入口还没有完全就绪。建议先进入项目详情查看结果摘要。`
  }

  if (hasResultRecord) {
    return `当前已有 ${resultSummary?.artifactCount ?? 0} 项结果记录，结果区会在主结果可用时优先展示下载入口。`
  }

  return '当前还没有结果产出。先关注阶段进度和关键进展，结果可用后会优先出现在这里。'
}

function formatDateTime(value: string) {
  const parsed = new Date(value)

  if (Number.isNaN(parsed.getTime())) {
    return value
  }

  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed)
}

function getErrorMessage(error: unknown) {
  if (error instanceof ApiError) {
    return error.message
  }

  if (error instanceof Error) {
    return error.message
  }

  return '请求失败，请稍后重试。'
}
