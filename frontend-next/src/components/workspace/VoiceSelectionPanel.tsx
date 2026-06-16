"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { useConfirmDialog } from '@/components/ui/confirm-dialog'
import { getErrorMessage } from '@/lib/api/errors'
import { getJob } from '@/lib/api/jobs'
import { getVoiceLibrary, type VoiceLibraryEntry } from '@/lib/api/voiceLibrary'
import {
  approveVoiceSelection,
  deleteUserVoice,
  getUserVoices,
  getVoiceCandidates,
  previewVoice,
  type SpeakerAudioReassignResult,
  type UserVoiceEntry,
  type VoiceCandidate,
  type VoiceCandidatesResponse,
  type VoiceMatchScope,
  type VoiceSelectionSpeakerApproval,
  getVoiceSelectionPricing,
  type VoiceSelectionPricingResponse,
} from '@/lib/api/voiceSelection'
import { apiClient } from '@/lib/api/client'
// Phase 4.2 E.1 — CosyVoice clone wiring (file-upload only; source_segments
// UI deferred to E.2). The button onClick below splits by provider:
// MiniMax keeps using the legacy VoiceCloneModal (extracted verbatim to
// ./VoiceCloneModal.tsx 2026-06-11; function body untouched, locked by
// G_MX.2 / G6.1.5 guards); CosyVoice routes to the dedicated
// CosyVoiceCloneModal from voice-clone/ which talks to the
// /api/voice/cosyvoice/clone endpoint via cosyvoiceClone.ts.
import { CosyVoiceCloneModal } from '@/components/voice-clone/CosyVoiceCloneModal'
import { SpeakerAudioAuditModal } from './SpeakerAudioAuditModal'
import { VoiceCloneModal } from './VoiceCloneModal'
import {
  getCosyvoiceCloneGate,
  type CosyvoiceCloneGateResponse,
} from '@/lib/api/cosyvoiceClone'
import type { ApiWebUiStateResponse } from '@/types/api'
import type { JobSummary } from '@/types/jobs'

/* ---------- Types ---------- */

interface ProbeText {
  segmentId: number
  sourceText: string
  cnText: string
}

interface SmartOfferedCandidate {
  voiceId: string
  userVoiceId?: string | null
  label?: string | null
  confidence?: string | null
  matchScope?: string | null
  reason?: string | null
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
  // Phase 4 follow-up (plan 2026-05-17): when Smart pipeline pauses on a
  // weak/cross-source personal-voice match, the backend writes a list of
  // offered candidates into vs_payload.speakers[i].smart_offered_candidates.
  // The first entry (highest-ranked candidate) becomes the default
  // selection so the user sees the recommendation without having to
  // scroll through the dropdown. Empty/absent for non-paused jobs.
  smartOfferedCandidates: SmartOfferedCandidate[]
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
  // Phase 4.2 E.1: separate state for CosyVoice clone — different modal,
  // different endpoint, different consent flow. Keeping it disjoint from
  // `cloneModalSpeaker` ensures dispatching the wrong modal is impossible
  // (only one of the two states is non-null at any time per the onClick
  // handler below).
  const [cosyvoiceCloneModalSpeaker, setCosyvoiceCloneModalSpeaker] =
    useState<string | null>(null)
  // Phase 4.2 E.1: clone-gate visibility cache. Fetched once on first mount
  // and re-fetched if the user becomes authenticated (component remount).
  // null = not yet loaded / fetch failed (treat as denied so we don't show
  // a button that the backend will 403).
  const [cosyvoiceCloneGate, setCosyvoiceCloneGate] =
    useState<CosyvoiceCloneGateResponse | null>(null)
  const [auditModalSpeaker, setAuditModalSpeaker] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState<Record<string, boolean>>({})
  const [previewError, setPreviewError] = useState<Record<string, string | null>>({})
  const [expiredVoiceIds, setExpiredVoiceIds] = useState<string[]>([])
  const previewAudioRef = useRef<HTMLAudioElement | null>(null)
  const { confirm, confirmDialog } = useConfirmDialog()

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

