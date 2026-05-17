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
  getVoiceCandidates,
  matchVoiceForSelection,
  previewVoice,
  reassignSpeakerAudioSegment,
  type SpeakerAudioSegment,
  type SpeakerAudioReassignResult,
  type UserVoiceEntry,
  type VoiceCandidate,
  type VoiceCandidatesResponse,
  type VoiceMatchScope,
  type VoiceReuseMatchResponse,
  type VoiceSelectionSpeakerApproval,
  getVoiceSelectionPricing,
  type VoiceSelectionPricingResponse,
  updateSpeakerAudioDubbingMode,
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
  speakerRole: string
  speakerRoleLabel: string
  speakerReviewHint: string
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
  voiceReuse: boolean
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

function formatTimecode(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
}

function formatReuseConfidence(confidence: VoiceReuseMatchResponse['confidence']): string {
  if (confidence === 'strong') return '同一视频 / 同一说话人'
  if (confidence === 'medium') return '同一视频 / 说话人名称相同'
  if (confidence === 'weak') return '同一视频 / 说话人编号可能变化'
  return '可复用候选'
}

function formatSeconds(value: number | null): string | null {
  if (value == null || !Number.isFinite(value)) return null
  return `${value.toFixed(1)}s`
}

/** Phase 2: short badge for personal-voice candidate match scope.
 *  Strong same-source = "★ 强匹配"; medium same-source = "● 同视频";
 *  cross-source named = "○ 跨视频同名". */
function matchScopeBadge(scope: VoiceMatchScope): string {
  switch (scope) {
    case 'same_source_strong':
      return '★ 强匹配'
    case 'same_source_named':
      return '● 同视频同名'
    case 'same_source_speaker_id_changed':
      return '● 同视频'
    case 'cross_source_named_person':
      return '○ 跨视频同名'
    default:
      return '○ 可能匹配'
  }
}

/** Phase 2: optional trailing source-video hint for candidate options.
 *  Returns "" when no useful evidence so the caller can render the bare label. */
