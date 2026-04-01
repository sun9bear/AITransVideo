import { apiClient } from '@/lib/api/client'
import type { ApiWebUiVoiceLibrarySnapshot } from '@/types/api'

export interface VoiceLibraryEntry {
  voiceId: string
  voiceType: string | null
  provider: string | null
  ttsProvider: string | null
  platform: string | null
  label: string | null
  createdAt: string | null
  notes: string | null
  verificationStatus: string | null
  speakerName: string | null
  speakerId: string | null
}

export interface VoiceLibrarySummary {
  speakerCount: number
  voiceCount: number
  builtinVoiceCount: number
  clonedVoiceCount: number
  voices: VoiceLibraryEntry[]
}

export async function getVoiceLibrary(): Promise<VoiceLibrarySummary> {
  const snapshot = await apiClient.get<ApiWebUiVoiceLibrarySnapshot>('/voice-library')
  return toVoiceLibrarySummary(snapshot)
}

function toVoiceLibrarySummary(snapshot: ApiWebUiVoiceLibrarySnapshot): VoiceLibrarySummary {
  const seen = new Set<string>()
  const voices: VoiceLibraryEntry[] = []

  for (const speaker of snapshot.speakers) {
    const speakerId = asString(speaker.speaker_id)
    const speakerName = asString(speaker.speaker_name) ?? asString(speaker.display_name)
    const speakerVoices = asArray(speaker.voices)

    for (const voice of speakerVoices) {
      const voiceId = asString(voice.voice_id)
      if (!voiceId || seen.has(voiceId)) {
        continue
      }
      seen.add(voiceId)
      voices.push({
        voiceId,
        voiceType: asString(voice.voice_type),
        provider: asString(voice.provider),
        ttsProvider: asString(voice.tts_provider),
        platform: asString(voice.platform),
        label: asString(voice.label),
        createdAt: asString(voice.created_at),
        notes: asString(voice.notes),
        verificationStatus: asString(voice.verification_status),
        speakerName,
        speakerId,
      })
    }
  }

  for (const option of snapshot.builtin_voice_options) {
    const voiceId = asString(option.voice_id)
    if (!voiceId || seen.has(voiceId)) {
      continue
    }
    seen.add(voiceId)
    voices.push({
      voiceId,
      voiceType: asString(option.voice_type) ?? 'builtin',
      provider: asString(option.provider),
      ttsProvider: asString(option.tts_provider),
      platform: asString(option.platform),
      label: asString(option.label),
      createdAt: asString(option.created_at),
      notes: asString(option.notes),
      verificationStatus: asString(option.verification_status),
      speakerName: asString(option.speaker_name),
      speakerId: asString(option.speaker_id),
    })
  }

  voices.sort((a, b) => {
    const aIsCloned = a.voiceType === 'cloned' ? 0 : 1
    const bIsCloned = b.voiceType === 'cloned' ? 0 : 1
    if (aIsCloned !== bIsCloned) {
      return aIsCloned - bIsCloned
    }
    const aTime = a.createdAt ? Date.parse(a.createdAt) : 0
    const bTime = b.createdAt ? Date.parse(b.createdAt) : 0
    return bTime - aTime
  })

  const clonedVoiceCount = voices.filter((v) => v.voiceType === 'cloned').length

  return {
    speakerCount: snapshot.speaker_count,
    voiceCount: snapshot.voice_count,
    builtinVoiceCount: snapshot.builtin_voice_count,
    clonedVoiceCount,
    voices,
  }
}

function asString(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) {
    return value.trim()
  }
  return null
}

function asArray(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) {
    return value as Record<string, unknown>[]
  }
  return []
}
