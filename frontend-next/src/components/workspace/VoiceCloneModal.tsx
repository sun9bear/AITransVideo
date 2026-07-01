'use client'

// Moved verbatim out of VoiceSelectionPanel.tsx (2026-06-11). MiniMax legacy
// clone path — function body untouched, locked by G6.1.5 / G_MX.2 guards.
// Consumers: VoiceSelectionPanel and the editing-state voice Tab
// (VoiceModifyTab). Editing consumers pass a minimal speaker-shaped object
// (speakerId / speakerName) plus the cost.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslations } from 'next-intl'

import { useApiErrorMessage } from '@/lib/api/error-localization'
import {
  cloneVoiceForSelection,
  getSpeakerAudioSegments,
  matchVoiceForSelection,
  type SpeakerAudioSegment,
  type VoiceReuseMatchResponse,
} from '@/lib/api/voiceSelection'

type VoiceCloneTranslator = ReturnType<typeof useTranslations<'appVoiceClone'>>

function formatReuseConfidence(t: VoiceCloneTranslator, confidence: VoiceReuseMatchResponse['confidence']): string {
  if (confidence === 'strong') return t('reuseConfidence.strong')
  if (confidence === 'medium') return t('reuseConfidence.medium')
  if (confidence === 'weak') return t('reuseConfidence.weak')
  return t('reuseConfidence.default')
}

function formatSeconds(value: number | null): string | null {
  if (value == null || !Number.isFinite(value)) return null
  return `${value.toFixed(1)}s`
}

export interface VoiceCloneModalSpeakerRef {
  speakerId: string
  speakerName: string
}

interface VoiceCloneModalProps {
  jobId: string
  speaker: VoiceCloneModalSpeakerRef
  cloneCostCredits: number
  selectedProvider?: string
  onClose: () => void
  onComplete: (speakerId: string, voiceId: string, options?: { reused?: boolean }) => void
}