function formatCandidateSourceHint(candidate: VoiceCandidate): string {
  const title = candidate.evidence.sourceVideoTitle
  if (!title) return ''
  return ` · ${title}`
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
  // Phase 2 (plan 2026-05-17): per-speaker personal-voice candidates. Loaded
  // best-effort after speakers — strong matches are auto-preselected and
  // emit voice_reuse=true on approval; medium/weak/cross-source show as
  // "需要确认" entries the user can still pick.
  const [voiceCandidates, setVoiceCandidates] = useState<Record<string, VoiceCandidatesResponse>>({})
  const [defaultProvider, setDefaultProvider] = useState('')
  const [hasMultiProvider, setHasMultiProvider] = useState(false)
  const [cloneCostCredits, setCloneCostCredits] = useState(0)
  const [pricing, setPricing] = useState<VoiceSelectionPricingResponse | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cloneModalSpeaker, setCloneModalSpeaker] = useState<string | null>(null)
  const [auditModalSpeaker, setAuditModalSpeaker] = useState<string | null>(null)
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
            speakerRole: String(s.speaker_role ?? ''),
            speakerRoleLabel: String(s.speaker_role_label ?? ''),
            speakerReviewHint: String(s.speaker_review_hint ?? ''),
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
            voiceReuse: false,
            isCloning: false,
            cloneError: null,
          }
        }

        // Phase 2: fetch personal-voice candidates per speaker (best-effort).
        // Done BEFORE setVoiceStates so strong matches can upgrade the initial
        // state to voiceSource='cloned' + voiceReuse=true. Failures degrade
        // silently — the panel still works without candidates. No paid API
        // calls in this path (read-only registry lookup on Gateway).
        const candidateMap: Record<string, VoiceCandidatesResponse> = {}
        await Promise.allSettled(
          loadedSpeakers.map(async (sp) => {
            try {
              const result = await getVoiceCandidates({
                jobId,
                speakerId: sp.speakerId,
                speakerName: sp.speakerName,
                selectedProvider: loadedDefaultProvider,
              })
              candidateMap[sp.speakerId] = result
            } catch (err) {
              // Best-effort; skip this speaker's candidates on failure.
              console.warn('getVoiceCandidates failed for speaker', sp.speakerId, err)
            }
          }),
        )
        if (cancelled) return

        // Upgrade initial state for any speaker with an auto-reuse strong
        // match (provided the candidate voice isn't on the expired list).
        // Only fires when initial source is 'auto_matched' or 'catalog' —
        // we never run AFTER user has explicitly picked something this load
        // (this hook only runs once on mount per jobId).
        for (const sp of loadedSpeakers) {
          const candidate = candidateMap[sp.speakerId]?.autoReuseVoice
          if (!candidate) continue
          if (payloadExpired.includes(candidate.voiceId)) continue
          // MiniMax-only for now (personal voices live in MiniMax registry);
          // skip if the default provider isn't minimax.
          if (loadedDefaultProvider && loadedDefaultProvider !== 'minimax') continue
          initialStates[sp.speakerId] = {
            ...initialStates[sp.speakerId],
            voiceId: candidate.voiceId,
            voiceSource: 'cloned',
            voiceReuse: true,
          }
        }

        setSpeakers(loadedSpeakers)
        setVoiceLibrary('voices' in voices ? (voices as { voices: VoiceLibraryEntry[] }).voices : [])
        setPersonalVoices(userVoices)
        setVoiceCandidates(candidateMap)
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
          voiceReuse: false,
          cloneError: null,
        },
      }
    })
  }, [speakers])

  const handleVoiceChange = useCallback((speakerId: string, voiceId: string) => {
    // Phase 2: detect whether the picked voice corresponds to a match
    // candidate (auto-reuse or one of the "需要确认" entries). When it
    // does, set voiceSource='cloned' + voiceReuse=true so the approve
    // payload carries the audit flag and the user isn't charged a
    // clone reserve. Picking a non-matched personal voice still counts
    // as 'cloned' source (it's a clone-typed voice) but voiceReuse stays
    // false — voice_reuse semantically means "matched candidate reuse".
    const candidates = voiceCandidates[speakerId]
    const matchedCandidate = candidates
      ? candidates.autoReuseVoice?.voiceId === voiceId
        ? candidates.autoReuseVoice
        : candidates.personalVoiceCandidates.find((c) => c.voiceId === voiceId) ?? null
      : null
    const isOtherPersonal =
      !matchedCandidate && personalVoices.some((v) => v.voiceId === voiceId)
    setVoiceStates((prev) => ({
      ...prev,
      [speakerId]: {
        ...prev[speakerId],
        voiceId,
        voiceSource: matchedCandidate || isOtherPersonal ? 'cloned' : 'catalog',
        voiceReuse: !!matchedCandidate,
        cloneError: null,
      },
    }))
  }, [voiceCandidates, personalVoices])

  const handleCloneComplete = useCallback((speakerId: string, voiceId: string, options?: { reused?: boolean }) => {
    setVoiceStates((prev) => ({
      ...prev,
      [speakerId]: { ...prev[speakerId], voiceId, voiceSource: 'cloned', voiceReuse: options?.reused ?? false, isCloning: false, cloneError: null },
    }))
    setCloneModalSpeaker(null)
    getUserVoices().then(setPersonalVoices).catch(() => {})
  }, [])

  const handleSpeakerSegmentReassigned = useCallback((result: SpeakerAudioReassignResult) => {
    if (!result.changed) return
    setSpeakers((prev) => prev.map((sp) => {
      if (result.fromSummary && sp.speakerId === result.fromSummary.speakerId) {
        return {
          ...sp,
          segmentCount: result.fromSummary.segmentCount,
          totalDurationS: result.fromSummary.totalDurationS,
        }
      }
      if (result.toSummary && sp.speakerId === result.toSummary.speakerId) {
        return {
          ...sp,
          segmentCount: result.toSummary.segmentCount,
          totalDurationS: result.toSummary.totalDurationS,
        }
      }
      return sp
    }))
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
          [speakerId]: { ...prev[speakerId], voiceId: '', voiceSource: 'catalog', voiceReuse: false },
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
    return speakers.some((sp) => sp.segmentCount > 0)
      && speakers.every((sp) => sp.segmentCount <= 0 || voiceStates[sp.speakerId]?.voiceId)
  }, [speakers, voiceStates])

  const anyCloning = useMemo(() => {
    return Object.values(voiceStates).some((v) => v.isCloning)
  }, [voiceStates])

  const handleSubmit = useCallback(async () => {
    if (!allSelected || isSubmitting) return

    // Phase 4: warn when selected voice cps deviates >30% from target
    const mismatchWarnings: string[] = []
    for (const sp of speakers) {
      if (sp.segmentCount <= 0) continue
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
      const approvals: VoiceSelectionSpeakerApproval[] = speakers.filter((sp) => sp.segmentCount > 0).map((sp) => {
        const st = voiceStates[sp.speakerId]
        const ttsProvider = st?.selectedProvider ?? ''
        return {
          speakerId: sp.speakerId,
          voiceId: st?.voiceId ?? '',
          voiceSource: st?.voiceSource ?? 'catalog',
          ttsProvider,
          voiceReuse: st?.voiceReuse ?? false,
          // Only meaningful for MiniMax — Gateway 据此聚合 job 级 quality_tier。
          // 非 MiniMax speaker 传 undefined，避免误算成 flagship。
          minimaxModel: ttsProvider === 'minimax' ? (st?.minimaxModel ?? 'turbo') : undefined,
        }
      })
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
        <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-3 border-[color:var(--cinnabar)] border-t-transparent" />
        <h3 className="text-lg font-semibold text-foreground">加载音色选择...</h3>
      </section>
    )
  }

  if (error && speakers.length === 0) {
    return (
      <section className="surface-card p-6">
        <p className="text-[color:var(--cinnabar)]">{error}</p>
      </section>
    )
  }

  const selectedCloneSpeaker = speakers.find((s) => s.speakerId === cloneModalSpeaker) ?? null
  const selectedAuditSpeaker = speakers.find((s) => s.speakerId === auditModalSpeaker) ?? null

  return (
    <>
      <section className="surface-card p-6 space-y-6">
        {/* Expired voices banner */}
        {expiredVoiceIds.length > 0 ? (
          <div className="rounded-lg border border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/8 p-3">
            <p className="text-sm text-[color:var(--cinnabar)]">
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
            const hasNoSegments = sp.segmentCount <= 0
            const statusLabel = hasNoSegments
              ? '无片段'
              : state?.voiceSource === 'cloned'
                ? '已克隆'
                : state?.voiceId
                  ? '已选择'
                  : '待选择'
            const statusColor = hasNoSegments
              ? 'text-muted-foreground'
              : state?.voiceSource === 'cloned'
                ? 'text-[color:var(--bamboo)]'
                : state?.voiceId
                  ? 'text-[color:var(--bamboo)]'
                  : 'text-[color:var(--ochre)]'

            return (
              <div key={sp.speakerId} className="rounded-lg border border-border bg-muted/30 p-4">
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
                      {sp.speakerRoleLabel ? (
                        <span className="rounded px-1.5 py-0.5 text-xs font-medium border border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/10 text-[color:var(--ochre)]">
                          {sp.speakerRoleLabel}
                        </span>
                      ) : null}
                      <span className={`text-xs font-medium ${statusColor}`}>{statusLabel}</span>
                    </div>
                    {sp.speakerReviewHint ? (
                      <p className="text-xs text-[color:var(--ochre)]">{sp.speakerReviewHint}</p>
                    ) : null}

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
                                  ? 'bg-primary text-primary-foreground'
                                  : 'bg-muted/50 text-muted-foreground hover:bg-muted'
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
                        className="h-8 rounded border border-border bg-background px-2 text-sm text-foreground w-[220px] truncate"
                        onChange={(e) => handleVoiceChange(sp.speakerId, e.target.value)}
                        value={state?.voiceId ?? ''}
                      >
                        <option value="">-- 选择音色 --</option>
                        {/* Phase 2 (plan 2026-05-17): personal-voice candidates
                           ordered above official recommendations per candidate
                           priority. Only meaningful for MiniMax (personal voices
                           live there). Three groups, in priority order:
                           1) 强匹配 — auto-reuse, voice_reuse=true preselected
                           2) 可能匹配 — requires user confirmation
                           3) 其他个人音色 — full library minus matched ones */}
                        {(() => {
                          if (currentProvider !== 'minimax') return null
                          const candidates = voiceCandidates[sp.speakerId]
                          const auto = candidates?.autoReuseVoice ?? null
                          const showAuto = auto && !expiredVoiceIds.includes(auto.voiceId)
                          if (!showAuto || !auto) return null
                          return (
                            <optgroup label="个人音色 · 强匹配 (不扣点)">
                              <option value={auto.voiceId}>
                                {`★ ${auto.label}${formatCandidateSourceHint(auto)}`}
                              </option>
                            </optgroup>
                          )
                        })()}
                        {(() => {
                          if (currentProvider !== 'minimax') return null
                          const candidates = voiceCandidates[sp.speakerId]
                          const list = (candidates?.personalVoiceCandidates ?? [])
                            .filter((c) => c.requiresUserConfirmation
                              && !expiredVoiceIds.includes(c.voiceId))
                          if (list.length === 0) return null
                          return (
                            <optgroup label="个人音色 · 可能匹配 (需要确认)">
                              {list.map((c) => (
                                <option key={`pvc-${c.voiceId}`} value={c.voiceId}>
                                  {`${matchScopeBadge(c.matchScope)} ${c.label}${formatCandidateSourceHint(c)}`}
                                </option>
                              ))}
                            </optgroup>
                          )
                        })()}
                        {(() => {
                          if (currentProvider !== 'minimax') return null
                          const candidates = voiceCandidates[sp.speakerId]
                          const candidateIds = new Set<string>()
                          if (candidates?.autoReuseVoice) {
                            candidateIds.add(candidates.autoReuseVoice.voiceId)
                          }
                          for (const c of candidates?.personalVoiceCandidates ?? []) {
                            candidateIds.add(c.voiceId)
                          }
                          const others = personalVoices.filter((v) =>
                            !expiredVoiceIds.includes(v.voiceId)
                            && !candidateIds.has(v.voiceId),
                          )
                          if (others.length === 0) return null
                          return (
                            <optgroup label="其他个人音色">
                              {others.map((v) => (
                                <option key={v.voiceId} value={v.voiceId}>
                                  {v.label || v.voiceId}
                                </option>
                              ))}
                            </optgroup>
                          )
                        })()}
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
                          className="h-8 rounded border border-border px-3 text-xs font-medium text-muted-foreground transition hover:bg-muted disabled:opacity-50"
                          disabled={previewLoading[sp.speakerId] || !state?.voiceId}
                          onClick={() => { void handlePreview(sp.speakerId) }}
                          type="button"
                        >
                          {previewLoading[sp.speakerId] ? '试听中...' : '试听'}
                        </button>
                      ) : null}

                      <button
                        className="h-8 rounded border border-border px-3 text-xs font-medium text-muted-foreground transition hover:bg-muted disabled:opacity-50"
                        disabled={sp.segmentCount <= 0}
                        onClick={() => setAuditModalSpeaker(sp.speakerId)}
                        type="button"
                      >
                        核对原音
                      </button>

                      {showClone ? (
                        <button
                          className="h-8 rounded px-3 text-xs font-medium transition border border-[color:var(--cinnabar)]/40 bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)] hover:bg-[color:var(--cinnabar)]/20 disabled:opacity-50"
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
                              <span className={`flex h-3.5 w-3.5 items-center justify-center rounded-full border-2 ${model === 'turbo' ? 'border-[color:var(--cinnabar)]' : 'border-muted-foreground/40'}`}>
                                {model === 'turbo' ? <span className="h-1.5 w-1.5 rounded-full bg-[color:var(--cinnabar)]" /> : null}
                              </span>
                              <span className="text-xs text-foreground">高级音质</span>
                              <span className="text-xs text-slate-400">{cpm.minimax_turbo} 点/分钟</span>
                            </label>
                            <label className="flex items-center gap-1.5 cursor-pointer" onClick={() => setVoiceStates((prev) => ({ ...prev, [sp.speakerId]: { ...prev[sp.speakerId], minimaxModel: 'hd' } }))}>
                              <span className={`flex h-3.5 w-3.5 items-center justify-center rounded-full border-2 ${model === 'hd' ? 'border-[color:var(--cinnabar)]' : 'border-muted-foreground/40'}`}>
                                {model === 'hd' ? <span className="h-1.5 w-1.5 rounded-full bg-[color:var(--cinnabar)]" /> : null}
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
                          <span className="flex h-3.5 w-3.5 items-center justify-center rounded-full border-2 border-[color:var(--cinnabar)]">
                            <span className="h-1.5 w-1.5 rounded-full bg-[color:var(--cinnabar)]" />
                          </span>
                          <span className="text-xs text-foreground">标准音质</span>
                          <span className="text-xs text-slate-400">{pts} 点/分钟</span>
                        </div>
                      ) : null
                    })() : null}

                    {previewError[sp.speakerId] ? (
                      <p className="text-xs text-[color:var(--cinnabar)]">{previewError[sp.speakerId]}</p>
                    ) : null}
                    {state?.cloneError ? (
                      <p className="text-xs text-[color:var(--cinnabar)]">{state.cloneError}</p>
                    ) : null}
                  </div>
                </div>
              </div>
            )
          })}
        </div>

        {/* Error */}
        {error ? <p className="text-sm text-[color:var(--cinnabar)]">{error}</p> : null}

        {/* Footer */}
        <div className="flex items-center justify-between pt-2 border-t border-border">
          <span className="text-xs text-slate-400">
            {speakers.filter((sp) => sp.segmentCount > 0 && voiceStates[sp.speakerId]?.voiceId).length} / {speakers.filter((sp) => sp.segmentCount > 0).length} 说话人已配置音色
          </span>
          <button
            className="rounded-lg bg-primary px-5 py-2 text-sm font-medium text-primary-foreground transition hover:bg-primary/85 disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={!allSelected || isSubmitting || anyCloning}
            onClick={() => { void handleSubmit() }}
            type="button"
          >
            {isSubmitting ? '确认中...' : '确认音色选择'}
          </button>
        </div>
      </section>

      {/* Clone Modal */}
      {cloneModalSpeaker && selectedCloneSpeaker ? (
        <VoiceCloneModal
          cloneCostCredits={cloneCostCredits}
          jobId={jobId}
          onClose={() => setCloneModalSpeaker(null)}
          onComplete={handleCloneComplete}
          selectedProvider={voiceStates[selectedCloneSpeaker.speakerId]?.selectedProvider ?? defaultProvider}
          speaker={selectedCloneSpeaker}
        />
      ) : null}

      {/* Source audio audit modal */}
      {auditModalSpeaker && selectedAuditSpeaker ? (
        <SpeakerAudioAuditModal
          jobId={jobId}
          onClose={() => setAuditModalSpeaker(null)}
          onReassigned={handleSpeakerSegmentReassigned}
          speaker={selectedAuditSpeaker}
          speakerOptions={speakers.map((sp) => ({
            speakerId: sp.speakerId,
            speakerName: sp.speakerName,
          }))}
        />
      ) : null}
    </>
  )
}

