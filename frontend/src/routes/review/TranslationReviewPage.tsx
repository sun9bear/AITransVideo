import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { ConfigSummaryCard } from '@/components/ConfigSummaryCard'
import { EmptyState } from '@/components/EmptyState'
import { StatusBadge } from '@/components/StatusBadge'
import {
  getReviewPageMessage,
  getStageLabel,
  getUserFacingProgressMessage,
} from '@/features/jobs/presentation'
import { ApiError } from '@/lib/api/client'
import { getJob } from '@/lib/api/jobs'
import { approveTranslationReview, getTranslationReview } from '@/lib/api/reviews'
import { usePollingTask } from '@/lib/react/usePollingTask'
import { ACTIVE_JOB_STATUSES, type JobSummary } from '@/types/jobs'
import type { TranslationReviewResource } from '@/types/reviews'

type TranslationSegmentState = Record<
  string,
  {
    cnText: string
    rewriteRequested: boolean
    translationConfirmed: boolean
    ttsCnText: string
    updatedAt: string
  }
>

export function TranslationReviewPage() {
  const params = useParams()
  const jobId = params.jobId?.trim() ?? ''
  const [resource, setResource] = useState<TranslationReviewResource | null>(null)
  const [segments, setSegments] = useState<TranslationSegmentState>({})
  const [submittedJob, setSubmittedJob] = useState<JobSummary | null>(null)
  const [pageSize, setPageSize] = useState(20)
  const [page, setPage] = useState(1)
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [pageError, setPageError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false

    const load = async () => {
      if (!jobId) {
        setPageError('缺少任务标识。')
        setIsLoading(false)
        return
      }

      setIsLoading(true)
      try {
        const nextResource = await getTranslationReview(jobId)
        if (cancelled) {
          return
        }

        setResource(nextResource)
        setPageSize(nextResource.defaultPageSize)
        setPage(1)
        setSegments(
          Object.fromEntries(
            nextResource.items.map((item) => [
              item.segmentId,
              {
                cnText: item.cnText,
                rewriteRequested: item.rewriteRequested,
                translationConfirmed: item.translationConfirmed,
                ttsCnText: item.ttsCnText,
                updatedAt: item.reviewUpdatedAt ?? '',
              },
            ]),
          ),
        )
        setSubmittedJob(null)
        setPageError(null)
      } catch (error) {
        if (!cancelled) {
          setPageError(getErrorMessage(error))
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false)
        }
      }
    }

    void load()

    return () => {
      cancelled = true
    }
  }, [jobId])

  usePollingTask(
    async () => {
      if (!jobId) {
        return
      }

      try {
        const nextJob = await getJob(jobId)
        setSubmittedJob(nextJob)
      } catch {
        return
      }
    },
    {
      enabled: Boolean(submittedJob && ACTIVE_JOB_STATUSES.includes(submittedJob.status)),
      immediate: false,
      intervalMs: 3000,
    },
  )

  const activeJob = submittedJob ?? resource?.job ?? null
  const hasAdvanced =
    submittedJob !== null &&
    (submittedJob.status !== 'waiting_for_review' ||
      submittedJob.currentStage !== 'translation_review')
  const totalItems = resource?.items.length ?? 0
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize))
  const currentPage = Math.min(page, totalPages)
  const hasPagination = totalItems > 20
  const visibleItems = resource?.items.slice((currentPage - 1) * pageSize, currentPage * pageSize) ?? []
  const confirmedCount = resource
    ? resource.items.filter((item) => segments[item.segmentId]?.translationConfirmed).length
    : 0
  const rewriteCount = resource
    ? resource.items.filter((item) => segments[item.segmentId]?.rewriteRequested).length
    : 0

  const handleApprove = async () => {
    if (!resource) {
      return
    }

    setIsSubmitting(true)
    setSubmitError(null)
    try {
      const now = new Date().toISOString()
      const result = await approveTranslationReview({
        jobId,
        projectDir: resource.projectDir,
        segments: Object.fromEntries(
          resource.items.map((item) => {
            const current = segments[item.segmentId] ?? {
              cnText: item.cnText,
              rewriteRequested: item.rewriteRequested,
              translationConfirmed: item.translationConfirmed,
              ttsCnText: item.ttsCnText,
              updatedAt: item.reviewUpdatedAt ?? '',
            }

            return [
              item.segmentId,
              {
                cnText: current.cnText,
                rewriteRequested: current.rewriteRequested,
                translationConfirmed: current.translationConfirmed,
                ttsCnText: current.ttsCnText,
                updatedAt: current.updatedAt || now,
              },
            ]
          }),
        ),
      })

      setSubmittedJob(result.job)
    } catch (error) {
      setSubmitError(getErrorMessage(error))
    } finally {
      setIsSubmitting(false)
    }
  }

  if (!jobId) {
    return (
      <EmptyState
        actionLabel="返回当前任务"
        actionTo="/tasks/current"
        description="请从当前任务或项目详情进入翻译审核。"
        title="缺少任务标识"
      />
    )
  }

  if (isLoading && !resource && !pageError) {
    return (
      <EmptyState
        description="正在读取当前翻译审核内容..."
        title="翻译审核加载中"
      />
    )
  }

  if (pageError && !resource) {
    return (
      <EmptyState
        actionLabel="返回当前任务"
        actionTo="/tasks/current"
        description={pageError}
        title="无法打开翻译审核"
      />
    )
  }

  if (!resource) {
    return (
      <EmptyState
        actionLabel="返回当前任务"
        actionTo="/tasks/current"
        description="当前没有可展示的翻译审核内容。"
        title="当前没有待处理的翻译审核"
      />
    )
  }

  return (
    <div className="space-y-6">
      {!hasAdvanced ? (
        <section className="sticky top-4 z-20 surface-card p-5 shadow-[0_24px_60px_-42px_rgba(25,37,47,0.6)]">
          <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-3">
                <h2 className="text-2xl font-semibold text-ink-950">翻译审核</h2>
                <StatusBadge status={activeJob?.status ?? resource.job.status} />
                <span className="rounded-full bg-sand-100 px-3 py-1 text-xs font-semibold text-ink-900/65">
                  待处理 {resource.items.length} 条
                </span>
              </div>
              <p className="text-sm leading-6 text-ink-900/75">
                {getReviewPageMessage('translation_review', resource.activeMessage)}
              </p>
              <p className="text-sm font-medium text-ink-900/60">
                当前必须完成：确认翻译与配音文本，必要时标记重写。
              </p>
            </div>

            <div className="flex flex-wrap gap-3">
              <button
                className="primary-button"
                disabled={isSubmitting}
                onClick={() => {
                  void handleApprove()
                }}
                type="button"
              >
                {isSubmitting ? '提交中...' : '确认并继续'}
              </button>
              <Link className="secondary-button" to="/tasks/current">
                返回当前任务
              </Link>
              <Link className="secondary-button" to={`/projects/${jobId}`}>
                查看项目详情
              </Link>
            </div>
          </div>

          {hasPagination ? (
            <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-ink-950/8 pt-4 text-sm text-ink-900/70">
              <span>
                第 {currentPage} / {totalPages} 页
              </span>
              <span>
                当前显示 {Math.min((currentPage - 1) * pageSize + 1, totalItems)}-
                {Math.min(currentPage * pageSize, totalItems)} / {totalItems}
              </span>
              <label className="flex items-center gap-2">
                <span>每页</span>
                <select
                  className="form-input min-w-[96px] py-2"
                  onChange={(event) => {
                    setPageSize(Number(event.currentTarget.value))
                    setPage(1)
                  }}
                  value={pageSize}
                >
                  {resource.pageSizeOptions.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
              <div className="flex gap-2">
                <button
                  className="secondary-button px-4 py-2"
                  disabled={currentPage <= 1}
                  onClick={() => {
                    setPage((currentValue) => Math.max(1, currentValue - 1))
                  }}
                  type="button"
                >
                  上一页
                </button>
                <button
                  className="secondary-button px-4 py-2"
                  disabled={currentPage >= totalPages}
                  onClick={() => {
                    setPage((currentValue) => Math.min(totalPages, currentValue + 1))
                  }}
                  type="button"
                >
                  下一页
                </button>
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      {submitError ? (
        <section className="notice-panel border border-coral-500/20 bg-coral-500/8">
          <p className="text-sm font-semibold text-coral-700">提交翻译审核失败</p>
          <p className="mt-2 text-sm text-coral-700/85">{submitError}</p>
        </section>
      ) : null}

      {hasAdvanced && submittedJob ? (
        <section className="surface-card p-6">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="space-y-2">
              <p className="eyebrow">审核已提交</p>
              <h3 className="text-xl font-semibold text-ink-950">翻译审核已完成</h3>
              <p className="text-sm leading-6 text-ink-900/70">
                当前任务已推进到 {getStageLabel(submittedJob.currentStage)}。
                {getUserFacingProgressMessage(submittedJob.progressMessage)
                  ? ` ${getUserFacingProgressMessage(submittedJob.progressMessage)}`
                  : ''}
              </p>
            </div>
            <StatusBadge status={submittedJob.status} />
          </div>

          <div className="mt-5 flex flex-wrap gap-3">
            {submittedJob.status === 'waiting_for_review' &&
            submittedJob.currentStage === 'voice_review' ? (
              <Link className="primary-button" to={`/reviews/${jobId}/voice`}>
                继续处理音色确认
              </Link>
            ) : null}
            <Link className="secondary-button" to="/tasks/current">
              返回当前任务
            </Link>
            <Link className="secondary-button" to={`/projects/${jobId}`}>
              查看项目详情
            </Link>
          </div>
        </section>
      ) : null}

      {!hasAdvanced ? (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(240px,0.3fr)]">
          <section className="surface-card p-5">
            <div className="space-y-1">
              <h3 className="text-lg font-semibold text-ink-950">待确认翻译</h3>
              <p className="muted-copy">
                首屏优先显示逐条待处理内容，概览和统计放到侧边栏。
              </p>
            </div>

            <div className="mt-5 space-y-4">
              {visibleItems.map((item) => {
                const current = segments[item.segmentId] ?? {
                  cnText: item.cnText,
                  rewriteRequested: item.rewriteRequested,
                  translationConfirmed: item.translationConfirmed,
                  ttsCnText: item.ttsCnText,
                  updatedAt: item.reviewUpdatedAt ?? '',
                }

                return (
                  <article
                    key={item.segmentId}
                    className="rounded-3xl border border-ink-950/8 bg-sand-50/70 p-4"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p className="eyebrow">片段 {item.segmentId}</p>
                        <p className="mt-1 text-sm font-semibold text-ink-950">
                          {item.displayName || item.speakerId}
                        </p>
                      </div>
                      <span className="rounded-full bg-sand-100 px-3 py-1 text-xs font-semibold text-ink-900/65">
                        {item.reviewUpdatedAt ? formatDateTime(item.reviewUpdatedAt) : '待确认'}
                      </span>
                    </div>

                    <div className="mt-4 space-y-4">
                      <div className="space-y-2">
                        <p className="form-label">原文</p>
                        <div className="rounded-2xl border border-ink-950/8 bg-white/75 px-4 py-3 text-sm leading-6 text-ink-900/80">
                          {item.sourceText || '-'}
                        </div>
                      </div>

                      <label className="space-y-2">
                        <span className="form-label">中文译文</span>
                        <textarea
                          className="form-input min-h-[132px] resize-y"
                          onChange={(event) => {
                            const nextValue = event.currentTarget.value
                            setSegments((currentState) => ({
                              ...currentState,
                              [item.segmentId]: {
                                ...(currentState[item.segmentId] ?? current),
                                cnText: nextValue,
                                updatedAt: new Date().toISOString(),
                              },
                            }))
                          }}
                          value={current.cnText}
                        />
                      </label>

                      <label className="space-y-2">
                        <span className="form-label">配音文本</span>
                        <textarea
                          className="form-input min-h-[132px] resize-y"
                          onChange={(event) => {
                            const nextValue = event.currentTarget.value
                            setSegments((currentState) => ({
                              ...currentState,
                              [item.segmentId]: {
                                ...(currentState[item.segmentId] ?? current),
                                ttsCnText: nextValue,
                                updatedAt: new Date().toISOString(),
                              },
                            }))
                          }}
                          value={current.ttsCnText}
                        />
                      </label>

                      <div className="grid gap-3 sm:grid-cols-2">
                        <label className="rounded-2xl border border-ink-950/8 bg-white/75 px-4 py-3 text-sm text-ink-900/75">
                          <div className="flex items-center gap-3">
                            <input
                              checked={current.translationConfirmed}
                              onChange={(event) => {
                                const checked = event.currentTarget.checked
                                setSegments((currentState) => ({
                                  ...currentState,
                                  [item.segmentId]: {
                                    ...(currentState[item.segmentId] ?? current),
                                    translationConfirmed: checked,
                                    updatedAt: new Date().toISOString(),
                                  },
                                }))
                              }}
                              type="checkbox"
                            />
                            <span>翻译已确认</span>
                          </div>
                        </label>

                        <label className="rounded-2xl border border-ink-950/8 bg-white/75 px-4 py-3 text-sm text-ink-900/75">
                          <div className="flex items-center gap-3">
                            <input
                              checked={current.rewriteRequested}
                              onChange={(event) => {
                                const checked = event.currentTarget.checked
                                setSegments((currentState) => ({
                                  ...currentState,
                                  [item.segmentId]: {
                                    ...(currentState[item.segmentId] ?? current),
                                    rewriteRequested: checked,
                                    updatedAt: new Date().toISOString(),
                                  },
                                }))
                              }}
                              type="checkbox"
                            />
                            <span>标记为需重写</span>
                          </div>
                        </label>
                      </div>
                    </div>
                  </article>
                )
              })}
            </div>
          </section>

          <div className="space-y-6">
            <ConfigSummaryCard
              description="侧边栏只保留本轮翻译审核的整体进度。"
              items={[
                {
                  label: '当前阶段',
                  value: getStageLabel(resource.job.currentStage),
                },
                {
                  label: '待处理片段',
                  value: String(resource.items.length),
                },
                {
                  label: '已确认片段',
                  value: String(confirmedCount),
                },
                {
                  label: '标记重写',
                  value: String(rewriteCount),
                },
              ]}
              title="审核概览"
            />
          </div>
        </div>
      ) : null}
    </div>
  )
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
