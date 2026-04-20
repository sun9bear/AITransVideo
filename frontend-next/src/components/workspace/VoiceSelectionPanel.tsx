"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { getErrorMessage } from '@/lib/api/errors'
import { getJob } from '@/lib/api/jobs'
import { getVoiceLibrary, type VoiceLibraryEntry } from '@/lib/api/voiceLibrary'
import {
  approveVoiceSelection,
  cloneVoiceForSelection,
  deleteUserVoice,
  getSpeakerAudioSegments,
  getUserVoices,
  previewVoice,
  type SpeakerAudioSegment,
  type UserVoiceEntry,
  type VoiceSelectionSpeakerApproval,
  getVoiceSelectionPricing,
  type VoiceSelectionPricingResponse,
} from '@/lib/api/voiceSelection'
import { apiClient } from '@/lib/api/client'
import type { ApiWebUiStateResponse } from '@/types/api'
import type { JobSummary } from '@/types/jobs'

/* ---------- Types ---------- */

interface ProbeText {
  segmentId: number
  sourceText: string
  cnText: string
}

interface SpeakerPayload {
  speakerId: string
  speakerName: string
  segmentCount: number
  totalDurationS: number
  canClone: boolean
  autoMatchedVoice: { voiceId: string; label: string } | null
  autoMatchedByProvider: Record<
    string,
    {
      voiceId: string
      label: string
      matchConfidence: string
      backups: { voiceId: string; label: string }[]
    } | null
  >
  probeTexts: ProbeText[]
  // Target chars/sec for this speaker (source_english_words_per_second × 1.8).
  // Used to warn when the selected voice's cps deviates >30%.
  targetCharsPerSecond: number | null
}

interface AvailableVoice {
  voiceId: string
  label: string
  gender: string
  provider: string
  // Voice speed calibration (Phase 1 of translation-duration-alignment).
  // Null when voice is not yet calibrated (cloned voices, new voices, etc.);
  // the runtime will fall back to probe calibration in that case.
  charsPerSecond?: number | null
  speedCalibratedAt?: string | null
}

interface ProviderInfo {
  label: string
  availableVoices: AvailableVoice[]
  supportsClone: boolean
}

interface SpeakerVoiceState {
  voiceId: string
  voiceSource: 'catalog' | 'cloned' | 'auto_matched'
  selectedProvider: string
  minimaxModel: 'turbo' | 'hd'
  isCloning: boolean
  cloneError: string | null
}

interface VoiceSelectionPanelProps {
  jobId: string
  onAdvanced: () => void
}

const PROVIDER_TAB_ORDER = ['minimax', 'cosyvoice', 'volcengine'] as const
const PROVIDER_SHORT_LABELS: Record<string, string> = {
  minimax: 'MiniMax',
  cosyvoice: 'CosyVoice',
  volcengine: '豆包',
}

/** Build a dropdown label including speed calibration info when available. */
function formatVoiceOptionLabel(v: AvailableVoice): string {
  const base = v.label || v.voiceId
  const cps = v.charsPerSecond
  if (cps == null) {
    return base
  }
  let tier = '中'
  if (cps < 3.5) tier = '慢'
  else if (cps >= 4.5) tier = '快'
  return `${base} · ${cps.toFixed(1)}字/秒(${tier})`
}

/* ---------- Main Component ---------- */