/* ---------- SpeakerAudioAuditModal ---------- */

// Exported so the editing-state voice Tab (VoiceModifyTab) can reuse the
// audio-listening UX. Editing 模式 reassign + keep-original 后端端点
// 要求 voice_selection_review 未 approved → editing 状态会 409。
// readOnly=true 隐藏这两个控件,只保留播放 + 段信息显示。
export interface SpeakerAudioAuditModalSpeakerRef {
  speakerId: string
  speakerName: string
}

export interface SpeakerAudioAuditModalProps {
  jobId: string
  speaker: SpeakerAudioAuditModalSpeakerRef
  speakerOptions: Array<Pick<SpeakerPayload, 'speakerId' | 'speakerName'>>
  onClose: () => void
  onReassigned: (result: SpeakerAudioReassignResult) => void
  /** 2026-05-09: editing 模式只读 — 隐藏 reassign / keep-original 控件,
   * 只保留播放 + 段信息显示。reassign/keep-original 改动用 翻译修改 Tab。 */
  readOnly?: boolean
}

export function SpeakerAudioAuditModal({
  jobId,
  speaker,
  speakerOptions,
  onClose,
  onReassigned,
  readOnly = false,
}: SpeakerAudioAuditModalProps) {
  const [segments, setSegments] = useState<SpeakerAudioSegment[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reassigningSegmentId, setReassigningSegmentId] = useState<number | null>(null)
  const [updatingDubbingModeSegmentId, setUpdatingDubbingModeSegmentId] = useState<number | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [playingSegmentId, setPlayingSegmentId] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        setIsLoading(true)
        setError(null)
        const result = await getSpeakerAudioSegments(jobId, speaker.speakerId)
        if (!cancelled) {
          setSegments([...result.segments].sort((a, b) => a.startMs - b.startMs))
        }
      } catch (err) {
        if (!cancelled) setError(getErrorMessage(err))
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [jobId, speaker.speakerId])

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

  const handleReassign = useCallback(async (seg: SpeakerAudioSegment, toSpeakerId: string) => {
    if (!toSpeakerId || toSpeakerId === speaker.speakerId || reassigningSegmentId) return
    setReassigningSegmentId(seg.segmentId)
    setError(null)
    try {
      const result = await reassignSpeakerAudioSegment({
        jobId,
        segmentId: seg.segmentId,
        fromSpeakerId: speaker.speakerId,
        toSpeakerId,
      })
      onReassigned(result)
      setSegments((prev) => prev.filter((item) => item.segmentId !== seg.segmentId))
      if (audioRef.current && playingSegmentId === seg.segmentId) {
        audioRef.current.pause()
        audioRef.current = null
        setPlayingSegmentId(null)
      }
    } catch (err) {
      setError(getErrorMessage(err))
    } finally {
      setReassigningSegmentId(null)
    }
  }, [jobId, onReassigned, playingSegmentId, reassigningSegmentId, speaker.speakerId])

  const handleDubbingModeChange = useCallback(async (seg: SpeakerAudioSegment, keepOriginal: boolean) => {
    if (updatingDubbingModeSegmentId) return
    const nextMode = keepOriginal ? 'keep_original' : 'dub'
    if (seg.dubbingMode === nextMode) return
    setUpdatingDubbingModeSegmentId(seg.segmentId)
    setError(null)
    setSegments((prev) => prev.map((item) => (
      item.segmentId === seg.segmentId ? { ...item, dubbingMode: nextMode } : item
    )))
    try {
      const result = await updateSpeakerAudioDubbingMode({
        jobId,
        segmentId: seg.segmentId,
        speakerId: speaker.speakerId,
        dubbingMode: nextMode,
      })
      setSegments((prev) => prev.map((item) => (
        item.segmentId === result.segmentId
          ? { ...item, dubbingMode: result.dubbingMode }
          : item
      )))
    } catch (err) {
      setSegments((prev) => prev.map((item) => (
        item.segmentId === seg.segmentId ? { ...item, dubbingMode: seg.dubbingMode } : item
      )))
      setError(getErrorMessage(err))
    } finally {
      setUpdatingDubbingModeSegmentId(null)
    }
  }, [jobId, speaker.speakerId, updatingDubbingModeSegmentId])

  useEffect(() => {
    return () => { if (audioRef.current) { audioRef.current.pause(); audioRef.current = null } }
  }, [])

  const totalDuration = useMemo(() => {
    return segments.reduce((sum, seg) => sum + seg.durationS, 0)
  }, [segments])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="w-full max-w-3xl max-h-[85vh] flex flex-col rounded-xl bg-card border border-border shadow-xl">
        <div className="flex items-center justify-between p-4 border-b border-border">
          <h3 className="text-base font-semibold text-foreground">核对原音 — {speaker.speakerName}</h3>
          <button className="text-slate-400 hover:text-foreground transition" onClick={onClose} type="button">
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M6 18L18 6M6 6l12 12" strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} /></svg>
          </button>
        </div>
        <div className="flex items-center gap-4 px-4 py-2 bg-slate-50/50 dark:bg-slate-800/30">
          <span className="text-xs text-slate-500">共 <span className="font-medium text-foreground">{segments.length}</span> 段</span>
          <span className="text-xs text-slate-500">总时长 <span className="font-medium text-foreground">{totalDuration.toFixed(1)}s</span></span>
          <span className="text-xs text-slate-400">按时间排序，修改后会立即保存。</span>
        </div>
        <div className="flex-1 overflow-y-auto p-4 space-y-1">
          {isLoading ? (
            <div className="text-center py-8 text-sm text-slate-400">加载原音片段...</div>
          ) : segments.length === 0 ? (
            <div className="text-center py-8 text-sm text-slate-400">当前说话人没有待核对片段</div>
          ) : segments.map((seg) => {
            const isPlaying = playingSegmentId === seg.segmentId
            const isReassigning = reassigningSegmentId === seg.segmentId
            const isUpdatingMode = updatingDubbingModeSegmentId === seg.segmentId
            return (
              <div className="flex items-center gap-3 rounded-lg border border-transparent px-3 py-2 transition hover:bg-slate-50 dark:hover:bg-slate-800/40" key={seg.segmentId}>
                <button className="h-7 w-7 rounded-full border border-border flex items-center justify-center shrink-0 hover:bg-muted transition" onClick={() => playSegment(seg)} type="button">
                  {isPlaying ? <svg className="h-3 w-3 text-[color:var(--cinnabar)]" fill="currentColor" viewBox="0 0 24 24"><rect height="16" rx="1" width="4" x="6" y="4" /><rect height="16" rx="1" width="4" x="14" y="4" /></svg> : <svg className="h-3 w-3 text-slate-500" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>}
                </button>
                <span className="w-12 shrink-0 text-xs tabular-nums text-slate-400">{formatTimecode(seg.startMs)}</span>
                <span className="min-w-0 flex-1 truncate text-xs text-foreground">{seg.sourceText || `片段 ${seg.segmentId}`}</span>
                <span className="w-12 shrink-0 text-right text-xs text-slate-400">{seg.durationS.toFixed(1)}s</span>
                {readOnly ? null : (
                  <>
                    <label className="flex h-8 w-[102px] shrink-0 items-center justify-center gap-1 rounded border border-slate-300 dark:border-slate-600 px-2 text-xs text-slate-600 dark:text-slate-300">
                      <input
                        checked={seg.dubbingMode === 'keep_original'}
                        className="h-3.5 w-3.5 accent-[color:var(--cinnabar)]"
                        disabled={isUpdatingMode || updatingDubbingModeSegmentId !== null}
                        onChange={(event) => { void handleDubbingModeChange(seg, event.target.checked) }}
                        type="checkbox"
                      />
                      保留原音
                    </label>
                    <select
                      className="h-8 w-[150px] shrink-0 rounded border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 px-2 text-xs text-foreground disabled:opacity-50"
                      disabled={isReassigning || reassigningSegmentId !== null}
                      onChange={(event) => { void handleReassign(seg, event.target.value) }}
                      value={speaker.speakerId}
                    >
                      {speakerOptions.map((option) => (
                        <option key={option.speakerId} value={option.speakerId}>
                          {option.speakerName || option.speakerId}
                        </option>
                      ))}
                    </select>
                  </>
                )}
              </div>
            )
          })}
        </div>
        <div className="flex items-center justify-between p-4 border-t border-border">
          <span className="text-xs text-slate-400">
            {readOnly
              ? '试听原音以核对说话人归属。需修改归属或保留原音请到「翻译修改」Tab 在段落上操作。'
              : '可修改说话人归属，也可让片段跳过翻译配音并保留原音。'}
          </span>
          <div className="flex items-center gap-2">
            {error ? <span className="text-xs text-[color:var(--cinnabar)] max-w-[280px] truncate">{error}</span> : null}
            <button className="h-8 rounded px-4 text-sm text-slate-500 transition hover:text-foreground" onClick={onClose} type="button">关闭</button>
          </div>
        </div>
      </div>
    </div>
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
  selectedProvider?: string
  onClose: () => void
  onComplete: (speakerId: string, voiceId: string, options?: { reused?: boolean }) => void
}