          // Phase 4 follow-up (plan 2026-05-17): smart_offered_candidates
          // is written by src/pipeline/process.py when Smart pipeline
          // pauses on weak/cross-source personal-voice matches. Each
          // item shape (per process.py:4166-4177):
          //   { voice_id, user_voice_id, label, confidence,
          //     match_scope, reason }
          // Filter to entries with a non-empty voice_id so the
          // pre-select tier-2 logic below can trust [0] is selectable.
          const rawOfferedCandidates = Array.isArray(s.smart_offered_candidates)
            ? (s.smart_offered_candidates as Record<string, unknown>[])
            : []
          const smartOfferedCandidates: SmartOfferedCandidate[] = rawOfferedCandidates
            .map((c) => ({
              voiceId: String(c.voice_id ?? ''),
              userVoiceId: c.user_voice_id != null ? String(c.user_voice_id) : null,
              label: c.label != null ? String(c.label) : null,
              confidence: c.confidence != null ? String(c.confidence) : null,
              matchScope: c.match_scope != null ? String(c.match_scope) : null,
              reason: c.reason != null ? String(c.reason) : null,
            }))
            .filter((c) => c.voiceId)

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
            smartOfferedCandidates,
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

        // Upgrade initial state per layered priority (plan 2026-05-17
        // §候选源优先级). Only runs once on mount per jobId, so it never
        // overrides a user pick mid-session.
        //
        //   Tier 1: Phase 2 strong-match preselect (autoReuseVoice)
        //   Tier 2: Phase 4 smart-offered candidate (only when Smart paused
        //           specifically for this speaker because the candidate
        //           was weak/cross-source — the backend already decided
        //           it was worth user confirmation, so we surface it as
        //           the default instead of forcing the user to dig in
        //           the dropdown)
        //   Tier 3: keep the auto_matched / catalog fallback already set
        //           above (no upgrade)
        for (const sp of loadedSpeakers) {
          const tier1 = candidateMap[sp.speakerId]?.autoReuseVoice
          const tier1Usable =
            !!tier1 &&
            !payloadExpired.includes(tier1.voiceId) &&
            // MiniMax-only for now (personal voices live in MiniMax
            // registry); skip if the default provider isn't minimax.
            (!loadedDefaultProvider || loadedDefaultProvider === 'minimax')
          if (tier1Usable && tier1) {
            initialStates[sp.speakerId] = {
              ...initialStates[sp.speakerId],
              voiceId: tier1.voiceId,
              voiceSource: 'cloned',
              voiceReuse: true,
            }
            continue
          }
          // Tier 2: smart paused on a weak/cross-source candidate for
          // this speaker. Pipeline writes the offered list ranked by
          // confidence; pick the head. Skip if expired (defence-in-depth
          // — pipeline shouldn't offer an expired voice but the
          // expired_voice_ids guard is the contract we trust). Also skip
          // when the picked provider isn't MiniMax — Phase 4 personal
          // voice candidates only exist in MiniMax registry today,
          // mirroring the tier-1 provider gate.
          const tier2 = sp.smartOfferedCandidates[0]
          const tier2Usable =
            !!tier2 &&
            !!tier2.voiceId &&
            !payloadExpired.includes(tier2.voiceId) &&
            (!loadedDefaultProvider || loadedDefaultProvider === 'minimax')
          if (tier2Usable && tier2) {
            initialStates[sp.speakerId] = {
              ...initialStates[sp.speakerId],
              voiceId: tier2.voiceId,
              voiceSource: 'cloned',
              voiceReuse: true,
            }
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

    // Phase 2 follow-up (plan 2026-05-17 review P2-4): candidates were
    // fetched once on mount with the default provider. Personal voices
    // are provider-isolated (a MiniMax clone never appears in the
    // CosyVoice candidate set), so switching from CosyVoice → MiniMax
    // mid-session used to leave the candidate optgroups stale or empty.
    // Refetch for this speaker only — best-effort, log on failure.
    const sp = speakers.find((s) => s.speakerId === speakerId)
    if (!sp) return
    void (async () => {
      try {
        const result = await getVoiceCandidates({
          jobId,
          speakerId,
          speakerName: sp.speakerName,
          selectedProvider: provider,
        })
        setVoiceCandidates((prev) => ({ ...prev, [speakerId]: result }))
      } catch (err) {
        console.warn('getVoiceCandidates refetch failed for speaker', speakerId, err)
      }
    })()
  }, [speakers, jobId])