export function VoiceSelectionPanel({ jobId, onAdvanced }: VoiceSelectionPanelProps) {
  const [speakers, setSpeakers] = useState<SpeakerPayload[]>([])
  const [voiceLibrary, setVoiceLibrary] = useState<VoiceLibraryEntry[]>([])
  const [personalVoices, setPersonalVoices] = useState<UserVoiceEntry[]>([])
  const [providerMap, setProviderMap] = useState<Record<string, ProviderInfo>>({})
  // Backward compat: flat availableVoices for old payloads without all_providers
  const [fallbackVoices, setFallbackVoices] = useState<AvailableVoice[]>([])
  const [voiceStates, setVoiceStates] = useState<Record<string, SpeakerVoiceState>>({})
  const [defaultProvider, setDefaultProvider] = useState('')
  const [hasMultiProvider, setHasMultiProvider] = useState(false)
  const [cloneCostCredits, setCloneCostCredits] = useState(0)
  const [pricing, setPricing] = useState<VoiceSelectionPricingResponse | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cloneModalSpeaker, setCloneModalSpeaker] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState<Record<string, boolean>>({})
  const [previewError, setPreviewError] = useState<Record<string, string | null>>({})
  const [expiredVoiceIds, setExpiredVoiceIds] = useState<string[]>([])
  const previewAudioRef = useRef<HTMLAudioElement | null>(null)

  // Load review payload
  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        setIsLoading(true)
        setError(null)

        const [reviewState, voices, userVoices, pricingResult] = await Promise.all([
          apiClient.get<{
            job_id: string
            status: string
            review_gate: Record<string, unknown> | null
            results: ApiWebUiStateResponse['results']
          }>(`/jobs/${jobId}/review-state`),
          getVoiceLibrary().catch(() => ({ voices: [] as VoiceLibraryEntry[] })),
          getUserVoices().catch(() => [] as UserVoiceEntry[]),
          getVoiceSelectionPricing().catch(() => null as VoiceSelectionPricingResponse | null),
        ])

        if (cancelled) return

        // Extract voice_selection_review stage payload
        const stages = reviewState.results?.review_flow?.stages ?? {}
        const vsStage = stages.voice_selection_review ?? null
        const payload = vsStage?.payload ?? {}

        // Parse speakers with per-provider auto-match
        const rawSpeakers = Array.isArray(payload.speakers) ? payload.speakers : []
        const loadedSpeakers: SpeakerPayload[] = rawSpeakers.map((s: Record<string, unknown>) => {
          // Parse auto_matched_by_provider (incl. backup_voices since Task 2)
          const rawByProv = (s.auto_matched_by_provider ?? {}) as Record<string, Record<string, unknown> | null>
          const amByProv: Record<
            string,
            { voiceId: string; label: string; matchConfidence: string; backups: { voiceId: string; label: string }[] } | null
          > = {}
          for (const [prov, match] of Object.entries(rawByProv)) {
            if (match && typeof match === 'object') {
              const rawBackups = Array.isArray(match.backup_voices) ? match.backup_voices : []
              const backups = rawBackups
                .map((b) => {
                  if (b && typeof b === 'object') {
                    const obj = b as Record<string, unknown>
                    return { voiceId: String(obj.voice_id ?? ''), label: String(obj.label ?? '') }
                  }
                  return { voiceId: '', label: '' }
                })
                .filter((b: { voiceId: string }) => b.voiceId)
              amByProv[prov] = {
                voiceId: String(match.voice_id ?? ''),
                label: String(match.label ?? ''),
                matchConfidence: String(match.match_confidence ?? ''),
                backups,
              }
            } else {
              amByProv[prov] = null
            }
          }

          return {
            speakerId: String(s.speaker_id ?? ''),
            speakerName: String(s.speaker_name ?? s.speaker_id ?? ''),
            segmentCount: Number(s.segment_count ?? 0),
            totalDurationS: Number(s.total_duration_s ?? 0),
            canClone: Boolean(s.can_clone),
            targetCharsPerSecond: s.target_chars_per_second != null ? Number(s.target_chars_per_second) : null,
            autoMatchedVoice: s.auto_matched_voice
              ? { voiceId: String((s.auto_matched_voice as Record<string, unknown>).voice_id ?? ''), label: String((s.auto_matched_voice as Record<string, unknown>).label ?? '') }
              : null,
            autoMatchedByProvider: amByProv,
            probeTexts: Array.isArray(s.probe_texts)
              ? (s.probe_texts as Record<string, unknown>[]).map((pt) => ({
                  segmentId: Number(pt.segment_id ?? 0),
                  sourceText: String(pt.source_text ?? ''),
                  cnText: String(pt.cn_text ?? ''),
                }))
              : [],
          }
        }).filter((s: SpeakerPayload) => s.speakerId)

        const loadedDefaultProvider = String(payload.tts_provider ?? '')
        setDefaultProvider(loadedDefaultProvider)
        // Pricing: Gateway truth only, no fallback to payload/hardcoded values
        if (pricingResult) {
          setPricing(pricingResult)
          setCloneCostCredits(pricingResult.voice_clone_cost_credits)
        }
        // else: cloneCostCredits stays at initial 0, clone modal shows "扣点信息暂不可用"

        // Parse all_providers (new three-engine payload)
        const rawAllProviders = payload.all_providers as Record<string, Record<string, unknown>> | undefined
        const multiProvider = !!rawAllProviders && Object.keys(rawAllProviders).length > 0
        setHasMultiProvider(multiProvider)

        if (multiProvider && rawAllProviders) {
          const pm: Record<string, ProviderInfo> = {}
          for (const [prov, info] of Object.entries(rawAllProviders)) {
            const rawVoices = Array.isArray(info.available_voices) ? info.available_voices : []
            pm[prov] = {
              label: String(info.label ?? prov),
              supportsClone: Boolean(info.supports_clone),
              availableVoices: rawVoices.map((v: Record<string, unknown>) => ({
                voiceId: String(v.voice_id ?? ''),
                label: String(v.label ?? v.voice_id ?? ''),
                gender: String(v.gender ?? ''),
                provider: String(v.provider ?? prov),
                charsPerSecond: v.chars_per_second != null ? Number(v.chars_per_second) : null,
                speedCalibratedAt: v.speed_calibrated_at != null ? String(v.speed_calibrated_at) : null,
              })).filter((v: AvailableVoice) => v.voiceId),
            }
          }
          setProviderMap(pm)
        }

        // Fallback: old single-provider available_voices
        const rawAvailableVoices = Array.isArray(payload.available_voices) ? payload.available_voices : []
        setFallbackVoices(rawAvailableVoices.map((v: Record<string, unknown>) => ({
          voiceId: String(v.voice_id ?? ''),
          label: String(v.label ?? v.voice_id ?? ''),
          gender: String(v.gender ?? ''),
          provider: String(v.provider ?? ''),
          charsPerSecond: v.chars_per_second != null ? Number(v.chars_per_second) : null,
          speedCalibratedAt: v.speed_calibrated_at != null ? String(v.speed_calibrated_at) : null,
        })).filter((v: AvailableVoice) => v.voiceId))

        // Check for expired voice IDs from pipeline validation
        const payloadExpired = Array.isArray(payload.expired_voice_ids) ? payload.expired_voice_ids.map(String) : []
        if (payloadExpired.length > 0) setExpiredVoiceIds(payloadExpired)

        // Init voice states — per-speaker provider + auto-matched voice
        const initialStates: Record<string, SpeakerVoiceState> = {}
        for (const sp of loadedSpeakers) {
          const spProvider = loadedDefaultProvider
          // Try per-provider auto-match first, then legacy auto_matched_voice
          const provMatch = sp.autoMatchedByProvider[spProvider]
          const autoVoice = provMatch?.voiceId || sp.autoMatchedVoice?.voiceId || ''
          const isExpired = autoVoice && payloadExpired.includes(autoVoice)

          initialStates[sp.speakerId] = {
            voiceId: isExpired ? '' : autoVoice,
            voiceSource: autoVoice && !isExpired ? 'auto_matched' : 'catalog',
            selectedProvider: spProvider,
            minimaxModel: 'turbo',
            isCloning: false,
            cloneError: null,
          }
        }

        setSpeakers(loadedSpeakers)
        setVoiceLibrary('voices' in voices ? (voices as { voices: VoiceLibraryEntry[] }).voices : [])
        setPersonalVoices(userVoices)
        setVoiceStates(initialStates)
      } catch (err) {
        if (!cancelled) setError(getErrorMessage(err))
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }

    load()
    return () => { cancelled = true }
  }, [jobId])

  const handleProviderChange = useCallback((speakerId: string, provider: string) => {
    setVoiceStates((prev) => {
      const current = prev[speakerId]
      if (!current) return prev
      // Find auto-matched voice for the new provider
      const sp = speakers.find((s) => s.speakerId === speakerId)
      const provMatch = sp?.autoMatchedByProvider[provider]
      return {
        ...prev,
        [speakerId]: {
          ...current,
          selectedProvider: provider,
          voiceId: provMatch?.voiceId ?? '',
          voiceSource: provMatch?.voiceId ? 'auto_matched' : 'catalog',
          cloneError: null,
        },
      }
    })
  }, [speakers])

  const handleVoiceChange = useCallback((speakerId: string, voiceId: string) => {
    setVoiceStates((prev) => ({
      ...prev,
      [speakerId]: { ...prev[speakerId], voiceId, voiceSource: 'catalog', cloneError: null },
    }))
  }, [])

  const handleCloneComplete = useCallback((speakerId: string, voiceId: string) => {
    setVoiceStates((prev) => ({
      ...prev,
      [speakerId]: { ...prev[speakerId], voiceId, voiceSource: 'cloned', isCloning: false, cloneError: null },
    }))
    setCloneModalSpeaker(null)
    getUserVoices().then(setPersonalVoices).catch(() => {})
  }, [])

  const handlePreview = useCallback(async (speakerId: string) => {
    const state = voiceStates[speakerId]
    if (!state?.voiceId) return

    if (previewAudioRef.current) {
      previewAudioRef.current.pause()
      previewAudioRef.current = null
    }

    setPreviewLoading((p) => ({ ...p, [speakerId]: true }))
    setPreviewError((p) => ({ ...p, [speakerId]: null }))

    try {
      // Use probe translation text for preview if available
      const speaker = speakers.find((sp) => sp.speakerId === speakerId)
      const probeText = speaker?.probeTexts?.[0]?.cnText || undefined

      const result = await previewVoice(jobId, state.voiceId, {
        ttsProvider: state.selectedProvider,
        sampleText: probeText,
      })

      if (result.expired) {
        setPreviewError((p) => ({ ...p, [speakerId]: '音色已失效，请重新选择' }))
        setVoiceStates((prev) => ({
          ...prev,
          [speakerId]: { ...prev[speakerId], voiceId: '', voiceSource: 'catalog' },
        }))
        setExpiredVoiceIds((prev) => [...prev, state.voiceId])
        await deleteUserVoice(state.voiceId).catch(() => {})
        setPersonalVoices((prev) => prev.filter((v) => v.voiceId !== state.voiceId))
        return
      }

      if (result.error) {
        setPreviewError((p) => ({ ...p, [speakerId]: result.error }))
        return
      }

      if (result.audioBase64) {
        const audio = new Audio(`data:audio/wav;base64,${result.audioBase64}`)
        audio.onended = () => { previewAudioRef.current = null }
        audio.play().catch(() => {})
        previewAudioRef.current = audio
      }
    } catch (err) {
      setPreviewError((p) => ({ ...p, [speakerId]: getErrorMessage(err) }))
    } finally {
      setPreviewLoading((p) => ({ ...p, [speakerId]: false }))
    }
  }, [voiceStates, jobId, speakers])

  useEffect(() => {
    return () => {
      if (previewAudioRef.current) {
        previewAudioRef.current.pause()
        previewAudioRef.current = null
      }
    }
  }, [])

  const allSelected = useMemo(() => {
    return speakers.length > 0 && speakers.every((sp) => voiceStates[sp.speakerId]?.voiceId)
  }, [speakers, voiceStates])

  const anyCloning = useMemo(() => {
    return Object.values(voiceStates).some((v) => v.isCloning)
  }, [voiceStates])

  const handleSubmit = useCallback(async () => {
    if (!allSelected || isSubmitting) return

    // Phase 4: warn when selected voice cps deviates >30% from target
    const mismatchWarnings: string[] = []
    for (const sp of speakers) {
      const targetCps = sp.targetCharsPerSecond
      if (targetCps == null || targetCps <= 0) continue
      const state = voiceStates[sp.speakerId]
      if (!state?.voiceId) continue
      // Find the selected voice's cps from the current provider's voice list
      const voices = getVoicesForSpeaker(sp.speakerId)
      const selectedVoice = voices.find((v) => v.voiceId === state.voiceId)
      const voiceCps = selectedVoice?.charsPerSecond
      if (voiceCps == null || voiceCps <= 0) continue
      const deviation = Math.abs(voiceCps - targetCps) / targetCps
      if (deviation > 0.30) {
        const pct = Math.round(deviation * 100)
        const fast = voiceCps > targetCps ? '快' : '慢'
        mismatchWarnings.push(
          `${sp.speakerName}：选定音色 ${voiceCps.toFixed(1)} 字/秒，` +
          `比原说话人需要的 ${targetCps.toFixed(1)} 字/秒${fast} ${pct}%，` +
          `配音可能需要大幅${voiceCps > targetCps ? '减速' : '加速'}。`
        )
      }
    }
    if (mismatchWarnings.length > 0) {
      const msg = '以下说话人的音色语速和原视频差异较大：\n\n' +
        mismatchWarnings.join('\n') +
        '\n\n这可能导致配音听感不自然。是否继续？'
      if (!window.confirm(msg)) return
    }

    setIsSubmitting(true)
    setError(null)
    try {
      const approvals: VoiceSelectionSpeakerApproval[] = speakers.map((sp) => ({
        speakerId: sp.speakerId,
        voiceId: voiceStates[sp.speakerId]?.voiceId ?? '',
        voiceSource: voiceStates[sp.speakerId]?.voiceSource ?? 'catalog',
        ttsProvider: voiceStates[sp.speakerId]?.selectedProvider ?? '',
      }))
      await approveVoiceSelection(jobId, approvals)
      onAdvanced()
    } catch (err) {
      setError(getErrorMessage(err))
    } finally {
      setIsSubmitting(false)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allSelected, isSubmitting, speakers, voiceStates, jobId, onAdvanced, fallbackVoices, providerMap, hasMultiProvider])

  // Helper: get available voices for a speaker's currently selected provider
  function getVoicesForSpeaker(speakerId: string): AvailableVoice[] {
    const state = voiceStates[speakerId]
    if (!state) return fallbackVoices
    if (hasMultiProvider && providerMap[state.selectedProvider]) {
      return providerMap[state.selectedProvider].availableVoices
    }
    return fallbackVoices
  }

  function canSpeakerClone(speakerId: string): boolean {
    const sp = speakers.find((s) => s.speakerId === speakerId)
    if (!sp?.canClone) return false
    const state = voiceStates[speakerId]
    if (!state) return false
    if (hasMultiProvider) {
      return providerMap[state.selectedProvider]?.supportsClone ?? false
    }
    return defaultProvider === 'minimax'
  }

  if (isLoading) {
    return (
      <section className="surface-card p-8 text-center">
        <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-3 border-teal-500 border-t-transparent" />
        <h3 className="text-lg font-semibold text-foreground">加载音色选择...</h3>
      </section>
    )
  }

  if (error && speakers.length === 0) {
    return (
      <section className="surface-card p-6">
        <p className="text-red-500">{error}</p>
      </section>
    )
  }

  const selectedSpeaker = speakers.find((s) => s.speakerId === cloneModalSpeaker) ?? null

  return (
    <>
      <section className="surface-card p-6 space-y-6">
        {/* Expired voices banner */}
        {expiredVoiceIds.length > 0 ? (
          <div className="rounded-lg border border-red-200 dark:border-red-500/20 bg-red-50 dark:bg-red-500/5 p-3">
            <p className="text-sm text-red-600 dark:text-red-400">
              检测到 {expiredVoiceIds.length} 个音色已失效，已从选项中移除。请重新选择音色。
            </p>
          </div>
        ) : null}

        {/* Header */}
        <div className="space-y-1">
          <h2 className="text-lg font-semibold text-foreground">音色选择</h2>
          <p className="text-sm text-slate-500">请为每位说话人选择预设音色或克隆专属音色，确认后继续生成配音。</p>
        </div>

        {/* Speaker list */}
        <div className="space-y-3">
          {speakers.map((sp, index) => {
            const state = voiceStates[sp.speakerId]
            const currentProvider = state?.selectedProvider ?? defaultProvider
            const voicesForProvider = getVoicesForSpeaker(sp.speakerId)
            const showClone = canSpeakerClone(sp.speakerId)
            const statusLabel = state?.voiceSource === 'cloned'
              ? '已克隆'
              : state?.voiceId
                ? '已选择'
                : '待选择'
            const statusColor = state?.voiceSource === 'cloned'
              ? 'text-teal-600 dark:text-teal-400'
              : state?.voiceId
                ? 'text-emerald-600 dark:text-emerald-400'
                : 'text-amber-600 dark:text-amber-400'

            return (
              <div key={sp.speakerId} className="rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50/50 dark:bg-slate-800/30 p-4">
                <div className="flex items-start gap-3">
                  {/* Avatar */}
                  <div className="flex h-10 w-10 items-center justify-center rounded-full bg-slate-200 dark:bg-slate-700 text-sm font-bold text-slate-600 dark:text-slate-300 shrink-0">
                    {String.fromCharCode(65 + index)}
                  </div>

                  <div className="flex-1 min-w-0 space-y-2">
                    {/* Name + status */}
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium text-foreground text-sm">{sp.speakerName}</span>
                      <span className="text-xs text-slate-400">{sp.speakerId}</span>
                      <span className="text-xs text-slate-400">{sp.segmentCount} 段 · {sp.totalDurationS.toFixed(1)}s</span>
                      <span className={`text-xs font-medium ${statusColor}`}>{statusLabel}</span>
                    </div>

                    {/* Provider Tabs (only when multi-provider payload) */}
                    {hasMultiProvider ? (
                      <div className="flex gap-1">
                        {PROVIDER_TAB_ORDER.filter((p) => !!providerMap[p]).map((prov) => {
                          const isActive = currentProvider === prov
                          return (
                            <button
                              key={prov}
                              className={`h-7 rounded-md px-3 text-xs font-medium transition ${
                                isActive
                                  ? 'bg-teal-600 text-white'
                                  : 'bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700'
                              }`}
                              onClick={() => handleProviderChange(sp.speakerId, prov)}
                              type="button"
                            >
                              {PROVIDER_SHORT_LABELS[prov] ?? prov}
                            </button>
                          )
                        })}
                      </div>
                    ) : null}

                    {/* Voice select + preview + clone */}
                    <div className="flex items-center gap-2 flex-wrap">
                      <select
                        className="h-8 rounded border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 px-2 text-sm text-foreground w-[220px] truncate"
                        onChange={(e) => handleVoiceChange(sp.speakerId, e.target.value)}
                        value={state?.voiceId ?? ''}
                      >
                        <option value="">-- 选择音色 --</option>
                        {/* Smart recommendations (Task 2): top match + backups, pinned to top */}
                        {(() => {
                          const provMatch = sp.autoMatchedByProvider[currentProvider]
                          if (!provMatch?.voiceId) return null
                          const voiceById = new Map(voicesForProvider.map((v) => [v.voiceId, v]))
                          // Top match first, then backups (keep order, dedupe).
                          const recIds: string[] = [provMatch.voiceId]
                          for (const b of provMatch.backups) {
                            if (!recIds.includes(b.voiceId)) recIds.push(b.voiceId)
                          }
                          if (recIds.length === 0) return null
                          return (
                            <optgroup label="🎯 智能推荐 (按匹配度排序)">
                              {recIds.map((vid, i) => {
                                const v = voiceById.get(vid)
                                const fallbackLabel =
                                  vid === provMatch.voiceId
                                    ? provMatch.label
                                    : provMatch.backups.find((b) => b.voiceId === vid)?.label || vid
                                const baseLabel = v ? formatVoiceOptionLabel(v) : fallbackLabel
                                const prefix = i === 0 ? '★ 自动匹配' : `#${i + 1} 推荐`
                                return (
                                  <option key={`rec-${vid}`} value={vid}>
                                    {`${prefix} · ${baseLabel}`}
                                  </option>
                                )
                              })}
                            </optgroup>
                          )
                        })()}
                        {/* MiniMax: personal voices first, then catalog grouped by gender */}
                        {currentProvider === 'minimax' && personalVoices.filter((v) => !expiredVoiceIds.includes(v.voiceId)).length > 0 ? (
                          <optgroup label="我的音色">
                            {personalVoices
                              .filter((v) => !expiredVoiceIds.includes(v.voiceId))
                              .map((v) => (
                                <option key={v.voiceId} value={v.voiceId}>
                                  {v.label || v.voiceId}
                                </option>
                              ))}
                          </optgroup>
                        ) : null}
                        {/* All providers: catalog voices grouped by gender */}
                        {(() => {
                          const femaleVoices = voicesForProvider.filter((v) => v.gender === 'female')
                          const maleVoices = voicesForProvider.filter((v) => v.gender === 'male')
                          const otherVoices = voicesForProvider.filter((v) => v.gender !== 'male' && v.gender !== 'female')
                          return (
                            <>
                              {femaleVoices.length > 0 ? (
                                <optgroup label={`女声 (${femaleVoices.length})`}>
                                  {femaleVoices.map((v) => <option key={v.voiceId} value={v.voiceId}>{formatVoiceOptionLabel(v)}</option>)}
                                </optgroup>
                              ) : null}
                              {maleVoices.length > 0 ? (
                                <optgroup label={`男声 (${maleVoices.length})`}>
                                  {maleVoices.map((v) => <option key={v.voiceId} value={v.voiceId}>{formatVoiceOptionLabel(v)}</option>)}
                                </optgroup>
                              ) : null}
                              {otherVoices.length > 0 ? (
                                <optgroup label={`其他 (${otherVoices.length})`}>
                                  {otherVoices.map((v) => <option key={v.voiceId} value={v.voiceId}>{formatVoiceOptionLabel(v)}</option>)}
                                </optgroup>
                              ) : null}
                            </>
                          )
                        })()}
                      </select>

                      {/* Preview button */}
                      {state?.voiceId ? (
                        <button
                          className="h-8 rounded border border-slate-300 dark:border-slate-600 px-3 text-xs font-medium text-slate-500 dark:text-slate-400 transition hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50"
                          disabled={previewLoading[sp.speakerId] || !state?.voiceId}
                          onClick={() => { void handlePreview(sp.speakerId) }}
                          type="button"
                        >
                          {previewLoading[sp.speakerId] ? '试听中...' : '试听'}
                        </button>
                      ) : null}

                      {showClone ? (
                        <button
                          className="h-8 rounded border border-teal-500/40 bg-teal-500/10 px-3 text-xs font-medium text-teal-600 dark:text-teal-400 transition hover:bg-teal-500/20 disabled:opacity-50"
                          disabled={state?.isCloning}
                          onClick={() => setCloneModalSpeaker(sp.speakerId)}
                          type="button"
                        >
                          {state?.isCloning ? '克隆中...' : '克隆音色'}
                        </button>
                      ) : null}
                    </div>

                    {/* TTS quality tier + credits — reads from Gateway pricing */}
                    {pricing ? (() => {
                      const prov = currentProvider
                      const cpm = pricing.credits_per_minute
                      if (prov === 'minimax') {
                        const model = state?.minimaxModel ?? 'turbo'
                        return (
                          <div className="flex items-center gap-4 flex-wrap">
                            <label className="flex items-center gap-1.5 cursor-pointer" onClick={() => setVoiceStates((prev) => ({ ...prev, [sp.speakerId]: { ...prev[sp.speakerId], minimaxModel: 'turbo' } }))}>
                              <span className={`flex h-3.5 w-3.5 items-center justify-center rounded-full border-2 ${model === 'turbo' ? 'border-teal-500' : 'border-slate-400 dark:border-slate-600'}`}>
                                {model === 'turbo' ? <span className="h-1.5 w-1.5 rounded-full bg-teal-500" /> : null}
                              </span>
                              <span className="text-xs text-foreground">高级音质</span>
                              <span className="text-xs text-slate-400">{cpm.minimax_turbo} 点/分钟</span>
                            </label>
                            <label className="flex items-center gap-1.5 cursor-pointer" onClick={() => setVoiceStates((prev) => ({ ...prev, [sp.speakerId]: { ...prev[sp.speakerId], minimaxModel: 'hd' } }))}>
                              <span className={`flex h-3.5 w-3.5 items-center justify-center rounded-full border-2 ${model === 'hd' ? 'border-teal-500' : 'border-slate-400 dark:border-slate-600'}`}>
                                {model === 'hd' ? <span className="h-1.5 w-1.5 rounded-full bg-teal-500" /> : null}
                              </span>
                              <span className="text-xs text-foreground">旗舰音质</span>
                              <span className="text-xs text-slate-400">{cpm.minimax_hd} 点/分钟</span>
                            </label>
                          </div>
                        )
                      }
                      const pts = prov === 'cosyvoice' ? cpm.cosyvoice : prov === 'volcengine' ? cpm.volcengine : null
                      return pts != null ? (
                        <div className="flex items-center gap-1.5">
                          <span className="flex h-3.5 w-3.5 items-center justify-center rounded-full border-2 border-teal-500">
                            <span className="h-1.5 w-1.5 rounded-full bg-teal-500" />
                          </span>
                          <span className="text-xs text-foreground">标准音质</span>
                          <span className="text-xs text-slate-400">{pts} 点/分钟</span>
                        </div>
                      ) : null
                    })() : null}

                    {previewError[sp.speakerId] ? (
                      <p className="text-xs text-red-500">{previewError[sp.speakerId]}</p>
                    ) : null}
                    {state?.cloneError ? (
                      <p className="text-xs text-red-500">{state.cloneError}</p>
                    ) : null}
                  </div>
                </div>
              </div>
            )
          })}
        </div>

        {/* Error */}
        {error ? <p className="text-sm text-red-500">{error}</p> : null}

        {/* Footer */}
        <div className="flex items-center justify-between pt-2 border-t border-slate-200 dark:border-slate-700">
          <span className="text-xs text-slate-400">
            {speakers.filter((sp) => voiceStates[sp.speakerId]?.voiceId).length} / {speakers.length} 说话人已配置音色
          </span>
          <button
            className="rounded-lg bg-teal-600 px-5 py-2 text-sm font-medium text-white transition hover:bg-teal-700 disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={!allSelected || isSubmitting || anyCloning}
            onClick={() => { void handleSubmit() }}
            type="button"
          >
            {isSubmitting ? '确认中...' : '确认音色选择'}
          </button>
        </div>
      </section>

      {/* Clone Modal */}
      {cloneModalSpeaker && selectedSpeaker ? (
        <VoiceCloneModal
          cloneCostCredits={cloneCostCredits}
          jobId={jobId}
          onClose={() => setCloneModalSpeaker(null)}
          onComplete={handleCloneComplete}
          speaker={selectedSpeaker}
        />
      ) : null}
    </>
  )
}