export function VoiceCloneModal({ jobId, speaker, cloneCostCredits, selectedProvider, onClose, onComplete }: VoiceCloneModalProps) {
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
        if (!cancelled) setError(getErrorMessage(err))
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [jobId, speaker.speakerId])

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
      setError(getErrorMessage(err))
    } finally {
      setIsCloning(false)
    }
  }, [isCloning, meetsMinDuration, exceedsMaxDuration, jobId, speaker.speakerId, selectedIds, onComplete])

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
          <h3 className="text-base font-semibold text-foreground">克隆音色 — {speaker.speakerName}</h3>
          <button className="text-slate-400 hover:text-foreground transition" onClick={onClose} type="button">
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M6 18L18 6M6 6l12 12" strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} /></svg>
          </button>
        </div>
        {isCheckingReuse || (reuseMatch?.matched && reuseMatch.voice) ? (
          <div className="border-b border-border px-4 py-3">
            {isCheckingReuse ? (
              <p className="text-xs text-slate-500">正在检查个人音色库...</p>
            ) : reuseMatch?.matched && reuseMatch.voice ? (
              <div className="rounded-lg border border-[color:var(--bamboo)]/30 bg-[color:var(--bamboo)]/10 p-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 space-y-1">
                    <p className="text-sm font-medium text-foreground">发现可复用音色：{reuseMatch.voice.label || reuseMatch.voice.voiceId}</p>
                    <p className="text-xs text-slate-500">
                      {formatReuseConfidence(reuseMatch.confidence)}
                      {reuseMatch.voice.sourceVideoTitle ? ` · ${reuseMatch.voice.sourceVideoTitle}` : ''}
                    </p>
                    <p className="text-xs text-slate-500">
                      复用不会消耗克隆点数
                      {formatSeconds(reuseMatch.voice.cloneSampleSeconds) ? ` · 原样本 ${formatSeconds(reuseMatch.voice.cloneSampleSeconds)}` : ''}
                      {reuseMatch.voice.provider ? ` · ${reuseMatch.voice.provider}` : ''}
                    </p>
                  </div>
                  <button
                    className="h-8 rounded-lg bg-primary px-3 text-xs font-medium text-primary-foreground transition hover:bg-primary/85"
                    onClick={handleReuse}
                    type="button"
                  >
                    复用此音色
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
        <div className="flex items-center gap-3 p-4 border-b border-slate-100 dark:border-slate-800">
          <button className="h-7 rounded px-3 text-xs font-medium transition border border-[color:var(--cinnabar)]/40 bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)] hover:bg-[color:var(--cinnabar)]/20" onClick={autoSelect} type="button">自动选择</button>
          <span className="text-xs text-slate-400">从最长片段开始自动勾选，总时长 &lt; 300s</span>
        </div>
        <div className="flex items-center gap-4 px-4 py-2 bg-slate-50/50 dark:bg-slate-800/30">
          <span className="text-xs text-slate-500">已选 <span className="font-medium text-foreground">{selectedIds.size}</span> 段</span>
          <span className="text-xs text-slate-500">总时长 <span className={`font-medium ${exceedsMaxDuration ? 'text-[color:var(--cinnabar)]' : meetsMinDuration ? 'text-[color:var(--bamboo)]' : 'text-[color:var(--ochre)]'}`}>{selectedDuration.toFixed(1)}s</span></span>
          {!meetsMinDuration ? <span className="text-xs text-[color:var(--ochre)]">至少需要 10s</span> : exceedsMaxDuration ? <span className="text-xs text-[color:var(--cinnabar)]">不能超过 300s</span> : <span className="text-xs text-[color:var(--bamboo)]">满足要求</span>}
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
              <div className={`flex items-center gap-3 rounded-lg px-3 py-2 transition cursor-pointer ${isSelected ? 'border bg-[color:var(--cinnabar)]/10 border-[color:var(--cinnabar)]/40' : 'border border-transparent hover:bg-muted/40'}`} key={seg.segmentId} onClick={() => toggleSegment(seg.segmentId)}>
                <div className={`h-4 w-4 rounded border-2 flex items-center justify-center shrink-0 ${isSelected ? 'border-teal-500 bg-teal-500' : 'border-slate-300 dark:border-slate-600'}`}>
                  {isSelected ? <svg className="h-3 w-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} /></svg> : null}
                </div>
                <button className="h-7 w-7 rounded-full border border-border flex items-center justify-center shrink-0 hover:bg-muted transition" onClick={(e) => { e.stopPropagation(); playSegment(seg) }} type="button">
                  {isPlaying ? <svg className="h-3 w-3 text-[color:var(--cinnabar)]" fill="currentColor" viewBox="0 0 24 24"><rect height="16" rx="1" width="4" x="6" y="4" /><rect height="16" rx="1" width="4" x="14" y="4" /></svg> : <svg className="h-3 w-3 text-slate-500" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>}
                </button>
                <span className="flex-1 text-xs text-foreground truncate">{seg.sourceText || `片段 ${seg.segmentId}`}</span>
                <span className="text-xs text-slate-400 shrink-0">{seg.durationS.toFixed(1)}s</span>
              </div>
            )
          })}
        </div>
        <div className="flex items-center justify-between p-4 border-t border-border">
          <span className="text-xs text-slate-400">
            {reuseMatch?.matched
              ? cloneCostCredits > 0 ? `重新克隆会消耗 ${cloneCostCredits} 点` : '重新克隆会消耗克隆点数'
              : cloneCostCredits > 0 ? `克隆费用：${cloneCostCredits} 点` : '扣点信息暂不可用'}
          </span>
          <div className="flex items-center gap-2">
            {error ? <span className="text-xs text-[color:var(--cinnabar)] max-w-[200px] truncate">{error}</span> : null}
            <button className="h-8 rounded px-4 text-sm text-slate-500 transition hover:text-foreground" disabled={isCloning} onClick={onClose} type="button">取消</button>
            <button className="h-8 rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground transition hover:bg-primary/85 disabled:opacity-50 disabled:cursor-not-allowed" disabled={isCloning || !meetsMinDuration || exceedsMaxDuration} onClick={() => { void handleClone() }} type="button">{isCloning ? '克隆中...' : reuseMatch?.matched ? '重新克隆' : '开始克隆'}</button>
          </div>
        </div>
      </div>
    </div>
  )
}
