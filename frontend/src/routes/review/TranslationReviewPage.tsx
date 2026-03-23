import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { ConfigSummaryCard } from '@/components/ConfigSummaryCard'
import { EmptyState } from '@/components/EmptyState'
import { StatusBadge } from '@/components/StatusBadge'
import { Toast } from '@/components/Toast'
import {
  getReviewPageMessage,
  getStageLabel,
  getUserFacingProgressMessage,
} from '@/features/jobs/presentation'
import { ApiError } from '@/lib/api/client'
import { getJob } from '@/lib/api/jobs'
import { approveTranslationReview, getTranslationReview, splitSegment } from '@/lib/api/reviews'
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
  const [segmentSpeakers, setSegmentSpeakers] = useState<Record<string, string>>({})
  const [splittingSegmentId, setSplittingSegmentId] = useState<string | null>(null)
  const [splitSourcePos, setSplitSourcePos] = useState(0)
  const [splitCnPos, setSplitCnPos] = useState(0)
  const [splitSpeakerA, setSplitSpeakerA] = useState('')
  const [splitSpeakerB, setSplitSpeakerB] = useState('')
  const [isSplitting, setIsSplitting] = useState(false)
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
        segmentSpeakers,
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
                    window.scrollTo({ top: 0, behavior: 'smooth' })
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
                    window.scrollTo({ top: 0, behavior: 'smooth' })
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

      <Toast message={submitError} onClose={() => setSubmitError(null)} />

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
                      <div className="flex items-center gap-3">
                        <p className="eyebrow">片段 {item.segmentId}</p>
                        {resource.speakerOptions.length > 1 ? (
                          <select
                            className="form-input min-w-[140px] py-1 text-sm font-semibold"
                            onChange={(event) => {
                              const nextSpeakerId = event.currentTarget.value
                              const currentSpeakerId = segmentSpeakers[item.segmentId] ?? item.speakerId
                              if (nextSpeakerId === currentSpeakerId) return

                              // Ask if user wants to swap all subsequent segments
                              const currentIdx = resource.items.findIndex((i) => i.segmentId === item.segmentId)
                              const subsequentItems = resource.items.slice(currentIdx)
                              const hasMultipleSpeakers = resource.speakerOptions.length > 1
                              const affectedCount = subsequentItems.filter((i) => {
                                const spk = segmentSpeakers[i.segmentId] ?? i.speakerId
                                return spk === currentSpeakerId || spk === nextSpeakerId
                              }).length

                              if (hasMultipleSpeakers && affectedCount > 1 && window.confirm(
                                `是否将后续所有 "${resource.speakerOptions.find(o => o.id === currentSpeakerId)?.displayName || currentSpeakerId}" 替换为 "${resource.speakerOptions.find(o => o.id === nextSpeakerId)?.displayName || nextSpeakerId}"，同时互换？\n\n将影响从当前片段起的 ${affectedCount} 个片段。\n\n点击"确定"批量互换，"取消"仅修改当前片段。`
                              )) {
                                // Batch swap: A→B, B→A for all subsequent segments
                                setSegmentSpeakers((current) => {
                                  const updated = { ...current }
                                  for (const sub of subsequentItems) {
                                    const subSpeaker = updated[sub.segmentId] ?? sub.speakerId
                                    if (subSpeaker === currentSpeakerId) {
                                      updated[sub.segmentId] = nextSpeakerId
                                    } else if (subSpeaker === nextSpeakerId) {
                                      updated[sub.segmentId] = currentSpeakerId
                                    }
                                  }
                                  return updated
                                })
                              } else {
                                // Single change
                                setSegmentSpeakers((current) => ({
                                  ...current,
                                  [item.segmentId]: nextSpeakerId,
                                }))
                              }
                            }}
                            value={segmentSpeakers[item.segmentId] ?? item.speakerId}
                          >
                            {resource.speakerOptions.map((option) => (
                              <option key={option.id} value={option.id}>
                                {option.displayName}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <p className="text-sm font-semibold text-ink-950">
                            {item.displayName || item.speakerId}
                          </p>
                        )}
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
                        <span className="form-label">配音文本</span>
                        <textarea
                          className="form-input min-h-[48px] resize-y"
                          onChange={(event) => {
                            const nextValue = event.currentTarget.value
                            setSegments((currentState) => ({
                              ...currentState,
                              [item.segmentId]: {
                                ...(currentState[item.segmentId] ?? current),
                                cnText: nextValue,
                                ttsCnText: nextValue,
                                updatedAt: new Date().toISOString(),
                              },
                            }))
                          }}
                          onInput={(event) => {
                            const el = event.currentTarget
                            el.style.height = 'auto'
                            el.style.height = `${el.scrollHeight}px`
                          }}
                          ref={(el) => {
                            if (el) {
                              el.style.height = 'auto'
                              el.style.height = `${el.scrollHeight}px`
                            }
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

                      {/* Split segment button */}
                      <div className="flex justify-end">
                        <button
                          className="text-xs text-ink-900/50 hover:text-ink-900/80 underline"
                          onClick={() => {
                            if (splittingSegmentId === item.segmentId) {
                              setSplittingSegmentId(null)
                            } else {
                              setSplittingSegmentId(item.segmentId)
                              setSplitSourcePos(Math.floor((item.sourceText || '').length / 2))
                              setSplitCnPos(Math.floor((current.cnText || '').length / 2))
                              setSplitSpeakerA(segmentSpeakers[item.segmentId] ?? item.speakerId)
                              setSplitSpeakerB(resource.speakerOptions.length > 1
                                ? resource.speakerOptions.find(o => o.id !== item.speakerId)?.id ?? item.speakerId
                                : item.speakerId)
                            }
                          }}
                          type="button"
                        >
                          {splittingSegmentId === item.segmentId ? '取消拆分' : '拆分片段'}
                        </button>
                      </div>

                      {/* Split panel */}
                      {splittingSegmentId === item.segmentId ? (
                        <div className="rounded-2xl border-2 border-amber-400/40 bg-amber-50/50 p-4 space-y-4">
                          <p className="text-sm font-semibold text-ink-950">拆分片段 {item.segmentId}</p>
                          <p className="text-xs text-ink-900/60">
                            在原文中选择拆分位置，系统会将该片段拆成两段，各自分配不同的发言人。
                          </p>

                          <div className="space-y-2">
                            <p className="form-label">原文拆分位置（字符位置: {splitSourcePos}）</p>
                            <input
                              className="w-full"
                              max={(item.sourceText || '').length}
                              min={1}
                              onChange={(e) => {
                                const pos = Number(e.currentTarget.value)
                                setSplitSourcePos(pos)
                              }}
                              type="range"
                              value={splitSourcePos}
                            />
                            <div className="grid grid-cols-2 gap-2 text-xs">
                              <div className="rounded-xl bg-white/80 p-2 border border-ink-950/8">
                                <p className="font-semibold text-ink-900/60 mb-1">片段 A</p>
                                <p className="text-ink-900/80">{(item.sourceText || '').slice(0, splitSourcePos)}</p>
                              </div>
                              <div className="rounded-xl bg-white/80 p-2 border border-ink-950/8">
                                <p className="font-semibold text-ink-900/60 mb-1">片段 B</p>
                                <p className="text-ink-900/80">{(item.sourceText || '').slice(splitSourcePos)}</p>
                              </div>
                            </div>
                          </div>

                          <div className="space-y-2">
                            <p className="form-label">中文译文拆分位置（字符位置: {splitCnPos}）</p>
                            <input
                              className="w-full"
                              max={(current.cnText || '').length}
                              min={1}
                              onChange={(e) => setSplitCnPos(Number(e.currentTarget.value))}
                              type="range"
                              value={splitCnPos}
                            />
                            <div className="grid grid-cols-2 gap-2 text-xs">
                              <div className="rounded-xl bg-white/80 p-2 border border-ink-950/8">
                                <p className="text-ink-900/80">{(current.cnText || '').slice(0, splitCnPos)}</p>
                              </div>
                              <div className="rounded-xl bg-white/80 p-2 border border-ink-950/8">
                                <p className="text-ink-900/80">{(current.cnText || '').slice(splitCnPos)}</p>
                              </div>
                            </div>
                          </div>

                          <div className="grid grid-cols-2 gap-3">
                            <label className="space-y-1">
                              <span className="form-label">片段 A 发言人</span>
                              <select
                                className="form-input text-sm"
                                onChange={(e) => setSplitSpeakerA(e.currentTarget.value)}
                                value={splitSpeakerA}
                              >
                                {resource.speakerOptions.map((option) => (
                                  <option key={option.id} value={option.id}>{option.displayName}</option>
                                ))}
                              </select>
                            </label>
                            <label className="space-y-1">
                              <span className="form-label">片段 B 发言人</span>
                              <select
                                className="form-input text-sm"
                                onChange={(e) => setSplitSpeakerB(e.currentTarget.value)}
                                value={splitSpeakerB}
                              >
                                {resource.speakerOptions.map((option) => (
                                  <option key={option.id} value={option.id}>{option.displayName}</option>
                                ))}
                              </select>
                            </label>
                          </div>

                          <button
                            className="primary-button text-sm"
                            disabled={isSplitting}
                            onClick={async () => {
                              setIsSplitting(true)
                              try {
                                const result = await splitSegment({
                                  projectDir: resource.projectDir,
                                  segmentId: item.segmentId,
                                  splitSourceIndex: splitSourcePos,
                                  splitCnIndex: splitCnPos,
                                  speakerA: splitSpeakerA,
                                  speakerB: splitSpeakerB,
                                  stage: 'translation_review',
                                  pendingSpeakerChanges: segmentSpeakers,
                                })
                                if (result.success) {
                                  // Reload the entire page to get fresh data
                                  window.location.reload()
                                  return
                                }
                                setSubmitError('拆分未生效，请检查片段数据。')
                              } catch (error) {
                                setSubmitError(`拆分失败: ${getErrorMessage(error)}`)
                              } finally {
                                setIsSplitting(false)
                              }
                            }}
                            type="button"
                          >
                            {isSplitting ? '拆分中...' : '确认拆分'}
                          </button>
                        </div>
                      ) : null}
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
