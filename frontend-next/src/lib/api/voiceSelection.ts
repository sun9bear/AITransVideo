import { apiClient } from '@/lib/api/client'
import { getJob } from '@/lib/api/jobs'
import type { ReviewJobTransition } from '@/types/reviews'

export interface VoiceSelectionSpeakerApproval {
  speakerId: string
  voiceId: string
  voiceSource: 'catalog' | 'cloned' | 'auto_matched'
  ttsProvider?: string
  voiceReuse?: boolean
  /** MiniMax 专属：用户在 UI 选择的音质档（turbo=高级 30pts/min, hd=旗舰 50pts/min）。
   * 非 MiniMax provider 可不传。Gateway 据此聚合 job 级 quality_tier + tts_model。 */
  minimaxModel?: 'turbo' | 'hd'
}

export async function approveVoiceSelection(
  jobId: string,
  speakers: VoiceSelectionSpeakerApproval[],
): Promise<ReviewJobTransition> {
  await apiClient.post<{ success: boolean }>(
    `/jobs/${jobId}/review/voice-selection/approve`,
    {
      body: {
        speakers: speakers.map((s) => ({
          speaker_id: s.speakerId,
          voice_id: s.voiceId,
          voice_source: s.voiceSource,
          tts_provider: s.ttsProvider ?? '',
          voice_reuse: s.voiceReuse ?? false,
          minimax_model: s.minimaxModel ?? null,
        })),
      },
    },
  )
  return { job: await getJob(jobId) }
}

export interface VoiceCloneInput {
  jobId: string
  speakerId: string
  segmentIds: number[]
}

export async function cloneVoiceForSelection(
  input: VoiceCloneInput,
): Promise<{ voiceId: string; status: string }> {
  const result = await apiClient.post<{ voice_id: string; status: string }>(
    `/jobs/${input.jobId}/voice-clone`,
    {
      body: {
        speaker_id: input.speakerId,
        segment_ids: input.segmentIds,
      },
      // 后端同步执行音频拼接 + MiniMax 上传 + clone（provider 单次 180s
      // 且带重试），且扣点在服务端完成。客户端超时只会造成"前端报失败、
      // 后端照样扣点入库"的脱节 + 用户重试重复扣点，必须禁用默认 30s。
      timeoutMs: 0,
    },
  )
  return { voiceId: result.voice_id, status: result.status }
}

export interface SpeakerAudioSegment {
  segmentId: number
  startMs: number
  endMs: number
  durationS: number
  sourceText: string
  audioUrl: string
  dubbingMode: 'dub' | 'keep_original'
}

export interface SpeakerAudioResponse {
  speakerId: string
  segments: SpeakerAudioSegment[]
  totalDurationS: number
}

export interface SpeakerAudioReassignResult {
  segmentId: number
  fromSpeakerId: string
  toSpeakerId: string
  changed: boolean
  fromSummary?: { speakerId: string; segmentCount: number; totalDurationS: number }
  toSummary?: { speakerId: string; segmentCount: number; totalDurationS: number }
}

export interface VoicePreviewResult {
  audioBase64: string
  expired: boolean
  error: string | null
}

export async function previewVoice(
  jobId: string,
  voiceId: string,
  options?: { ttsProvider?: string; sampleText?: string },
): Promise<VoicePreviewResult> {
  const result = await apiClient.post<{
    audio_base64: string
    expired: boolean
    error: string | null
  }>(`/jobs/${jobId}/review/voice/preview`, {
    body: {
      voice_id: voiceId,
      tts_provider: options?.ttsProvider ?? '',
      sample_text: options?.sampleText ?? '',
    },
  })
  return {
    audioBase64: result.audio_base64,
    expired: result.expired,
    error: result.error,
  }
}

export interface UserVoiceEntry {
  id: string
  voiceId: string
  voiceType: string
  provider: string
  ttsProvider: string | null
  platform: string | null
  label: string
  sourceSpeakerId: string | null
  sourceJobId: string | null
  sourceType: string | null
  sourceRef: string | null
  sourceContentHash: string | null
  sourceUploadMd5: string | null
  sourceVideoTitle: string | null
  sourceSpeakerName: string | null
  sourceSpeakerNameKey: string | null
  sourcePublishedAt: string | null
  sourceContentSummary: string | null
  sourceContentEra: string | null
  sourceContentTags: unknown
  cloneSampleSeconds: number | null
  cloneSampleSegmentIds: unknown
  createdFrom: string | null
  notes: string | null
  createdAt: string | null
}

