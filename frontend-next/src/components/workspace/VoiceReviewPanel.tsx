"use client"

import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/lib/api/client'
import {
  approveVoiceReview,
  cloneVoiceForReview,
  getVoiceReview,
  previewVoice,
} from '@/lib/api/reviews'
import { getVoiceLibrary, type VoiceLibraryEntry } from '@/lib/api/voiceLibrary'
import type { VoiceReviewResource, VoiceReviewSpeaker } from '@/types/reviews'

interface SpeakerVoiceState {
  voiceId: string
  manualVoiceId: string
  isCloning: boolean
  isPreviewing: boolean
  cloneError: string | null
  previewError: string | null
}

interface VoiceReviewPanelProps {
  jobId: string
  onAdvanced: () => void
}

export function VoiceReviewPanel({ jobId, onAdvanced }: VoiceReviewPanelProps) {
  const [resource, setResource] = useState<VoiceReviewResource | null>(null)
  const [speakerStates, setSpeakerStates] = useState<Record<string, SpeakerVoiceState>>({})
  const [allVoices, setAllVoices] = useState<VoiceLibraryEntry[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [pageError, setPageError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setIsLoading(true)
      try {
        const nextResource = await getVoiceReview(jobId)
        if (cancelled) return
        setResource(nextResource)
        try { const lib = await getVoiceLibrary(); setAllVoices(lib.voices) } catch { /* non-critical */ }
        const states: Record<string, SpeakerVoiceState> = {}
        for (const speaker of nextResource.speakers) {
          states[speaker.speakerId] = {
            voiceId: speaker.resolvedVoiceId || speaker.defaultVoiceId || '',
            manualVoiceId: '', isCloning: false, isPreviewing: false, cloneError: null, previewError: null,
          }
        }
        setSpeakerStates(states)
        setPageError(null)
      } catch (error) {
        if (!cancelled) setPageError(getErrorMessage(error))
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [jobId])

  const updateSpeakerState = useCallback((speakerId: string, update: Partial<SpeakerVoiceState>) => {
    setSpeakerStates((prev) => ({ ...prev, [speakerId]: { ...prev[speakerId], ...update } }))
  }, [])

  const handlePreview = useCallback(async (speakerId: string, voiceId: string) => {
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
  }, [updateSpeakerState])

  const handleClone = useCallback(async (speaker: VoiceReviewSpeaker) => {
    updateSpeakerState(speaker.speakerId, { isCloning: true, cloneError: null })
    try {
      const result = await cloneVoiceForReview(speaker.speakerId, speaker.speakerName, speaker.samplePath || '', resource?.projectDir)
      updateSpeakerState(speaker.speakerId, { voiceId: result.voiceId, isCloning: false })
      try { const lib = await getVoiceLibrary(); setAllVoices(lib.voices) } catch { /* non-critical */ }
    } catch (error) {
      updateSpeakerState(speaker.speakerId, { isCloning: false, cloneError: getErrorMessage(error) })
    }
  }, [updateSpeakerState, resource?.projectDir])

  const handleApprove = async () => {
    if (!resource) return
    setIsSubmitting(true)
    setSubmitError(null)
    try {
      const voiceIdA = speakerStates['speaker_a']?.voiceId || speakerStates['speaker_a']?.manualVoiceId || ''
      const voiceIdB = speakerStates['speaker_b']?.voiceId || speakerStates['speaker_b']?.manualVoiceId || ''
      if (!voiceIdA) { setSubmitError('发言人 A 的音色尚未配置。'); setIsSubmitting(false); return }
      await approveVoiceReview({ jobId, projectDir: resource.projectDir, voiceIdA, voiceIdB: voiceIdB || undefined })
      onAdvanced()
    } catch (error) {
      setSubmitError(getErrorMessage(error))
    } finally {
      setIsSubmitting(false)
    }
  }

  if (isLoading && !resource) {
    return <PanelLoading message="正在读取音色确认内容..." />
  }
  if (pageError && !resource) {
    return <PanelError message={pageError} />
  }
  if (!resource) {
    return <PanelError message="当前没有可展示的音色确认内容。" />
  }

  return (
    <div className="space-y-5">
      <audio ref={audioRef} className="hidden" />

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          确认各发言人的音色配置。可试听已有音色、克隆新音色或手动输入 Voice ID。
        </p>
        <button
          className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:opacity-50"
          disabled={isSubmitting}
          onClick={() => { void handleApprove() }}
          type="button"
        >
          {isSubmitting ? '提交中...' : '✓ 确认并继续'}
        </button>
      </div>

      {submitError ? <ErrorBanner message={submitError} /> : null}

      <div className="space-y-4">
        {resource.speakers.map((speaker) => {
          const state = speakerStates[speaker.speakerId]
          if (!state) return null
          const hasVoice = Boolean(state.voiceId)

          return (
            <article key={speaker.speakerId} className="rounded-2xl border border-white/10 bg-white/[0.03] p-5 space-y-4">
              {/* Header */}
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wider text-white/40">{speaker.speakerLabel || speaker.speakerId}</p>
                  <h3 className="text-lg font-semibold text-white/90">{speaker.speakerName}</h3>
                </div>
                <span className={`rounded-full px-3 py-1 text-xs font-semibold ${hasVoice ? 'bg-emerald-500/15 text-emerald-400' : 'bg-amber-500/15 text-amber-400'}`}>
                  {hasVoice ? '已配置' : '待配置'}
                </span>
              </div>

              {/* Current voice */}
              {hasVoice ? (
                <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <p className="text-xs text-white/40">当前选择</p>
                      <p className="text-sm font-semibold text-white/90">
                        {allVoices.find(v => v.voiceId === state.voiceId)?.label || allVoices.find(v => v.voiceId === state.voiceId)?.speakerName || state.voiceId}
                      </p>
                      <p className="text-xs font-mono text-white/30">{state.voiceId}</p>
                    </div>
                    <button
                      className="rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-4 py-2 text-xs font-medium text-cyan-400 transition hover:bg-cyan-500/20 disabled:opacity-50"
                      disabled={state.isPreviewing}
                      onClick={() => { void handlePreview(speaker.speakerId, state.voiceId) }}
                      type="button"
                    >
                      {state.isPreviewing ? '试听中...' : '试听'}
                    </button>
                  </div>
                  {state.previewError ? <p className="mt-2 text-xs text-red-400">{state.previewError}</p> : null}
                </div>
              ) : null}

              {/* Three ways */}
              <div className="space-y-3">
                {/* Way 1: Library */}
                <div className="rounded-xl border border-white/8 bg-white/5 p-4 space-y-2">
                  <p className="text-xs font-medium text-white/50">方式一：从音色库选择</p>
                  {allVoices.length > 0 ? (
                    <div className="flex gap-2">
                      <div className="group flex-1 rounded-lg border border-white/8 bg-white/5 transition hover:border-primary/30 focus-within:border-primary/40">
                        <select className="w-full rounded-lg bg-transparent px-3 py-2 text-sm text-white/90 focus:outline-none" onChange={(e) => { if (e.currentTarget.value) updateSpeakerState(speaker.speakerId, { voiceId: e.currentTarget.value }) }} value={state.voiceId}>
                          <option value="">— 请选择 —</option>
                          {allVoices.map((v) => <option key={v.voiceId} value={v.voiceId}>{v.speakerName ? `${v.speakerName} - ` : ''}{v.label || v.voiceId}</option>)}
                        </select>
                      </div>
                      <button className="rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-3 py-2 text-xs font-medium text-cyan-400 transition hover:bg-cyan-500/20 disabled:opacity-50" disabled={!state.voiceId || state.isPreviewing} onClick={() => { void handlePreview(speaker.speakerId, state.voiceId) }} type="button">
                        {state.isPreviewing ? '...' : '试听'}
                      </button>
                    </div>
                  ) : (
                    <p className="text-xs text-white/30">音色库中暂无已有音色</p>
                  )}
                </div>

                {/* Way 2: Clone */}
                <div className="rounded-xl border border-white/8 bg-white/5 p-4 space-y-2">
                  <p className="text-xs font-medium text-white/50">方式二：克隆音色（每次 ¥9.9）</p>
                  <p className="text-xs text-white/30">从视频中自动提取发言人音频样本并克隆</p>
                  <button
                    className="inline-flex rounded-lg bg-primary/80 px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary disabled:opacity-50"
                    disabled={state.isCloning}
                    onClick={() => { void handleClone(speaker) }}
                    type="button"
                  >
                    {state.isCloning ? '正在采样并克隆...' : '克隆音色'}
                  </button>
                  {state.cloneError ? <p className="text-xs text-red-400">{state.cloneError}</p> : null}
                </div>

                {/* Way 3: Manual */}
                <div className="rounded-xl border border-white/8 bg-white/5 p-4 space-y-2">
                  <p className="text-xs font-medium text-white/50">方式三：手动输入 Voice ID</p>
                  <div className="flex gap-2">
                    <div className="group flex-1 rounded-lg border border-white/8 bg-white/5 transition hover:border-primary/30 focus-within:border-primary/40">
                      <input className="w-full rounded-lg bg-transparent px-3 py-2 text-sm text-white/90 placeholder:text-white/30 focus:outline-none" onChange={(e) => updateSpeakerState(speaker.speakerId, { manualVoiceId: e.currentTarget.value })} placeholder="vt_speaker_xxx" value={state.manualVoiceId} />
                    </div>
                    <button className="rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-xs text-white/60 transition hover:bg-white/10 disabled:opacity-30" disabled={!state.manualVoiceId.trim()} onClick={() => { const vid = state.manualVoiceId.trim(); if (vid) updateSpeakerState(speaker.speakerId, { voiceId: vid }) }} type="button">应用</button>
                    <button className="rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-3 py-2 text-xs font-medium text-cyan-400 transition hover:bg-cyan-500/20 disabled:opacity-50" disabled={!state.manualVoiceId.trim() || state.isPreviewing} onClick={() => { const vid = state.manualVoiceId.trim(); if (vid) void handlePreview(speaker.speakerId, vid) }} type="button">试听</button>
                  </div>
                </div>
              </div>
            </article>
          )
        })}
      </div>
    </div>
  )
}

function PanelLoading({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="mb-4 h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      <p className="text-sm text-muted-foreground">{message}</p>
    </div>
  )
}

function PanelError({ message }: { message: string }) {
  return (
    <div className="rounded-2xl border border-red-500/20 bg-red-500/8 p-6 text-center">
      <p className="text-sm text-red-400">{message}</p>
    </div>
  )
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-2xl border border-red-500/20 bg-red-500/8 p-4 text-sm text-red-400">
      {message}
    </div>
  )
}

function getErrorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '请求失败，请稍后重试。'
}
