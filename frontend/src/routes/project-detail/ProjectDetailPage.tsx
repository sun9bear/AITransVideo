import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { ConfigSummaryCard } from '@/components/ConfigSummaryCard'
import { EmptyState } from '@/components/EmptyState'
import { LogViewer } from '@/components/LogViewer'
import { ResultDownloadList } from '@/components/ResultDownloadList'
import { StatusBadge } from '@/components/StatusBadge'
import {
  getErrorSummaryMessage,
  getJobDisplayTitle,
  getJobSecondaryLabel,
  getReviewActionForJob,
  getReviewPrompt,
  getStageLabel,
  getUserFacingProgressMessage,
} from '@/features/jobs/presentation'
import { selectCurrentTaskJob } from '@/features/jobs/selectors'
import { ApiError } from '@/lib/api/client'
import { getJobLogs, getProjectDetail, listJobs } from '@/lib/api/jobs'
import { usePollingTask } from '@/lib/react/usePollingTask'
import {
  ACTIVE_JOB_STATUSES,
  JOB_STATUS_LABELS,
  type JobLogEntry,
  type ProjectDetailResource,
} from '@/types/jobs'

const primaryResultKeys = new Set([
  'publish.dubbed_video',
  'editor.dubbed_audio_complete',
  'editor.subtitles',
])