function mapUserVoiceEntry(v: Record<string, unknown>): UserVoiceEntry {
  return {
    id: String(v.id ?? ''),
    voiceId: String(v.voice_id ?? ''),
    voiceType: String(v.voice_type ?? 'cloned'),
    provider: String(v.provider ?? ''),
    ttsProvider: v.tts_provider ? String(v.tts_provider) : null,
    platform: v.platform ? String(v.platform) : null,
    label: String(v.label ?? v.voice_id ?? ''),
    sourceSpeakerId: v.source_speaker_id ? String(v.source_speaker_id) : null,
    sourceJobId: v.source_job_id ? String(v.source_job_id) : null,
    sourceType: v.source_type ? String(v.source_type) : null,
    sourceRef: v.source_ref ? String(v.source_ref) : null,
    sourceContentHash: v.source_content_hash ? String(v.source_content_hash) : null,
    sourceUploadMd5: v.source_upload_md5 ? String(v.source_upload_md5) : null,
    sourceVideoTitle: v.source_video_title ? String(v.source_video_title) : null,
    sourceSpeakerName: v.source_speaker_name ? String(v.source_speaker_name) : null,
    sourceSpeakerNameKey: v.source_speaker_name_key ? String(v.source_speaker_name_key) : null,
    sourcePublishedAt: v.source_published_at ? String(v.source_published_at) : null,
    sourceContentSummary: v.source_content_summary ? String(v.source_content_summary) : null,
    sourceContentEra: v.source_content_era ? String(v.source_content_era) : null,
    sourceContentTags: v.source_content_tags ?? null,
    cloneSampleSeconds: typeof v.clone_sample_seconds === 'number' ? v.clone_sample_seconds : null,
    cloneSampleSegmentIds: v.clone_sample_segment_ids ?? null,
    createdFrom: v.created_from ? String(v.created_from) : null,
    notes: v.notes ? String(v.notes) : null,
    createdAt: v.created_at ? String(v.created_at) : null,
  }
}

export async function getUserVoices(): Promise<UserVoiceEntry[]> {
  const resp = await fetch('/gateway/user-voices', { credentials: 'include' })
  if (!resp.ok) return []
  const data = await resp.json()
  return (data.voices ?? []).map((v: Record<string, unknown>) => mapUserVoiceEntry(v))
}

export type VoiceReuseConfidence = 'strong' | 'medium' | 'weak'

export interface VoiceReuseCandidate {
  matched: true
  confidence: VoiceReuseConfidence
  reason: string
  score: number
  autoReuseAllowed: boolean
  voice: UserVoiceEntry
}

export interface VoiceReuseMatchResponse {
  matched: boolean
  confidence: VoiceReuseConfidence | null
  autoReuseAllowed: boolean
  reason: string
  voice: UserVoiceEntry | null
  candidates: VoiceReuseCandidate[]
}

export async function matchVoiceForSelection(input: {
  jobId: string
  speakerId: string
  speakerName?: string
  selectedProvider?: string
}): Promise<VoiceReuseMatchResponse> {
  const result = await apiClient.post<{
    matched: boolean
    confidence: VoiceReuseConfidence | null
    auto_reuse_allowed: boolean
    reason: string
    voice: Record<string, unknown> | null
    candidates: Array<{
      matched: true
      confidence: VoiceReuseConfidence
      reason: string
      score: number
      auto_reuse_allowed: boolean
      voice: Record<string, unknown>
    }>
  }>(`/jobs/${input.jobId}/voice-match`, {
    body: {
      speaker_id: input.speakerId,
      speaker_name: input.speakerName ?? '',
      selected_provider: input.selectedProvider ?? '',
    },
  })
  return {
    matched: Boolean(result.matched),
    confidence: result.confidence,
    autoReuseAllowed: Boolean(result.auto_reuse_allowed),
    reason: String(result.reason ?? ''),
    voice: result.voice ? mapUserVoiceEntry(result.voice) : null,
    candidates: (result.candidates ?? []).map((candidate) => ({
      matched: true,
      confidence: candidate.confidence,
      reason: String(candidate.reason ?? ''),
      score: Number(candidate.score ?? 0),
      autoReuseAllowed: Boolean(candidate.auto_reuse_allowed),
      voice: mapUserVoiceEntry(candidate.voice),
    })),
  }
}