export function VoiceCloneModal({ jobId, speaker, cloneCostCredits, selectedProvider, onClose, onComplete }: VoiceCloneModalProps) {
  const t = useTranslations('appVoiceClone')
  const localizeError = useApiErrorMessage()
  const [segments, setSegments] = useState<SpeakerAudioSegment[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [isLoading, setIsLoading] = useState(true)
  const [isCheckingReuse, setIsCheckingReuse] = useState(true)
  const [reuseMatch, setReuseMatch] = useState<VoiceReuseMatchResponse | null>(null)
  const [isCloning, setIsCloning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [playingSegmentId, setPlayingSegmentId] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        setIsLoading(true)
        const result = await getSpeakerAudioSegments(jobId, speaker.speakerId)
        if (!cancelled) setSegments(result.segments)
      } catch (err) {
        if (!cancelled) setError(localizeError(err))
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [jobId, speaker.speakerId, localizeError])

  useEffect(() => {
    let cancelled = false
    async function loadReuseMatch() {
      try {
        setIsCheckingReuse(true)
        const result = await matchVoiceForSelection({
          jobId,
          speakerId: speaker.speakerId,
          speakerName: speaker.speakerName,
          selectedProvider,
        })
        if (!cancelled) setReuseMatch(result)
      } catch {
        if (!cancelled) setReuseMatch(null)
      } finally {
        if (!cancelled) setIsCheckingReuse(false)
      }
    }
    loadReuseMatch()
    return () => { cancelled = true }
  }, [jobId, speaker.speakerId, speaker.speakerName, selectedProvider])

  const selectedDuration = useMemo(() => {
    return segments.filter((s) => selectedIds.has(s.segmentId)).reduce((sum, s) => sum + s.durationS, 0)
  }, [segments, selectedIds])

  const meetsMinDuration = selectedDuration >= 10
  const exceedsMaxDuration = selectedDuration >= 300

  const toggleSegment = useCallback((segmentId: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(segmentId)) next.delete(segmentId)
      else next.add(segmentId)
      return next
    })
  }, [])

  const autoSelect = useCallback(() => {
    const sorted = [...segments].sort((a, b) => b.durationS - a.durationS)
    const selected = new Set<number>()
    let total = 0
    for (const seg of sorted) {
      if (total + seg.durationS >= 300) break
      selected.add(seg.segmentId)
      total += seg.durationS
    }
    setSelectedIds(selected)
  }, [segments])

  const playSegment = useCallback((seg: SpeakerAudioSegment) => {
    if (audioRef.current) { audioRef.current.pause(); audioRef.current = null }
    if (playingSegmentId === seg.segmentId) { setPlayingSegmentId(null); return }
    const audio = new Audio(seg.audioUrl)
    audio.onended = () => setPlayingSegmentId(null)
    audio.onerror = () => setPlayingSegmentId(null)
    audio.play().catch(() => setPlayingSegmentId(null))
    audioRef.current = audio
    setPlayingSegmentId(seg.segmentId)
  }, [playingSegmentId])

  const handleClone = useCallback(async () => {
    if (isCloning || !meetsMinDuration || exceedsMaxDuration) return
    setIsCloning(true)
    setError(null)
    try {
      const result = await cloneVoiceForSelection({ jobId, speakerId: speaker.speakerId, segmentIds: Array.from(selectedIds) })
      onComplete(speaker.speakerId, result.voiceId)
    } catch (err) {
      setError(localizeError(err))
    } finally {
      setIsCloning(false)
    }
  }, [isCloning, meetsMinDuration, exceedsMaxDuration, jobId, speaker.speakerId, selectedIds, onComplete, localizeError])

  const handleReuse = useCallback(() => {
    if (!reuseMatch?.voice?.voiceId) return
    onComplete(speaker.speakerId, reuseMatch.voice.voiceId, { reused: true })
  }, [onComplete, reuseMatch, speaker.speakerId])

  useEffect(() => {
    return () => { if (audioRef.current) { audioRef.current.pause(); audioRef.current = null } }
  }, [])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="w-full max-w-2xl max-h-[85vh] flex flex-col rounded-xl bg-card border border-border shadow-xl">
        <div className="flex items-center justify-between p-4 border-b border-border">
          <h3 className="text-base font-semibold text-foreground">{t('title', { speakerName: speaker.speakerName })}</h3>
          <button className="text-slate-400 hover:text-foreground transition" onClick={onClose} type="button">
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M6 18L18 6M6 6l12 12" strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} /></svg>
          </button>
        </div>
        {isCheckingReuse || (reuseMatch?.matched && reuseMatch.voice) ? (
          <div className="border-b border-border px-4 py-3">
            {isCheckingReuse ? (
              <p className="text-xs text-slate-500">{t('checkingPersonalLibrary')}</p>
            ) : reuseMatch?.matched && reuseMatch.voice ? (
              <div className="rounded-lg border border-[color:var(--bamboo)]/30 bg-[color:var(--bamboo)]/10 p-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 space-y-1">
                    <p className="text-sm font-medium text-foreground">{t('foundReusableVoice', { label: reuseMatch.voice.label || reuseMatch.voice.voiceId })}</p>
                    <p className="text-xs text-slate-500">
                      {formatReuseConfidence(t, reuseMatch.confidence)}
                      {reuseMatch.voice.sourceVideoTitle ? ` · ${reuseMatch.voice.sourceVideoTitle}` : ''}
                    </p>
                    <p className="text-xs text-slate-500">
                      {t('reuseNoCost')}
                      {formatSeconds(reuseMatch.voice.cloneSampleSeconds) ? t('originalSampleSuffix', { seconds: formatSeconds(reuseMatch.voice.cloneSampleSeconds) as string }) : ''}
                      {reuseMatch.voice.provider ? ` · ${reuseMatch.voice.provider}` : ''}
                    </p>
                  </div>
                  <button
                    className="h-8 rounded-lg bg-primary px-3 text-xs font-medium text-primary-foreground transition hover:bg-primary/85"
                    onClick={handleReuse}
                    type="button"
                  >
                    {t('reuseThisVoice')}
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
        <div className="flex items-center gap-3 p-4 border-b border-slate-100 dark:border-slate-800">
          <button className="h-7 rounded px-3 text-xs font-medium transition border border-[color:var(--cinnabar)]/40 bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)] hover:bg-[color:var(--cinnabar)]/20" onClick={autoSelect} type="button">{t('autoSelect')}</button>
          <span className="text-xs text-slate-400">{t('autoSelectHint')}</span>
        </div>
        <div className="flex items-center gap-4 px-4 py-2 bg-slate-50/50 dark:bg-slate-800/30">
          <span className="text-xs text-slate-500">{t('selectedCountPrefix')} <span className="font-medium text-foreground">{selectedIds.size}</span> {t('selectedCountSuffix')}</span>
          <span className="text-xs text-slate-500">{t('totalDurationLabel')} <span className={`font-medium ${exceedsMaxDuration ? 'text-[color:var(--cinnabar)]' : meetsMinDuration ? 'text-[color:var(--bamboo)]' : 'text-[color:var(--ochre)]'}`}>{selectedDuration.toFixed(1)}s</span></span>
          {!meetsMinDuration ? <span className="text-xs text-[color:var(--ochre)]">{t('needAtLeast10s')}</span> : exceedsMaxDuration ? <span className="text-xs text-[color:var(--cinnabar)]">{t('exceeds300s')}</span> : <span className="text-xs text-[color:var(--bamboo)]">{t('meetsRequirement')}</span>}
        </div>
        <div className="flex-1 overflow-y-auto p-4 space-y-1">
          {isLoading ? (
            <div className="text-center py-8 text-sm text-slate-400">{t('loadingSegments')}</div>
          ) : segments.length === 0 ? (
            <div className="text-center py-8 text-sm text-slate-400">{t('noSegmentsAvailable')}</div>
          ) : segments.map((seg) => {
            const isSelected = selectedIds.has(seg.segmentId)
            const isPlaying = playingSegmentId === seg.segmentId
            return (
              <div className={`flex items-center gap-3 rounded-lg px-3 py-2 transition cursor-pointer ${isSelected ? 'border bg-[color:var(--cinnabar)]/10 border-[color:var(--cinnabar)]/40' : 'border border-transparent hover:bg-muted/40'}`} key={seg.segmentId} onClick={() => toggleSegment(seg.segmentId)}>
                <div className={`h-4 w-4 rounded border-2 flex items-center justify-center shrink-0 ${isSelected ? 'border-teal-500 bg-teal-500' : 'border-slate-300 dark:border-slate-600'}`}>
                  {isSelected ? <svg className="h-3 w-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} /></svg> : null}
                </div>
                <button className="h-7 w-7 rounded-full border border-border flex items-center justify-center shrink-0 hover:bg-muted transition" onClick={(e) => { e.stopPropagation(); playSegment(seg) }} type="button">
                  {isPlaying ? <svg className="h-3 w-3 text-[color:var(--cinnabar)]" fill="currentColor" viewBox="0 0 24 24"><rect height="16" rx="1" width="4" x="6" y="4" /><rect height="16" rx="1" width="4" x="14" y="4" /></svg> : <svg className="h-3 w-3 text-slate-500" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>}
                </button>
                <span className="flex-1 text-xs text-foreground truncate">{seg.sourceText || t('segmentFallback', { id: seg.segmentId })}</span>
                <span className="text-xs text-slate-400 shrink-0">{seg.durationS.toFixed(1)}s</span>
              </div>
            )
          })}
        </div>
        <div className="flex items-center justify-between p-4 border-t border-border">
          <span className="text-xs text-slate-400">
            {reuseMatch?.matched
              ? cloneCostCredits > 0 ? t('recloneCostCredits', { credits: cloneCostCredits }) : t('recloneCostUnknown')
              : cloneCostCredits > 0 ? t('cloneCostCredits', { credits: cloneCostCredits }) : t('creditsInfoUnavailable')}
          </span>
          <div className="flex items-center gap-2">
            {error ? <span className="text-xs text-[color:var(--cinnabar)] max-w-[200px] truncate">{error}</span> : null}
            <button className="h-8 rounded px-4 text-sm text-slate-500 transition hover:text-foreground" disabled={isCloning} onClick={onClose} type="button">{t('cancel')}</button>
            <button className="h-8 rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground transition hover:bg-primary/85 disabled:opacity-50 disabled:cursor-not-allowed" disabled={isCloning || !meetsMinDuration || exceedsMaxDuration} onClick={() => { void handleClone() }} type="button">{isCloning ? t('cloning') : reuseMatch?.matched ? t('reclone') : t('startClone')}</button>
          </div>
        </div>
      </div>
    </div>
  )
}
