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
import { Toast } from '@/components/Toast'
import { approveSpeakerReview, getSpeakerReview, splitSegment } from '@/lib/api/reviews'
import { usePollingTask } from '@/lib/react/usePollingTask'
import { ACTIVE_JOB_STATUSES, type JobSummary } from '@/types/jobs'
import type { SpeakerReviewResource } from '@/types/reviews'

type SpeakerConfirmationState = Record<
  string,
  {
    speakerConfirmed: boolean
    transcriptConfirmed: boolean
    updatedAt: string
  }
>

export function SpeakerReviewPage() {
  const params = useParams()
  const jobId = params.jobId?.trim() ?? ''
  const [resource, setResource] = useState<SpeakerReviewResource | null>(null)
  const [speakerNames, setSpeakerNames] = useState<Record<string, string>>({})
  const [segmentSpeakers, setSegmentSpeakers] = useState<Record<string, string>>({})
  const [confirmations, setConfirmations] = useState<SpeakerConfirmationState>({})
  const [submittedJob, setSubmittedJob] = useState<JobSummary | null>(null)
  const [pageSize, setPageSize] = useState(20)
  const [page, setPage] = useState(1)
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [pageError, setPageError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [splittingSegmentId, setSplittingSegmentId] = useState<string | null>(null)
  const [splitSourcePos, setSplitSourcePos] = useState(0)
  const [splitSpeakerA, setSplitSpeakerA] = useState('')
  const [splitSpeakerB, setSplitSpeakerB] = useState('')
  const [isSplitting, setIsSplitting] = useState(false)

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
        const nextResource = await getSpeakerReview(jobId)
        if (cancelled) {
          return
        }

        setResource(nextResource)
        setPageSize(nextResource.defaultPageSize)
        setPage(1)
        setSpeakerNames(
          Object.fromEntries(
            nextResource.speakerOptions.map((option) => [option.id, option.displayName]),
          ),
        )
        setSegmentSpeakers(
          Object.fromEntries(nextResource.items.map((item) => [item.segmentId, item.speakerId])),
        )
        setConfirmations(
          Object.fromEntries(
            nextResource.items.map((item) => [
              item.segmentId,
              {
                speakerConfirmed: item.speakerConfirmed,
                transcriptConfirmed: item.transcriptConfirmed,
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
    (submittedJob.status !== 'waiting_for_review' || submittedJob.currentStage !== 'speaker_review')
  const totalItems = resource?.items.length ?? 0
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize))
  const currentPage = Math.min(page, totalPages)
  const hasPagination = totalItems > 20
  const visibleItems = resource?.items.slice((currentPage - 1) * pageSize, currentPage * pageSize) ?? []
  const confirmedCount = resource
    ? resource.items.filter((item) => {
        const confirmation = confirmations[item.segmentId]
        return Boolean(confirmation?.speakerConfirmed && confirmation?.transcriptConfirmed)
      }).length
    : 0

  const handleApprove = async () => {
    if (!resource) {
      return
    }

    setIsSubmitting(true)
    setSubmitError(null)
    try {
      const now = new Date().toISOString()
      const result = await approveSpeakerReview({
        confirmations: Object.fromEntries(
          resource.items.map((item) => {
            const current = confirmations[item.segmentId] ?? {
              speakerConfirmed: item.speakerConfirmed,
              transcriptConfirmed: item.transcriptConfirmed,
              updatedAt: item.reviewUpdatedAt ?? '',
            }

            return [
              item.segmentId,
              {
                speakerConfirmed: current.speakerConfirmed,
                transcriptConfirmed: current.transcriptConfirmed,
                updatedAt: current.updatedAt || now,
              },
            ]
          }),
        ),
        jobId,
        projectDir: resource.projectDir,
        segmentSpeakers,
        speakerNames,
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
        description="请从当前任务或项目详情进入说话人审核。"
        title="缺少任务标识"
      />
    )
  }

  if (isLoading && !resource && !pageError) {
    return (
      <EmptyState
        description="正在读取当前说话人审核内容..."
        title="说话人审核加载中"
      />
    )
  }

  if (pageError && !resource) {
    return (
      <EmptyState
        actionLabel="返回当前任务"
        actionTo="/tasks/current"
        description={pageError}
        title="无法打开说话人审核"
      />
    )
  }

  if (!resource) {
    return (
      <EmptyState
        actionLabel="返回当前任务"
        actionTo="/tasks/current"
        description="当前没有可展示的说话人审核内容。"
        title="当前没有待处理的说话人审核"
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
                <h2 className="text-2xl font-semibold text-ink-950">说话人审核</h2>
                <StatusBadge status={activeJob?.status ?? resource.job.status} />
                <span className="rounded-full bg-sand-100 px-3 py-1 text-xs font-semibold text-ink-900/65">
                  待处理 {resource.items.length} 条
                </span>
              </div>
              <p className="text-sm leading-6 text-ink-900/75">
                {getReviewPageMessage('speaker_review', resource.activeMessage)}
              </p>
              <p className="text-sm font-medium text-ink-900/60">
                当前必须完成：确认说话人归属与转写，共 {resource.items.length} 条。
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
              <h3 className="text-xl font-semibold text-ink-950">说话人审核已完成</h3>
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
            submittedJob.currentStage === 'translation_review' ? (
              <Link className="primary-button" to={`/reviews/${jobId}/translation`}>
                继续处理翻译审核
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
        <div className="space-y-4">
            <div className="space-y-4">
              {visibleItems.map((item) => {
                const confirmation = confirmations[item.segmentId] ?? {
                  speakerConfirmed: item.speakerConfirmed,
                  transcriptConfirmed: item.transcriptConfirmed,
                  updatedAt: item.reviewUpdatedAt ?? '',
                }

                return (
                  <article
                    key={item.segmentId}
                    className="rounded-2xl border border-ink-950/8 bg-sand-50/70 p-4"
                  >
                    <div className="flex flex-wrap items-center gap-3">
                      <span className="text-xs font-medium text-ink-900/50">片段 {item.segmentId}</span>
                      <select
                          className="form-input"
                          onChange={(event) => {
                            const nextSpeakerId = event.currentTarget.value
                            const currentSpeakerId = segmentSpeakers[item.segmentId] ?? item.speakerId
                            if (nextSpeakerId === currentSpeakerId) return

                            const currentIdx = resource.items.findIndex((i) => i.segmentId === item.segmentId)
                            const subsequentItems = resource.items.slice(currentIdx)
                            const hasMultipleSpeakers = resource.speakerOptions.length > 1
                            const affectedCount = subsequentItems.filter((i) => {
                              const spk = segmentSpeakers[i.segmentId] ?? i.speakerId
                              return spk === currentSpeakerId || spk === nextSpeakerId
                            }).length

                            if (hasMultipleSpeakers && affectedCount > 1 && window.confirm(
                              `是否将后续所有 "${speakerNames[currentSpeakerId] ?? currentSpeakerId}" 替换为 "${speakerNames[nextSpeakerId] ?? nextSpeakerId}"，同时互换？\n\n将影响从当前片段起的 ${affectedCount} 个片段。\n\n点击"确定"批量互换，"取消"仅修改当前片段。`
                            )) {
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
                              {speakerNames[option.id] ?? option.displayName}
                            </option>
                          ))}
                        </select>
                      <div className="flex-1" />
                      <div className="flex items-center">
                        <button
                          className="text-xs text-ink-900/50 hover:text-ink-900/80 underline"
                          onClick={() => {
                            if (splittingSegmentId === item.segmentId) {
                              setSplittingSegmentId(null)
                            } else {
                              setSplittingSegmentId(item.segmentId)
                              setSplitSourcePos(Math.floor((item.sourceText || '').length / 2))
                              setSplitSpeakerA(segmentSpeakers[item.segmentId] ?? item.speakerId)
                              setSplitSpeakerB(resource.speakerOptions.length > 1
                                ? resource.speakerOptions.find(o => o.id !== (segmentSpeakers[item.segmentId] ?? item.speakerId))?.id ?? item.speakerId
                                : item.speakerId)
                            }
                          }}
                          type="button"
                        >
                          {splittingSegmentId === item.segmentId ? '取消拆分' : '拆分片段'}
                        </button>
                      </div>

                      {splittingSegmentId === item.segmentId ? (
                        <div className="rounded-2xl border-2 border-amber-400/40 bg-amber-50/50 p-4 space-y-4">
                          <p className="text-sm font-semibold text-ink-950">拆分片段 {item.segmentId}</p>

                          <div className="space-y-2">
                            <p className="form-label">原文拆分位置（字符: {splitSourcePos}）</p>
                            <input
                              className="w-full"
                              max={(item.sourceText || '').length}
                              min={1}
                              onChange={(e) => setSplitSourcePos(Number(e.currentTarget.value))}
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

                          <div className="grid grid-cols-2 gap-3">
                            <label className="space-y-1">
                              <span className="form-label">片段 A 发言人</span>
                              <select
                                className="form-input text-sm"
                                onChange={(e) => setSplitSpeakerA(e.currentTarget.value)}
                                value={splitSpeakerA}
                              >
                                {resource.speakerOptions.map((option) => (
                                  <option key={option.id} value={option.id}>{speakerNames[option.id] ?? option.displayName}</option>
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
                                  <option key={option.id} value={option.id}>{speakerNames[option.id] ?? option.displayName}</option>
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
                                  splitCnIndex: 0,
                                  speakerA: splitSpeakerA,
                                  speakerB: splitSpeakerB,
                                  stage: 'speaker_review',
                                })
                                if (result.success) {
                                  window.location.reload()
                                  return
                                }
                                setSubmitError('拆分未生效，请检查片段数据。')
                              } catch (error) {
                                setSubmitError(`拆分失败: ${error instanceof Error ? error.message : '未知错误'}`)
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
              description="侧边栏只保留本轮审核的整体进度。"
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
                  label: '说话人数',
                  value: String(resource.speakerOptions.length),
                },
                {
                  label: '已确认片段',
                  value: String(confirmedCount),
                },
              ]}
              title="审核概览"
            />

            <section className="surface-card p-5">
              <div className="space-y-1">
                <h3 className="text-lg font-semibold text-ink-950">说话人名称</h3>
                <p className="muted-copy">这里只保留显示名称调整，不让次级信息抢过片段处理本身。</p>
              </div>

              <div className="mt-5 grid gap-4 md:grid-cols-2 xl:grid-cols-1">
                {resource.speakerOptions.map((option) => (
                  <label key={option.id} className="space-y-2">
                    <span className="form-label">{option.id}</span>
                    <input
                      className="form-input"
                      onChange={(event) => {
                        const nextValue = event.currentTarget.value
                        setSpeakerNames((current) => ({
                          ...current,
                          [option.id]: nextValue,
                        }))
                      }}
                      value={speakerNames[option.id] ?? option.displayName}
                    />
                  </label>
                ))}
              </div>
            </section>
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