/* ---------- VoiceCloneModal ---------- */

// Exported so the editing-state voice Tab (VoiceModifyTab) can reuse
// the exact same clone UX — selecting source segments, paid-API
// trigger, credit cost display. Editing consumers pass a minimal
// speaker-shaped object (speakerId / speakerName) plus the cost.
export interface VoiceCloneModalSpeakerRef {
  speakerId: string
  speakerName: string
}

interface VoiceCloneModalProps {
  jobId: string
  speaker: VoiceCloneModalSpeakerRef
  cloneCostCredits: number
  onClose: () => void
  onComplete: (speakerId: string, voiceId: string) => void
}

export function VoiceCloneModal({ jobId, speaker, cloneCostCredits, onClose, onComplete }: VoiceCloneModalProps) {
  const [segments, setSegments] = useState<SpeakerAudioSegment[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [isLoading, setIsLoading] = useState(true)
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
        if (!cancelled) setError(getErrorMessage(err))
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [jobId, speaker.speakerId])

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
      setError(getErrorMessage(err))
    } finally {
      setIsCloning(false)
    }
  }, [isCloning, meetsMinDuration, exceedsMaxDuration, jobId, speaker.speakerId, selectedIds, onComplete])

  useEffect(() => {
    return () => { if (audioRef.current) { audioRef.current.pause(); audioRef.current = null } }
  }, [])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="w-full max-w-2xl max-h-[85vh] flex flex-col rounded-xl bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 shadow-xl">
        <div className="flex items-center justify-between p-4 border-b border-slate-200 dark:border-slate-700">
          <h3 className="text-base font-semibold text-foreground">克隆音色 — {speaker.speakerName}</h3>
          <button className="text-slate-400 hover:text-foreground transition" onClick={onClose} type="button">
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M6 18L18 6M6 6l12 12" strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} /></svg>
          </button>
        </div>
        <div className="flex items-center gap-3 p-4 border-b border-slate-100 dark:border-slate-800">
          <button className="h-7 rounded border border-teal-500/40 bg-teal-500/10 px-3 text-xs font-medium text-teal-600 dark:text-teal-400 transition hover:bg-teal-500/20" onClick={autoSelect} type="button">自动选择</button>
          <span className="text-xs text-slate-400">从最长片段开始自动勾选，总时长 &lt; 300s</span>
        </div>
        <div className="flex items-center gap-4 px-4 py-2 bg-slate-50/50 dark:bg-slate-800/30">
          <span className="text-xs text-slate-500">已选 <span className="font-medium text-foreground">{selectedIds.size}</span> 段</span>
          <span className="text-xs text-slate-500">总时长 <span className={`font-medium ${exceedsMaxDuration ? 'text-red-500' : meetsMinDuration ? 'text-teal-600 dark:text-teal-400' : 'text-amber-500'}`}>{selectedDuration.toFixed(1)}s</span></span>
          {!meetsMinDuration ? <span className="text-xs text-amber-500">至少需要 10s</span> : exceedsMaxDuration ? <span className="text-xs text-red-500">不能超过 300s</span> : <span className="text-xs text-teal-600 dark:text-teal-400">满足要求</span>}
        </div>
        <div className="flex-1 overflow-y-auto p-4 space-y-1">
          {isLoading ? (
            <div className="text-center py-8 text-sm text-slate-400">加载音频片段...</div>
          ) : segments.length === 0 ? (
            <div className="text-center py-8 text-sm text-slate-400">没有可用的音频片段</div>
          ) : segments.map((seg) => {
            const isSelected = selectedIds.has(seg.segmentId)
            const isPlaying = playingSegmentId === seg.segmentId
            return (
              <div className={`flex items-center gap-3 rounded-lg px-3 py-2 transition cursor-pointer ${isSelected ? 'bg-teal-50 dark:bg-teal-900/20 border border-teal-300 dark:border-teal-700' : 'border border-transparent hover:bg-slate-50 dark:hover:bg-slate-800/40'}`} key={seg.segmentId} onClick={() => toggleSegment(seg.segmentId)}>
                <div className={`h-4 w-4 rounded border-2 flex items-center justify-center shrink-0 ${isSelected ? 'border-teal-500 bg-teal-500' : 'border-slate-300 dark:border-slate-600'}`}>
                  {isSelected ? <svg className="h-3 w-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} /></svg> : null}
                </div>
                <button className="h-7 w-7 rounded-full border border-slate-300 dark:border-slate-600 flex items-center justify-center shrink-0 hover:bg-slate-100 dark:hover:bg-slate-700 transition" onClick={(e) => { e.stopPropagation(); playSegment(seg) }} type="button">
                  {isPlaying ? <svg className="h-3 w-3 text-teal-500" fill="currentColor" viewBox="0 0 24 24"><rect height="16" rx="1" width="4" x="6" y="4" /><rect height="16" rx="1" width="4" x="14" y="4" /></svg> : <svg className="h-3 w-3 text-slate-500" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>}
                </button>
                <span className="flex-1 text-xs text-foreground truncate">{seg.sourceText || `片段 ${seg.segmentId}`}</span>
                <span className="text-xs text-slate-400 shrink-0">{seg.durationS.toFixed(1)}s</span>
              </div>
            )
          })}
        </div>
        <div className="flex items-center justify-between p-4 border-t border-slate-200 dark:border-slate-700">
          <span className="text-xs text-slate-400">{cloneCostCredits > 0 ? `克隆费用：${cloneCostCredits} 点` : '扣点信息暂不可用'}</span>
          <div className="flex items-center gap-2">
            {error ? <span className="text-xs text-red-500 max-w-[200px] truncate">{error}</span> : null}
            <button className="h-8 rounded px-4 text-sm text-slate-500 transition hover:text-foreground" disabled={isCloning} onClick={onClose} type="button">取消</button>
            <button className="h-8 rounded-lg bg-teal-600 px-4 text-sm font-medium text-white transition hover:bg-teal-700 disabled:opacity-50 disabled:cursor-not-allowed" disabled={isCloning || !meetsMinDuration || exceedsMaxDuration} onClick={() => { void handleClone() }} type="button">{isCloning ? '克隆中...' : '开始克隆'}</button>
          </div>
        </div>
      </div>
    </div>
  )
}