/* ---------- Phase 2: Candidate-first voice selection (plan 2026-05-17) ---------- */

export type VoiceMatchScope =
  | 'same_source_strong'
  | 'same_source_named'
  | 'same_source_speaker_id_changed'
  | 'cross_source_named_person'

export interface VoiceCandidateEvidence {
  sourceVideoTitle: string | null
  sourceSpeakerName: string | null
  cloneSampleSeconds: number | null
  createdAt: string | null
}

export interface VoiceCandidate {
  voiceId: string
  userVoiceId: string
  label: string
  confidence: VoiceReuseConfidence
  matchScope: VoiceMatchScope
  requiresUserConfirmation: boolean
  score: number
  reason: string
  evidence: VoiceCandidateEvidence
}

export interface VoiceCandidatesResponse {
  speakerId: string
  sourceContentHash: string | null
  /** Top "strong" same-source match — preselect this without user confirmation.
   *  Backend filters to confidence === "strong" + match_scope === "same_source_strong"
   *  + auto_reuse_allowed === true. */
  autoReuseVoice: VoiceCandidate | null
  /** Strong + medium + weak + cross-source candidates. Items with
   *  requiresUserConfirmation=true should render as "需要确认". */
  personalVoiceCandidates: VoiceCandidate[]
  // official_voice_candidates omitted — always [] in Phase 1; frontend continues
  // to read official voices from review_state.payload (existing path).
}

function mapVoiceCandidate(raw: Record<string, unknown>): VoiceCandidate {
  const evidenceRaw = (raw.evidence ?? {}) as Record<string, unknown>
  return {
    voiceId: String(raw.voice_id ?? ''),
    userVoiceId: String(raw.user_voice_id ?? ''),
    label: String(raw.label ?? raw.voice_id ?? ''),
    confidence: (raw.confidence ?? 'weak') as VoiceReuseConfidence,
    matchScope: (raw.match_scope ?? 'cross_source_named_person') as VoiceMatchScope,
    requiresUserConfirmation: Boolean(raw.requires_user_confirmation),
    score: Number(raw.score ?? 0),
    reason: String(raw.reason ?? ''),
    evidence: {
      sourceVideoTitle: evidenceRaw.source_video_title
        ? String(evidenceRaw.source_video_title)
        : null,
      sourceSpeakerName: evidenceRaw.source_speaker_name
        ? String(evidenceRaw.source_speaker_name)
        : null,
      cloneSampleSeconds:
        typeof evidenceRaw.clone_sample_seconds === 'number'
          ? evidenceRaw.clone_sample_seconds
          : null,
      createdAt: evidenceRaw.created_at ? String(evidenceRaw.created_at) : null,
    },
  }
}

export async function getVoiceCandidates(input: {
  jobId: string
  speakerId: string
  speakerName?: string
  sourceSpeakerNameKey?: string
  selectedProvider?: string
  includeCrossSource?: boolean
  limit?: number
}): Promise<VoiceCandidatesResponse> {
  const result = await apiClient.post<{
    speaker_id: string
    source_content_hash: string | null
    auto_reuse_voice: Record<string, unknown> | null
    personal_voice_candidates: Array<Record<string, unknown>>
    official_voice_candidates: Array<Record<string, unknown>>
  }>(`/jobs/${input.jobId}/voice-candidates`, {
    body: {
      speaker_id: input.speakerId,
      speaker_name: input.speakerName ?? '',
      source_speaker_name_key: input.sourceSpeakerNameKey ?? '',
      selected_provider: input.selectedProvider ?? '',
      include_cross_source: input.includeCrossSource ?? true,
      limit: input.limit ?? 3,
    },
  })
  return {
    speakerId: String(result.speaker_id ?? ''),
    sourceContentHash: result.source_content_hash
      ? String(result.source_content_hash)
      : null,
    autoReuseVoice: result.auto_reuse_voice
      ? mapVoiceCandidate(result.auto_reuse_voice)
      : null,
    personalVoiceCandidates: (result.personal_voice_candidates ?? []).map(
      mapVoiceCandidate,
    ),
  }
}