  const handleVoiceChange = useCallback((speakerId: string, voiceId: string) => {
    // Phase 2 (plan 2026-05-17): detect whether the picked voice
    // corresponds to a match candidate (auto-reuse or one of the
    // "需要确认" entries) OR is one of the user's other personal
    // voices ("其他个人音色" optgroup). Both cases reuse an
    // existing cloned voice — no clone provider call, no clone
    // points — and BOTH must carry voiceReuse=true so the gateway
    // audit (``_record_voice_reuse_events``) records the reuse.
    // Otherwise picks from "其他个人音色" silently bypass the audit
    // even though they ARE a reuse event.
    const candidates = voiceCandidates[speakerId]
    const matchedCandidate = candidates
      ? candidates.autoReuseVoice?.voiceId === voiceId
        ? candidates.autoReuseVoice
        : candidates.personalVoiceCandidates.find((c) => c.voiceId === voiceId) ?? null
      : null
    const isOtherPersonal =
      !matchedCandidate && personalVoices.some((v) => v.voiceId === voiceId)
    const isPersonalVoice = !!matchedCandidate || isOtherPersonal
    setVoiceStates((prev) => ({
      ...prev,
      [speakerId]: {
        ...prev[speakerId],
        voiceId,
        voiceSource: isPersonalVoice ? 'cloned' : 'catalog',
        voiceReuse: isPersonalVoice,
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

  // Phase 4.2 E.1: fetch CosyVoice clone-gate once on mount. Failure is
  // treated as "denied" (state stays null) so we don't render a button the
  // backend will 403. The gate is per-user policy state (D.1 endpoint);
  // does not need to be re-polled.
  useEffect(() => {
    let cancelled = false
    getCosyvoiceCloneGate()
      .then((data) => {
        if (cancelled) return
        setCosyvoiceCloneGate(data)
      })
      .catch(() => {
        // Silent fail → null state → button hidden for CosyVoice provider.
        // Common reasons: user unauthenticated (401), gateway down. The
        // visible "no clone available" state matches the safe default.
      })
    return () => {
      cancelled = true
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
      const confirmed = await confirm({ title: '语速差异提醒', description: msg })
      if (!confirmed) return
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
  }, [allSelected, isSubmitting, speakers, voiceStates, jobId, onAdvanced, fallbackVoices, providerMap, hasMultiProvider, confirm])

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
    const provider = hasMultiProvider
      ? state.selectedProvider
      : defaultProvider
    // Runtime availability (A0b): worker enabled? OSS configured? DashScope
    // key in env? Computed in pipeline.process._build_voice_selection_review.
    const supportsClone = hasMultiProvider
      ? providerMap[state.selectedProvider]?.supportsClone ?? false
      : defaultProvider === 'minimax'
    if (!supportsClone) return false
    // Phase 4.2 E.1: CosyVoice provider adds a second gate — policy-level
    // authorization visibility (admin / allowlist / GA). MiniMax is
    // unaffected (legacy behavior preserved).
    //
    // PR #15 P2 fix (Codex 2026-05-27): also AND runtime_ready —
    // policy authorization alone is not enough; the backend Layers 2-3
    // (worker_enabled + uploader production-ready) must also be in place.
    // Without this AND, admin/allowlist users see the button but POST
    // /clone immediately 503s on `clone_feature_disabled` /
    // `sample_uploader_not_configured`. The backend computes the joint
    // result as `can_show_clone_button = can_access_clone && runtime_ready`
    // — we reference it directly to stay in lockstep with the server.
    if (provider === 'cosyvoice') {
      return cosyvoiceCloneGate?.can_show_clone_button === true
    }
    return true
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
                        {/* Phase 4.2 E.1 PR #15 Codex P2² fix (2026-05-27):
                            CosyVoice personal clones. After a successful
                            CosyVoice clone the new voice lives in
                            `personalVoices` but the prior code only
                            rendered MiniMax optgroups → select had a
                            voiceId without a matching <option> → user
                            saw an empty dropdown after closing the modal.
                            Filter on multiple acceptable shapes:
                              - provider === 'cosyvoice_voice_clone' (canonical
                                value gateway writes on clone success)
                              - ttsProvider === 'cosyvoice' (UserVoiceEntry
                                lowercased flavour)
                            so we don't accidentally exclude rows from old
                            test fixtures or upcoming backend renames. */}
                        {(() => {
                          if (currentProvider !== 'cosyvoice') return null
                          const cosyClones = personalVoices.filter((v) => {
                            if (expiredVoiceIds.includes(v.voiceId)) return false
                            return (
                              v.provider === 'cosyvoice_voice_clone'
                              || v.ttsProvider === 'cosyvoice'
                            )
                          })
                          if (cosyClones.length === 0) return null
                          return (
                            <optgroup label="我的 CosyVoice 克隆音色">
                              {cosyClones.map((v) => (
                                <option key={`cosy-clone-${v.voiceId}`} value={v.voiceId}>
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
                          onClick={() => {
                            // Phase 4.2 E.1 — provider-aware dispatch.
                            // MiniMax: keeps legacy in-file VoiceCloneModal
                            // (function body untouched, hits
                            // /jobs/${id}/voice-clone). CosyVoice: opens
                            // the dedicated CosyVoiceCloneModal which hits
                            // /api/voice/cosyvoice/clone. Mutual exclusion
                            // is locked by G6.5.3 URL-set guard.
                            if (currentProvider === 'cosyvoice') {
                              setCosyvoiceCloneModalSpeaker(sp.speakerId)
                            } else {
                              setCloneModalSpeaker(sp.speakerId)
                            }
                          }}
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

      {/* Clone Modal — MiniMax legacy path (./VoiceCloneModal.tsx, locked
          unchanged by G6.1.5 / G_MX.2 guards). */}
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

      {/* Phase 4.2 E.2: CosyVoice clone modal — file upload + source_segments
          picker. `defaultSourceJobId={jobId}` enables the segments radio;
          modal renders `<CosyVoiceSegmentPicker>` internally when user
          switches to that mode. `sourceSegmentIds` prop (D.2 公开契约保留)
          intentionally not passed —— modal's internal state takes over. */}
      {cosyvoiceCloneModalSpeaker ? (
        <CosyVoiceCloneModal
          open={true}
          onClose={() => setCosyvoiceCloneModalSpeaker(null)}
          speakerId={cosyvoiceCloneModalSpeaker}
          speakerName={
            speakers.find((s) => s.speakerId === cosyvoiceCloneModalSpeaker)
              ?.speakerName ?? cosyvoiceCloneModalSpeaker
          }
          defaultSourceJobId={jobId}
          onSuccess={(voice) => {
            handleCloneComplete(cosyvoiceCloneModalSpeaker, voice.voice_id, {
              reused: false,
            })
            setCosyvoiceCloneModalSpeaker(null)
          }}
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

      {confirmDialog}
    </>
  )
}
