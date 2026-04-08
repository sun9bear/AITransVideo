import { apiClient } from '@/lib/api/client'
import { getJob } from '@/lib/api/jobs'
import type { ReviewJobTransition } from '@/types/reviews'

export interface VoiceSelectionSpeakerApproval {
  speakerId: string
  voiceId: string
  voiceSource: 'catalog' | 'cloned' | 'auto_matched'
  ttsProvider?: string
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
}

export interface SpeakerAudioResponse {
  speakerId: string
  segments: SpeakerAudioSegment[]
  totalDurationS: number
}

export interface VoicePreviewResult {
  audioBase64: string
  expired: boolean
  error: string | null
}

export async function previewVoice(
  jobId: string,
  voiceId: string,
  options?: { ttsProvider?: string },
): Promise<VoicePreviewResult> {
  const result = await apiClient.post<{
    audio_base64: string
    expired: boolean
    error: string | null
  }>(`/jobs/${jobId}/review/voice/preview`, {
    body: { voice_id: voiceId, tts_provider: options?.ttsProvider ?? '' },
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
  notes: string | null
  createdAt: string | null
}

export async function getUserVoices(): Promise<UserVoiceEntry[]> {
  const resp = await fetch('/gateway/user-voices', { credentials: 'include' })
  if (!resp.ok) return []
  const data = await resp.json()
  return (data.voices ?? []).map((v: Record<string, unknown>) => ({
    id: String(v.id ?? ''),
    voiceId: String(v.voice_id ?? ''),
    voiceType: String(v.voice_type ?? 'cloned'),
    provider: String(v.provider ?? ''),
    ttsProvider: v.tts_provider ? String(v.tts_provider) : null,
    platform: v.platform ? String(v.platform) : null,
    label: String(v.label ?? v.voice_id ?? ''),
    sourceSpeakerId: v.source_speaker_id ? String(v.source_speaker_id) : null,
    notes: v.notes ? String(v.notes) : null,
    createdAt: v.created_at ? String(v.created_at) : null,
  }))
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
    })),
    totalDurationS: result.total_duration_s,
  }
}
