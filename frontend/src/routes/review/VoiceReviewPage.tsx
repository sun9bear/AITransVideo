import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { EmptyState } from '@/components/EmptyState'
import { StatusBadge } from '@/components/StatusBadge'
import { Toast } from '@/components/Toast'
import { getStageLabel } from '@/features/jobs/presentation'
import { ApiError } from '@/lib/api/client'
import { getJob } from '@/lib/api/jobs'
import {
  approveVoiceReview,
  cloneVoiceForReview,
  getVoiceReview,
  previewVoice,
} from '@/lib/api/reviews'
import { getVoiceLibrary, type VoiceLibraryEntry } from '@/lib/api/voiceLibrary'
import { usePollingTask } from '@/lib/react/usePollingTask'
import { ACTIVE_JOB_STATUSES, type JobSummary } from '@/types/jobs'
import type { VoiceReviewResource, VoiceReviewSpeaker } from '@/types/reviews'

interface SpeakerVoiceState {
  voiceId: string
  manualVoiceId: string
  isCloning: boolean
  isPreviewing: boolean
  cloneError: string | null
  previewError: string | null
}

export function VoiceReviewPage() {
  const params = useParams()
  const navigate = useNavigate()
  const jobId = params.jobId?.trim() ?? ''
  const [resource, setResource] = useState<VoiceReviewResource | null>(null)
  const [speakerStates, setSpeakerStates] = useState<Record<string, SpeakerVoiceState>>({})
  const [allVoices, setAllVoices] = useState<VoiceLibraryEntry[]>([])
  const [submittedJob, setSubmittedJob] = useState<JobSummary | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [pageError, setPageError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

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
        if (cancelled) return

        setResource(nextResource)
        // Load all voices from voice library for the dropdown
        try {
          const lib = await getVoiceLibrary()
          setAllVoices(lib.voices)
        } catch {
          // non-critical, dropdown will just be empty
        }
        // Initialize speaker states
        const states: Record<string, SpeakerVoiceState> = {}
        for (const speaker of nextResource.speakers) {
          states[speaker.speakerId] = {
            voiceId: speaker.resolvedVoiceId || speaker.defaultVoiceId || '',
            manualVoiceId: '',
            isCloning: false,
            isPreviewing: false,
            cloneError: null,
            previewError: null,
          }
        }
        setSpeakerStates(states)
        setPageError(null)
      } catch (error) {
        if (!cancelled) {
          // Fallback: redirect to translation-config if voice review unavailable
          try {
            const job = await getJob(jobId)
            if (job.status === 'waiting_for_review') {
              navigate(`/reviews/${jobId}/translation-config`, { replace: true })
              return
            }
          } catch {
            // ignore
          }
          setPageError(getErrorMessage(error))
        }
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }

    void load()
    return () => { cancelled = true }
  }, [jobId, navigate])

  usePollingTask(
    async () => {
      if (!jobId) return
      try {
        const nextJob = await getJob(jobId)
        setSubmittedJob(nextJob)
      } catch { return }
    },
    {
      enabled: Boolean(submittedJob && ACTIVE_JOB_STATUSES.includes(submittedJob.status)),
      immediate: false,
      intervalMs: 3000,
    },
  )

  const updateSpeakerState = useCallback(
    (speakerId: string, update: Partial<SpeakerVoiceState>) => {
      setSpeakerStates((prev) => ({
        ...prev,
        [speakerId]: { ...prev[speakerId], ...update },
      }))
    },
    [],
  )

  const handlePreview = useCallback(
    async (speakerId: string, voiceId: string) => {
      if (!voiceId) return
      updateSpeakerState(speakerId, { isPreviewing: true, previewError: null })
      try {
        const result = await previewVoice(voiceId, speakerId)
        if (result.audioBase64 && audioRef.current) {
          audioRef.current.src = `data:audio/wav;base64,${result.audioBase64}`
          audioRef.current.play()
        }
      } catch (error) {
        updateSpeakerState(speakerId, { previewError: getErrorMessage(error) })
      } finally {
        updateSpeakerState(speakerId, { isPreviewing: false })
      }
    },
    [updateSpeakerState],
  )

  const handleClone = useCallback(
    async (speaker: VoiceReviewSpeaker) => {
      updateSpeakerState(speaker.speakerId, { isCloning: true, cloneError: null })
      try {
        const result = await cloneVoiceForReview(
          speaker.speakerId,
          speaker.speakerName,
          speaker.samplePath || '',
          resource?.projectDir,
        )
        updateSpeakerState(speaker.speakerId, {
          voiceId: result.voiceId,
          isCloning: false,
        })
        // Refresh voice library dropdown
        try {
          const lib = await getVoiceLibrary()
          setAllVoices(lib.voices)
        } catch {
          // non-critical
        }
      } catch (error) {
        updateSpeakerState(speaker.speakerId, {
          isCloning: false,
          cloneError: getErrorMessage(error),
        })
      }
    },
    [updateSpeakerState],
  )

  const handleApprove = async () => {
    if (!resource) return
    setIsSubmitting(true)
    setSubmitError(null)
    try {
      const voiceIdA = speakerStates['speaker_a']?.voiceId || speakerStates['speaker_a']?.manualVoiceId || ''
      const voiceIdB = speakerStates['speaker_b']?.voiceId || speakerStates['speaker_b']?.manualVoiceId || ''

      if (!voiceIdA) {
        setSubmitError('Speaker A 的音色尚未配置。')
        setIsSubmitting(false)
        return
      }

      const result = await approveVoiceReview({
        jobId,
        projectDir: resource.projectDir,
        voiceIdA: voiceIdA,
        voiceIdB: voiceIdB || undefined,
      })
      setSubmittedJob(result.job)
    } catch (error) {
      setSubmitError(getErrorMessage(error))
    } finally {
      setIsSubmitting(false)
    }
  }

  const activeJob = submittedJob ?? resource?.job ?? null
  const hasAdvanced =
    submittedJob !== null &&
    (submittedJob.status !== 'waiting_for_review' ||
      submittedJob.currentStage !== 'voice_review')

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
    return <EmptyState description="正在读取音色确认内容..." title="音色确认加载中" />
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
      {/* Hidden audio element for preview playback */}
      <audio ref={audioRef} className="hidden" />

      {!hasAdvanced ? (
        <section className="sticky top-4 z-20 surface-card p-5 shadow-[0_24px_60px_-42px_rgba(25,37,47,0.6)]">
          <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-3">
                <h2 className="text-2xl font-semibold text-ink-950">音色确认</h2>
                <StatusBadge status={activeJob?.status ?? resource.job.status} />
              </div>
              <p className="text-sm leading-6 text-ink-900/75">
                请确认各发言人的音色配置。可以试听已有音色、克隆新音色，或手动输入 Voice ID。
              </p>
            </div>
            <div className="flex flex-wrap gap-3">
              <button
                className="primary-button"
                disabled={isSubmitting}
                onClick={() => { void handleApprove() }}
                type="button"
              >
                {isSubmitting ? '提交中...' : '确认并继续'}
              </button>
              <Link className="secondary-button" to="/tasks/current">
                返回当前任务
              </Link>
            </div>
          </div>
        </section>
      ) : null}

      <Toast message={submitError} onClose={() => setSubmitError(null)} />

      {hasAdvanced && submittedJob ? (
        <section className="surface-card p-6">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="space-y-2">
              <p className="eyebrow">音色已确认</p>
              <h3 className="text-xl font-semibold text-ink-950">音色确认已完成</h3>
              <p className="text-sm leading-6 text-ink-900/70">
                当前任务已推进到 {getStageLabel(submittedJob.currentStage)}。
              </p>
            </div>
            <StatusBadge status={submittedJob.status} />
          </div>
          <div className="mt-5 flex flex-wrap gap-3">
            <Link className="secondary-button" to="/tasks/current">
              返回当前任务
            </Link>
          </div>
        </section>
      ) : null}

      {!hasAdvanced ? (
        <div className="space-y-4">
          {resource.speakers.map((speaker) => {
            const state = speakerStates[speaker.speakerId]
            if (!state) return null
            const hasVoice = Boolean(state.voiceId)

            return (
              <article
                key={speaker.speakerId}
                className="surface-card p-5 space-y-4"
              >
                {/* Header */}
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="eyebrow">{speaker.speakerLabel || speaker.speakerId}</p>
                    <h3 className="text-lg font-semibold text-ink-950">
                      {speaker.speakerName}
                    </h3>
                  </div>
                  <span className={`rounded-full px-3 py-1 text-xs font-semibold ${
                    hasVoice
                      ? 'bg-emerald-100 text-emerald-700'
                      : 'bg-amber-100 text-amber-700'
                  }`}>
                    {hasVoice ? '✓ 已配置音色' : '⚠ 需要配置音色'}
                  </span>
                </div>

                {/* Current selected voice with preview */}
                {hasVoice ? (
                  <div className="rounded-2xl border border-emerald-200 bg-emerald-50/50 p-4">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm font-medium text-ink-900/70">当前选择</p>
                        <p className="text-sm font-semibold text-ink-950">
                          {allVoices.find(v => v.voiceId === state.voiceId)?.label
                            || allVoices.find(v => v.voiceId === state.voiceId)?.speakerName
                            || state.voiceId}
                        </p>
                        <p className="text-xs font-mono text-ink-900/40">{state.voiceId}</p>
                      </div>
                      <button
                        className="secondary-button text-sm px-4 py-2"
                        disabled={state.isPreviewing}
                        onClick={() => { void handlePreview(speaker.speakerId, state.voiceId) }}
                        type="button"
                      >
                        {state.isPreviewing ? '试听中...' : '▶ 试听当前音色'}
                      </button>
                    </div>
                    {state.previewError ? (
                      <p className="mt-2 text-xs text-coral-600">{state.previewError}</p>
                    ) : null}
                  </div>
                ) : null}

                {/* Three ways to select voice — always all available */}
                <div className="space-y-3">

                  {/* Way 1: Select from voice library (dropdown) */}
                  <div className="rounded-2xl border border-ink-950/8 bg-white/75 p-4 space-y-2">
                    <p className="form-label">方式一：从音色库选择</p>
                    {allVoices.length > 0 ? (
                      <div className="flex gap-2">
                        <select
                          className="form-input flex-1 text-sm"
                          onChange={(e) => {
                            const vid = e.currentTarget.value
                            if (vid) {
                              updateSpeakerState(speaker.speakerId, { voiceId: vid })
                            }
                          }}
                          value={state.voiceId}
                        >
                          <option value="">— 请选择音色 —</option>
                          {allVoices.map((voice) => (
                            <option key={voice.voiceId} value={voice.voiceId}>
                              {voice.speakerName ? `${voice.speakerName} - ` : ''}{voice.label || voice.voiceId}
                            </option>
                          ))}
                        </select>
                        <button
                          className="secondary-button text-sm px-4"
                          disabled={!state.voiceId || state.isPreviewing}
                          onClick={() => { void handlePreview(speaker.speakerId, state.voiceId) }}
                          type="button"
                        >
                          {state.isPreviewing ? '...' : '试听'}
                        </button>
                      </div>
                    ) : (
                      <p className="text-xs text-ink-900/50">
                        音色库中暂无已有音色
                      </p>
                    )}
                  </div>

                  {/* Way 2: Clone voice */}
                  <div className="rounded-2xl border border-ink-950/8 bg-white/75 p-4 space-y-2">
                    <p className="form-label">方式二：克隆音色（每次 ¥9.9）</p>
                    <p className="text-xs text-ink-900/50">
                      从视频中自动提取发言人音频样本并克隆
                    </p>
                    <button
                      className="primary-button text-sm"
                      disabled={state.isCloning}
                      onClick={() => { void handleClone(speaker) }}
                      type="button"
                    >
                      {state.isCloning ? '正在采样并克隆，请稍候...' : '🎤 克隆音色'}
                    </button>
                    {state.cloneError ? (
                      <p className="text-xs text-coral-600">{state.cloneError}</p>
                    ) : null}
                  </div>

                  {/* Way 3: Manual Voice ID */}
                  <div className="rounded-2xl border border-ink-950/8 bg-white/75 p-4 space-y-2">
                    <p className="form-label">方式三：手动输入 Voice ID</p>
                    <div className="flex gap-2">
                      <input
                        className="form-input flex-1 text-sm"
                        onChange={(e) => {
                          updateSpeakerState(speaker.speakerId, {
                            manualVoiceId: e.currentTarget.value,
                          })
                        }}
                        placeholder="vt_speaker_xxx_xxx"
                        value={state.manualVoiceId}
                      />
                      <button
                        className="secondary-button text-sm px-3"
                        disabled={!state.manualVoiceId.trim()}
                        onClick={() => {
                          const vid = state.manualVoiceId.trim()
                          if (vid) {
                            updateSpeakerState(speaker.speakerId, {
                              voiceId: vid,
                            })
                          }
                        }}
                        type="button"
                      >
                        应用
                      </button>
                      <button
                        className="secondary-button text-sm px-3"
                        disabled={!state.manualVoiceId.trim() || state.isPreviewing}
                        onClick={() => {
                          const vid = state.manualVoiceId.trim()
                          if (vid) { void handlePreview(speaker.speakerId, vid) }
                        }}
                        type="button"
                      >
                        试听
                      </button>
                    </div>
                  </div>
                </div>
              </article>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}

function getErrorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '请求失败，请稍后重试。'
}