export async function deleteUserVoice(voiceId: string): Promise<boolean> {
  const resp = await fetch(`/gateway/user-voices/${encodeURIComponent(voiceId)}`, {
    method: 'DELETE',
    credentials: 'include',
  })
  if (!resp.ok) return false
  const data = await resp.json()
  return data.deleted ?? false
}

export async function getSpeakerAudioSegments(
  jobId: string,
  speakerId: string,
): Promise<SpeakerAudioResponse> {
  const result = await apiClient.get<{
    speaker_id: string
    segments: Array<{
      segment_id: number
      start_ms: number
      end_ms: number
      duration_s: number
      source_text: string
      dubbing_mode?: string
      audio_url: string
    }>
    total_duration_s: number
  }>(`/jobs/${jobId}/speaker-audio/${speakerId}`)

  return {
    speakerId: result.speaker_id,
    segments: result.segments.map((s) => ({
      segmentId: s.segment_id,
      startMs: s.start_ms,
      endMs: s.end_ms,
      durationS: s.duration_s,
      sourceText: s.source_text,
      audioUrl: s.audio_url,
      dubbingMode: s.dubbing_mode === 'keep_original' ? 'keep_original' : 'dub',
    })),
    totalDurationS: result.total_duration_s,
  }
}

export async function updateSpeakerAudioDubbingMode(input: {
  jobId: string
  segmentId: number
  speakerId: string
  dubbingMode: 'dub' | 'keep_original'
}): Promise<{ segmentId: number; speakerId: string; dubbingMode: 'dub' | 'keep_original'; changed: boolean }> {
  const result = await apiClient.post<{
    segment_id: number
    speaker_id: string
    dubbing_mode: string
    changed: boolean
  }>(`/jobs/${input.jobId}/speaker-audio/dubbing-mode`, {
    body: {
      segment_id: input.segmentId,
      speaker_id: input.speakerId,
      dubbing_mode: input.dubbingMode,
    },
  })

  return {
    segmentId: result.segment_id,
    speakerId: result.speaker_id,
    dubbingMode: result.dubbing_mode === 'keep_original' ? 'keep_original' : 'dub',
    changed: result.changed,
  }
}

export async function reassignSpeakerAudioSegment(input: {
  jobId: string
  segmentId: number
  fromSpeakerId: string
  toSpeakerId: string
}): Promise<SpeakerAudioReassignResult> {
  const result = await apiClient.post<{
    segment_id: number
    from_speaker_id: string
    to_speaker_id: string
    changed: boolean
    from_summary?: { speaker_id: string; segment_count: number; total_duration_s: number }
    to_summary?: { speaker_id: string; segment_count: number; total_duration_s: number }
  }>(`/jobs/${input.jobId}/speaker-audio/reassign`, {
    body: {
      segment_id: input.segmentId,
      from_speaker_id: input.fromSpeakerId,
      to_speaker_id: input.toSpeakerId,
    },
  })

  return {
    segmentId: result.segment_id,
    fromSpeakerId: result.from_speaker_id,
    toSpeakerId: result.to_speaker_id,
    changed: result.changed,
    fromSummary: result.from_summary
      ? {
          speakerId: result.from_summary.speaker_id,
          segmentCount: result.from_summary.segment_count,
          totalDurationS: result.from_summary.total_duration_s,
        }
      : undefined,
    toSummary: result.to_summary
      ? {
          speakerId: result.to_summary.speaker_id,
          segmentCount: result.to_summary.segment_count,
          totalDurationS: result.to_summary.total_duration_s,
        }
      : undefined,
  }
}

export interface VoiceSelectionPricingResponse {
  service_mode: string
  credits_per_minute: {
    volcengine: number
    cosyvoice: number
    minimax_turbo: number
    minimax_hd: number
  }
  voice_clone_cost_credits: number
  /** Phase 4 (plan 2026-05-17): admin enabled smart_pause_on_possible_user_voice_match.
   * When true, the Smart submission UI should warn that Smart jobs may pause for
   * human confirmation when a possible (non-strong) personal-voice candidate
   * is found. Defaults to false (no warning) for backward-compat with old
   * gateways that don't return the field. */
  smart_pause_warning_enabled?: boolean
}

export async function getVoiceSelectionPricing(): Promise<VoiceSelectionPricingResponse> {
  const resp = await fetch('/api/voice-selection/pricing', { credentials: 'include' })
  if (!resp.ok) throw new Error(`pricing fetch failed: ${resp.status}`)
  return resp.json()
}