export function ProjectDetailPage() {
  const params = useParams()
  const jobId = params.jobId?.trim() ?? ''
  const [detail, setDetail] = useState<ProjectDetailResource | null>(null)
  const [logs, setLogs] = useState<JobLogEntry[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)

  const loadDetail = async (silent = false) => {
    if (!jobId) {
      setPageError('缺少任务编号。')
      setIsLoading(false)
      return
    }

    if (!silent) {
      setIsLoading(true)
    }

    try {
      const resolvedJobId =
        jobId === 'latest' ? selectCurrentTaskJob(await listJobs())?.id ?? null : jobId

      if (!resolvedJobId) {
        setDetail(null)
        setLogs([])
        setPageError('当前还没有任务记录，暂时无法打开项目详情。')
        return
      }

      const [nextDetail, nextLogs] = await Promise.all([
        getProjectDetail(resolvedJobId),
        getJobLogs(resolvedJobId),
      ])

      setDetail(nextDetail)
      setLogs(nextLogs)
      setPageError(null)
    } catch (error) {
      setPageError(getErrorMessage(error))
    } finally {
      setIsLoading(false)
    }
  }

  usePollingTask(() => loadDetail(!isLoading), {
    enabled: Boolean(jobId),
    intervalMs: 5000,
  })

  if (!jobId) {
    return (
      <EmptyState
        actionLabel="返回当前任务"
        actionTo="/tasks/current"
        description="项目详情页需要带着任务编号进入。"
        title="缺少任务编号"
      />
    )
  }

  if (isLoading && !detail && !pageError) {
    return (
      <EmptyState
        description="正在加载项目详情、结果摘要、下载入口和关键日志。"
        title="项目详情加载中"
      />
    )
  }

  if (pageError && !detail) {
    return (
      <EmptyState
        actionLabel="返回当前任务"
        actionTo="/tasks/current"
        description={pageError}
        title="无法读取项目详情"
      />
    )
  }

  if (!detail) {
    return (
      <EmptyState
        actionLabel="返回当前任务"
        actionTo="/tasks/current"
        description="当前没有可展示的项目详情。"
        title="没有项目详情"
      />
    )
  }

  const reviewPrompt = getReviewPrompt(detail.job.reviewGate, detail.job.currentStage)
  const reviewAction = getReviewActionForJob(
    detail.job.reviewGate,
    detail.job.currentStage,
    detail.job.id,
  )
  const isWaitingForReview = detail.job.status === 'waiting_for_review'
  const isActiveJob = ACTIVE_JOB_STATUSES.includes(detail.job.status)
  const displayTitle = getJobDisplayTitle(detail.job)
  const secondaryLabel = getJobSecondaryLabel(detail.job)
  const availableDownloadCount = detail.downloads.filter((item) => item.available).length
  const primaryAvailableCount = detail.downloads.filter(
    (item) => item.available && primaryResultKeys.has(item.key),
  ).length
  const secondaryAvailableCount = Math.max(availableDownloadCount - primaryAvailableCount, 0)
  const resultHeadline = getResultHeadline({
    availableDownloadCount,
    isActiveJob,
    isFailed: detail.job.status === 'failed',
    isWaitingForReview,
  })
  const resultDescription = getResultDescription({
    availableDownloadCount,
    detail,
    isActiveJob,
    isWaitingForReview,
    primaryAvailableCount,
  })
  const headerMessage =
    detail.job.status === 'waiting_for_review'
      ? reviewPrompt.message
      : getUserFacingProgressMessage(detail.job.progressMessage) ??
        '这里集中展示当前任务的结果摘要、下载入口和少量关键日志。'

  return (
    <div className="space-y-6">
      <section className="surface-card p-6 lg:p-8">
        <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-3">
            <p className="eyebrow">项目详情</p>
            <h2 className="section-title">{displayTitle}</h2>
            <p className="text-sm font-medium text-ink-900/62">{secondaryLabel}</p>
            <p className="muted-copy max-w-3xl">{headerMessage}</p>
          </div>
          <StatusBadge status={detail.job.status} />
        </div>

        <div className="mt-6 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <SummaryTile label="当前状态" value={JOB_STATUS_LABELS[detail.job.status]} />
          <SummaryTile label="当前阶段" value={getStageLabel(detail.job.currentStage)} />
          <SummaryTile
            label="结果摘要"
            value={
              availableDownloadCount > 0
                ? `${availableDownloadCount} 项可下载`
                : detail.job.status === 'failed'
                  ? '当前没有可用结果'
                  : '结果仍在生成中'
            }
          />
          <SummaryTile label="最近更新" value={formatDateTime(detail.job.updatedAt)} />
        </div>

        <div className="mt-6 flex flex-wrap gap-3">
          {isWaitingForReview && reviewAction.nativeRoute ? (
            <Link className="primary-button" to={reviewAction.nativeRoute}>
              {reviewAction.label}
            </Link>
          ) : availableDownloadCount > 0 ? (
            <a className="primary-button" href="#result-downloads">
              查看结果与下载
            </a>
          ) : (
            <Link className="primary-button" to="/tasks/current">
              返回当前任务
            </Link>
          )}

          <Link className="secondary-button" to="/tasks/current">
            当前任务
          </Link>
          <Link className="secondary-button" to="/translations/new">
            新建翻译
          </Link>
        </div>
      </section>

      {pageError ? (
        <section className="notice-panel border border-coral-500/20 bg-coral-500/8">
          <p className="text-sm font-semibold text-coral-700">刷新项目详情失败</p>
          <p className="mt-2 text-sm text-coral-700/85">{pageError}</p>
        </section>
      ) : null}

      {isWaitingForReview ? (
        <section className="surface-card p-6">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="space-y-2">
              <p className="eyebrow">等待审核</p>
              <h3 className="text-xl font-semibold text-ink-950">{reviewPrompt.title}</h3>
              <p className="text-sm leading-6 text-ink-900/70">{reviewPrompt.message}</p>
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
              <Link className="secondary-button" to="/tasks/current">
                返回当前任务
              </Link>
            </div>
          </div>
        </section>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(260px,0.35fr)]">
        <div className="space-y-6">
          <section className="surface-card p-5">
            <p className="eyebrow">结果摘要</p>
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
                    : detail.result.artifactCount > 0
                      ? '已有补充产物记录'
                      : '当前没有补充结果'
                }
              />
              <SummaryTile
                label="日志区"
                value={logs.length > 0 ? `保留最近 ${Math.min(logs.length, 5)} 条关键日志` : '当前没有关键日志'}
              />
            </div>
          </section>

          <section id="result-downloads">
            <ResultDownloadList items={detail.downloads} />
          </section>

          {detail.job.status === 'failed' ? (
            <section className="surface-card p-5">
              <p className="eyebrow">处理失败</p>
              <h3 className="mt-2 text-xl font-semibold text-coral-700">这次任务没有完成</h3>
              <p className="mt-2 text-sm leading-6 text-coral-700/85">
                {getErrorSummaryMessage(detail.job.errorSummary)}
              </p>
            </section>
          ) : null}

          <LogViewer
            description="日志区只保留少量关键记录，避免状态、结果和下载入口被技术输出压住。"
            emptyMessage="当前没有关键日志可显示。"
            entries={logs}
            initialVisibleCount={5}
            title="关键日志"
          />

          <details className="surface-card p-5">
            <summary className="cursor-pointer text-sm font-semibold text-ink-950">
              查看内部文件位置与补充结果
            </summary>

            <div className="mt-5 space-y-3">
              {detail.result.outputs.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-ink-950/12 bg-sand-50/80 px-4 py-5 text-sm text-ink-900/65">
                  当前没有可展示的内部结果记录。
                </div>
              ) : (
                detail.result.outputs.map((output) => (
                  <div
                    key={`${output.key}-${output.name}`}
                    className="rounded-2xl border border-ink-950/8 bg-sand-50/70 px-4 py-4"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="text-sm font-semibold text-ink-950">{output.name}</p>
                      <span className="rounded-full bg-sand-100 px-3 py-1 text-xs font-semibold text-ink-900/65">
                        {output.exists ? '已生成' : '暂缺'}
                      </span>
                    </div>
                    {output.path ? (
                      <p className="mt-2 break-all text-sm text-ink-900/62">{output.path}</p>
                    ) : (
                      <p className="mt-2 text-sm text-ink-900/62">当前没有返回文件位置。</p>
                    )}
                  </div>
                ))
              )}
            </div>
          </details>
        </div>

        <div className="space-y-6">
          <ConfigSummaryCard
            description="项目概览区只保留理解这条任务所需的少量上下文，不让路径和内部字段抢首屏。"
            items={[
              {
                label: '来源',
                value: secondaryLabel,
              },
              {
                label: '说话人数',
                value: detail.job.speakers,
              },
              {
                label: '音色 A / B',
                value: `${detail.job.voiceA ?? '-'} / ${detail.job.voiceB ?? '-'}`,
              },
              {
                label: '输出目标',
                value: detail.job.outputTarget,
              },
            ]}
            title="项目概览"
          />

          <ConfigSummaryCard
            description="时间与状态保留在次级区，帮助回看项目但不压过结果与下载入口。"
            items={[
              {
                label: '创建时间',
                value: formatDateTime(detail.job.createdAt),
              },
              {
                label: '最近更新',
                value: formatDateTime(detail.job.updatedAt),
              },
              {
                label: '任务状态',
                value: JOB_STATUS_LABELS[detail.job.status],
              },
              {
                label: '当前阶段',
                value: getStageLabel(detail.job.currentStage),
              },
            ]}
            title="时间与状态"
          />
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

function getResultHeadline({
  availableDownloadCount,
  isActiveJob,
  isFailed,
  isWaitingForReview,
}: {
  availableDownloadCount: number
  isActiveJob: boolean
  isFailed: boolean
  isWaitingForReview: boolean
}) {
  if (availableDownloadCount > 0) {
    return '主结果已经可以查看与下载'
  }

  if (isFailed) {
    return '当前没有可用结果'
  }

  if (isWaitingForReview) {
    return '先完成当前审核，结果会继续生成'
  }

  if (isActiveJob) {
    return '结果仍在处理中'
  }

  return '当前还没有可下载结果'
}

function getResultDescription({
  availableDownloadCount,
  detail,
  isActiveJob,
  isWaitingForReview,
  primaryAvailableCount,
}: {
  availableDownloadCount: number
  detail: ProjectDetailResource
  isActiveJob: boolean
  isWaitingForReview: boolean
  primaryAvailableCount: number
}) {
  if (primaryAvailableCount > 0) {
    return `当前已有 ${availableDownloadCount} 项结果可下载，其中 ${primaryAvailableCount} 项属于主结果。默认先展示这些最常用的文件。`
  }

  if (detail.job.status === 'failed') {
    return getErrorSummaryMessage(detail.job.errorSummary)
  }

  if (isWaitingForReview) {
    return '当前任务正停在审核阶段。先处理审核，结果区会在后续阶段继续生成并更新下载入口。'
  }

  if (isActiveJob) {
    return `当前已有 ${detail.result.artifactCount} 项结果记录，结果和下载入口会在可用时继续更新。`
  }

  return '当前还没有可下载结果。你仍然可以先查看项目摘要和关键日志。'
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
    if (error.status === 404) {
      return '当前任务记录不存在。'
    }

    return error.message
  }

  if (error instanceof Error) {
    return error.message
  }

  return '请求失败，请稍后重试。'
}
