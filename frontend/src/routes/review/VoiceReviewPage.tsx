import { useEffect, useState, type Dispatch, type SetStateAction } from 'react'
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
import {
  approveVoiceReview,
  bindVoiceReviewDefault,
  getVoiceReview,
  registerVoiceReviewManual,
} from '@/lib/api/reviews'
import { usePollingTask } from '@/lib/react/usePollingTask'
import { ACTIVE_JOB_STATUSES, type JobSummary } from '@/types/jobs'
import type { VoiceReviewResource, VoiceReviewSpeaker } from '@/types/reviews'

export function VoiceReviewPage() {
  const params = useParams()
  const jobId = params.jobId?.trim() ?? ''
  const [resource, setResource] = useState<VoiceReviewResource | null>(null)
  const [selectedVoiceIds, setSelectedVoiceIds] = useState<Record<string, string>>({})
  const [manualVoiceIds, setManualVoiceIds] = useState<Record<string, string>>({})
  const [submittedJob, setSubmittedJob] = useState<JobSummary | null>(null)
  const [bindingSpeakerId, setBindingSpeakerId] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [pageError, setPageError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [bindingError, setBindingError] = useState<string | null>(null)
  const [bindingStatus, setBindingStatus] = useState<string | null>(null)

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
        const nextResource = await getVoiceReview(jobId)
        if (cancelled) {
          return
        }

        syncVoiceReviewState({
          nextResource,
          setManualVoiceIds,
          setResource,
          setSelectedVoiceIds,
        })
        setSubmittedJob(null)
        setPageError(null)
        setBindingError(null)
        setBindingStatus(null)
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
    (submittedJob.status !== 'waiting_for_review' || submittedJob.currentStage !== 'voice_review')
  const unresolvedCount = resource
    ? resource.speakers.filter((speaker) => !hasUsableBinding(speaker)).length
    : 0

  const applyUpdatedResource = (nextResource: VoiceReviewResource) => {
    syncVoiceReviewState({
      nextResource,
      setManualVoiceIds,
      setResource,
      setSelectedVoiceIds,
    })
  }

  const handleSetDefault = async (speaker: VoiceReviewSpeaker) => {
    const voiceId = (selectedVoiceIds[speaker.speakerId] ?? '').trim()
    if (!voiceId) {
      setBindingError(`请先为 ${speaker.speakerName} 选择一个可用音色。`)
      setBindingStatus(null)
      return
    }

    setBindingSpeakerId(speaker.speakerId)
    setBindingError(null)
    setBindingStatus(null)
    try {
      const nextResource = await bindVoiceReviewDefault({
        jobId,
        speakerId: speaker.speakerId,
        voiceId,
      })
      applyUpdatedResource(nextResource)
      setBindingStatus(`已更新 ${speaker.speakerName} 的默认音色。`)
    } catch (error) {
      setBindingError(getErrorMessage(error))
    } finally {
      setBindingSpeakerId(null)
    }
  }

  const handleRegisterManual = async (speaker: VoiceReviewSpeaker) => {
    const voiceId = (manualVoiceIds[speaker.speakerId] ?? '').trim()
    if (!voiceId) {
      setBindingError(`请先输入 ${speaker.speakerName} 的 Voice ID。`)
      setBindingStatus(null)
      return
    }

    setBindingSpeakerId(speaker.speakerId)
    setBindingError(null)
    setBindingStatus(null)
    try {
      const nextResource = await registerVoiceReviewManual({
        jobId,
        samplePath: speaker.samplePath,
        speakerId: speaker.speakerId,
        speakerName: speaker.speakerName,
        voiceId,
      })
      applyUpdatedResource(nextResource)
      setBindingStatus(`已绑定 ${speaker.speakerName} 的手动 Voice ID。`)
    } catch (error) {
      setBindingError(getErrorMessage(error))
    } finally {
      setBindingSpeakerId(null)
    }
  }

  const handleApprove = async () => {
    if (!resource) {
      return
    }

    setIsSubmitting(true)
    setSubmitError(null)
    try {
      const result = await approveVoiceReview({
        jobId,
        projectDir: resource.projectDir,
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
        description="请从当前任务或项目详情进入音色确认。"
        title="缺少任务标识"
      />
    )
  }

  if (isLoading && !resource && !pageError) {
    return (
      <EmptyState
        description="正在读取当前音色确认内容..."
        title="音色确认加载中"
      />
    )
  }

  if (pageError && !resource) {
    return (
      <EmptyState
        actionLabel="返回当前任务"
        actionTo="/tasks/current"
        description={pageError}
        title="无法打开音色确认"
      />
    )
  }

  if (!resource) {
    return (
      <EmptyState
        actionLabel="返回当前任务"
        actionTo="/tasks/current"
        description="当前没有可展示的音色确认内容。"
        title="当前没有待处理的音色确认"
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
                <h2 className="text-2xl font-semibold text-ink-950">音色确认</h2>
                <StatusBadge status={activeJob?.status ?? resource.job.status} />
                <span className="rounded-full bg-sand-100 px-3 py-1 text-xs font-semibold text-ink-900/65">
                  待处理 {resource.speakers.length} 位说话人
                </span>
              </div>
              <p className="text-sm leading-6 text-ink-900/75">
                {getReviewPageMessage('voice_review', resource.activeMessage)}
              </p>
              <p className="text-sm font-medium text-ink-900/60">
                当前必须完成：为所有待处理说话人形成可用音色绑定后再继续。
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
                {isSubmitting ? '提交中...' : '确认音色并继续'}
              </button>
              <Link className="secondary-button" to="/tasks/current">
                返回当前任务
              </Link>
              <Link className="secondary-button" to={`/projects/${jobId}`}>
                查看项目详情
              </Link>
            </div>
          </div>
        </section>
      ) : null}

      {submitError ? (
        <section className="notice-panel border border-coral-500/20 bg-coral-500/8">
          <p className="text-sm font-semibold text-coral-700">提交音色确认失败</p>
          <p className="mt-2 text-sm text-coral-700/85">{submitError}</p>
        </section>
      ) : null}

      {bindingError ? (
        <section className="notice-panel border border-coral-500/20 bg-coral-500/8">
          <p className="text-sm font-semibold text-coral-700">更新音色绑定失败</p>
          <p className="mt-2 text-sm text-coral-700/85">{bindingError}</p>
        </section>
      ) : null}

      {bindingStatus ? (
        <section className="notice-panel border border-ink-950/10 bg-sand-50/85">
          <p className="text-sm font-semibold text-ink-950">音色绑定已更新</p>
          <p className="mt-2 text-sm text-ink-900/70">{bindingStatus}</p>
        </section>
      ) : null}

      {hasAdvanced && submittedJob ? (
        <section className="surface-card p-6">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="space-y-2">
              <p className="eyebrow">审核已提交</p>
              <h3 className="text-xl font-semibold text-ink-950">音色确认已完成</h3>
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
              <h3 className="text-lg font-semibold text-ink-950">待确认说话人音色</h3>
              <p className="muted-copy">
                首屏优先呈现需要逐位确认的绑定操作，概览与说明放到侧边栏。
              </p>
            </div>

            <div className="mt-5 space-y-4">
              {resource.speakers.map((speaker) => {
                const availableVoiceId = selectedVoiceIds[speaker.speakerId] ?? ''
                const manualVoiceId = manualVoiceIds[speaker.speakerId] ?? ''
                const isBinding = bindingSpeakerId === speaker.speakerId

                return (
                  <article
                    key={speaker.speakerId}
                    className="rounded-3xl border border-ink-950/8 bg-sand-50/70 p-4"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p className="eyebrow">{speaker.speakerLabel ?? speaker.speakerId}</p>
                        <p className="mt-1 text-sm font-semibold text-ink-950">
                          {speaker.speakerName}
                        </p>
                      </div>
                      <span className="rounded-full bg-sand-100 px-3 py-1 text-xs font-semibold text-ink-900/65">
                        {speaker.resolvedStatus ?? '待确认'}
                      </span>
                    </div>

                    <div className="mt-4 grid gap-4 md:grid-cols-2">
                      <div className="rounded-2xl border border-ink-950/8 bg-white/75 px-4 py-3">
                        <p className="form-label">当前结果</p>
                        <p className="mt-2 break-all text-sm font-medium text-ink-950">
                          {speaker.resolvedLabel ?? speaker.resolvedVoiceId ?? '-'}
                        </p>
                        <p className="mt-1 text-sm text-ink-900/65">
                          {[
                            speaker.resolvedStatus,
                            speaker.resolvedSource,
                            speaker.resolvedVoiceType,
                          ]
                            .filter(Boolean)
                            .join(' / ') || '-'}
                        </p>
                      </div>
                      <div className="rounded-2xl border border-ink-950/8 bg-white/75 px-4 py-3">
                        <p className="form-label">默认音色</p>
                        <p className="mt-2 break-all text-sm font-medium text-ink-950">
                          {speaker.defaultVoiceId ?? '-'}
                        </p>
                        <p className="mt-1 text-sm text-ink-900/65">
                          类型：{speaker.defaultVoiceType ?? '-'}
                        </p>
                      </div>
                    </div>

                    <div className="mt-4 space-y-3">
                      <div className="space-y-2">
                        <p className="form-label">可用音色 ({speaker.availableVoices.length})</p>
                        {speaker.availableVoices.length > 0 ? (
                          <>
                            <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
                              <select
                                className="form-input"
                                onChange={(event) => {
                                  const nextVoiceId = event.currentTarget.value
                                  setSelectedVoiceIds((current) => ({
                                    ...current,
                                    [speaker.speakerId]: nextVoiceId,
                                  }))
                                }}
                                value={availableVoiceId}
                              >
                                <option value="">请选择一个可用音色</option>
                                {speaker.availableVoices.map((voice) => (
                                  <option key={voice.voiceId} value={voice.voiceId}>
                                    {voice.label ?? voice.voiceId}
                                  </option>
                                ))}
                              </select>
                              <button
                                className="secondary-button"
                                disabled={isBinding}
                                onClick={() => {
                                  void handleSetDefault(speaker)
                                }}
                                type="button"
                              >
                                {isBinding ? '绑定中...' : '设为该说话人的默认音色'}
                              </button>
                            </div>

                            <div className="grid gap-3">
                              {speaker.availableVoices.map((voice) => {
                                const isDefault = speaker.defaultVoiceId === voice.voiceId
                                const isResolved = speaker.resolvedVoiceId === voice.voiceId

                                return (
                                  <div
                                    key={`${speaker.speakerId}-${voice.voiceId}`}
                                    className="rounded-2xl border border-ink-950/8 bg-white/75 px-4 py-3"
                                  >
                                    <div className="flex flex-wrap items-center justify-between gap-2">
                                      <p className="text-sm font-semibold text-ink-950">
                                        {voice.label ?? voice.voiceId}
                                      </p>
                                      <span className="rounded-full bg-sand-100 px-3 py-1 text-xs font-semibold text-ink-900/65">
                                        {voice.voiceType ?? '未知类型'}
                                      </span>
                                    </div>
                                    <p className="mt-2 break-all text-sm text-ink-900/70">
                                      {voice.voiceId}
                                    </p>
                                    <p className="mt-2 text-sm text-ink-900/60">
                                      {[voice.provider, voice.ttsProvider, voice.platform]
                                        .filter(Boolean)
                                        .join(' / ') || '-'}
                                    </p>
                                    <p className="mt-2 text-sm text-ink-900/60">
                                      {[
                                        isDefault ? '默认' : null,
                                        isResolved ? '当前结果' : null,
                                        voice.verificationStatus,
                                      ]
                                        .filter(Boolean)
                                        .join(' / ') || '可用'}
                                    </p>
                                  </div>
                                )
                              })}
                            </div>
                          </>
                        ) : (
                          <div className="rounded-2xl border border-dashed border-ink-950/12 bg-white/70 px-4 py-4 text-sm text-ink-900/65">
                            当前没有可直接选择的已注册音色，可改用手动 Voice ID 绑定。
                          </div>
                        )}
                      </div>

                      <div className="rounded-2xl border border-ink-950/8 bg-white/75 px-4 py-4">
                        <p className="form-label">手动 Voice ID</p>
                        <div className="mt-3 flex flex-col gap-3 lg:flex-row lg:items-center">
                          <input
                            className="form-input"
                            onChange={(event) => {
                              const nextValue = event.currentTarget.value
                              setManualVoiceIds((current) => ({
                                ...current,
                                [speaker.speakerId]: nextValue,
                              }))
                            }}
                            placeholder="输入现有 Voice ID"
                            value={manualVoiceId}
                          />
                          <button
                            className="secondary-button"
                            disabled={isBinding}
                            onClick={() => {
                              void handleRegisterManual(speaker)
                            }}
                            type="button"
                          >
                            {isBinding ? '绑定中...' : '使用这个 Voice ID'}
                          </button>
                        </div>
                      </div>

                      <details className="rounded-2xl border border-ink-950/8 bg-white/75 px-4 py-4">
                        <summary className="cursor-pointer text-sm font-semibold text-ink-950">
                          查看采样与次级信息
                        </summary>
                        <div className="mt-4 grid gap-4 md:grid-cols-2">
                          <div>
                            <p className="form-label">入口参数</p>
                            <p className="mt-2 text-sm text-ink-900/80">
                              {speaker.voiceArgName ?? '-'}
                            </p>
                          </div>
                          <div>
                            <p className="form-label">示例音频路径</p>
                            <p className="mt-2 break-all text-sm text-ink-900/80">
                              {speaker.samplePath ?? '-'}
                            </p>
                          </div>
                          <div>
                            <p className="form-label">示例时长</p>
                            <p className="mt-2 text-sm text-ink-900/80">
                              {formatSeconds(speaker.sampleDurationS)}
                            </p>
                          </div>
                          <div>
                            <p className="form-label">静音占比</p>
                            <p className="mt-2 text-sm text-ink-900/80">
                              {formatPercent(speaker.silenceRatio)}
                            </p>
                          </div>
                        </div>
                      </details>
                    </div>
                  </article>
                )
              })}
            </div>
          </section>

          <div className="space-y-6">
            <ConfigSummaryCard
              description="侧边栏只保留整体状态和是否还能提交。"
              items={[
                {
                  label: '当前阶段',
                  value: getStageLabel(resource.job.currentStage),
                },
                {
                  label: '待处理说话人',
                  value: String(resource.speakers.length),
                },
                {
                  label: '未完成绑定',
                  value: String(unresolvedCount),
                  hint:
                    unresolvedCount > 0
                      ? '仍有说话人没有形成可用绑定时，提交会被后端拒绝。'
                      : '当前所有说话人都已有可用绑定，可直接提交。',
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

function syncVoiceReviewState({
  nextResource,
  setManualVoiceIds,
  setResource,
  setSelectedVoiceIds,
}: {
  nextResource: VoiceReviewResource
  setManualVoiceIds: Dispatch<SetStateAction<Record<string, string>>>
  setResource: Dispatch<SetStateAction<VoiceReviewResource | null>>
  setSelectedVoiceIds: Dispatch<SetStateAction<Record<string, string>>>
}) {
  setResource(nextResource)
  setSelectedVoiceIds((current) =>
    Object.fromEntries(
      nextResource.speakers.map((speaker) => {
        const candidateIds = new Set(speaker.availableVoices.map((voice) => voice.voiceId))
        const currentValue = current[speaker.speakerId] ?? ''
        const preferredValue =
          speaker.defaultVoiceId ??
          speaker.resolvedVoiceId ??
          speaker.availableVoices[0]?.voiceId ??
          ''

        return [
          speaker.speakerId,
          currentValue && candidateIds.has(currentValue) ? currentValue : preferredValue,
        ]
      }),
    ),
  )
  setManualVoiceIds((current) =>
    Object.fromEntries(
      nextResource.speakers.map((speaker) => [speaker.speakerId, current[speaker.speakerId] ?? '']),
    ),
  )
}

function hasUsableBinding(speaker: VoiceReviewSpeaker) {
  return Boolean(normalizeText(speaker.resolvedVoiceId) ?? normalizeText(speaker.defaultVoiceId))
}

function formatSeconds(value: number) {
  return `${value.toFixed(2)}s`
}

function formatPercent(value: number) {
  return `${(value * 100).toFixed(1)}%`
}

function normalizeText(value: string | null | undefined) {
  const normalized = value?.trim()
  return normalized || null
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
