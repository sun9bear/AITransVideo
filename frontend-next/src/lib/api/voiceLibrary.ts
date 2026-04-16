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
  // Calibration result merged from GET /gateway/user-voices (the legacy
  // /voice-library endpoint doesn't carry it). null until the user presses
  // "测试语速".
  charsPerSecond: number | null
  speedCalibratedAt: string | null
}

export interface VoiceLibrarySummary {
  speakerCount: number
  voiceCount: number
  builtinVoiceCount: number
  clonedVoiceCount: number
  voices: VoiceLibraryEntry[]
}

interface UserVoiceCalibrationEntry {
  voice_id: string
  chars_per_second: number | null
  speed_calibrated_at: string | null
}

interface UserVoicesResponse {
  voices?: UserVoiceCalibrationEntry[]
}

interface CalibrateSpeedResponse {
  ok: boolean
  voice?: UserVoiceCalibrationEntry
  calibration?: {
    cps: number
    total_hanzi: number
    total_duration_ms: number
    provider: string
    model: string
    per_text: Array<{ name: string; hanzi: number; duration_ms: number; cps: number }>
  }
  error?: string
  message?: string
}

export async function getVoiceLibrary(): Promise<VoiceLibrarySummary> {
  // The legacy /voice-library endpoint is the source of truth for which
  // voices the user has and how they map to speakers. /gateway/user-voices
  // is the only place that carries the chars_per_second calibration. Fetch
  // both and merge — the user-voices call is best-effort: if it fails we
  // still render the library, just without cps badges.
  const [snapshot, userVoiceCalibrations] = await Promise.all([
    apiClient.get<ApiWebUiVoiceLibrarySnapshot>('/voice-library'),
    fetchUserVoiceCalibrations().catch(() => new Map<string, { cps: number | null; calibratedAt: string | null }>()),
  ])
  return toVoiceLibrarySummary(snapshot, userVoiceCalibrations)
}

async function fetchUserVoiceCalibrations(): Promise<
  Map<string, { cps: number | null; calibratedAt: string | null }>
> {
  // /gateway/user-voices is gateway-native (not job-api proxy). Same
  // raw-fetch pattern as frontend-next/src/lib/api/voiceSelection.ts:111.
  const resp = await fetch('/gateway/user-voices', { credentials: 'include' })
  if (!resp.ok) {
    throw new Error(`fetch user-voices failed: ${resp.status}`)
  }
  const data = (await resp.json()) as UserVoicesResponse
  const out = new Map<string, { cps: number | null; calibratedAt: string | null }>()
  for (const v of data?.voices ?? []) {
    if (!v?.voice_id) continue
    out.set(v.voice_id, {
      cps: typeof v.chars_per_second === 'number' ? v.chars_per_second : null,
      calibratedAt: v.speed_calibrated_at ?? null,
    })
  }
  return out
}

/**
 * Trigger speed calibration for a cloned voice.
 *
 * Cost: ~CNY 0.06 (3 standard texts × ~120 billed chars × CNY 2/万 turbo).
 * Cost is paid by the user explicitly clicking the "测试语速" button —
 * complies with CLAUDE.md "付费 API 不能自动调用" since this is
 * user-explicitly-triggered.
 *
 * Throws an Error with the gateway-provided message when the API
 * returns a non-2xx status (so the page UI can surface it to the user).
 */
export async function calibrateVoiceSpeed(voiceId: string): Promise<CalibrateSpeedResponse> {
  const resp = await fetch(
    `/gateway/user-voices/${encodeURIComponent(voiceId)}/calibrate-speed`,
    {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    },
  )
  const data = (await resp.json().catch(() => ({}))) as CalibrateSpeedResponse
  if (!resp.ok || !data?.ok) {
    const msg = data?.message || data?.error || `calibrate-speed failed (${resp.status})`
    throw new Error(msg)
  }
  return data
}

function toVoiceLibrarySummary(
  snapshot: ApiWebUiVoiceLibrarySnapshot,
  calibrations: Map<string, { cps: number | null; calibratedAt: string | null }>,
): VoiceLibrarySummary {
  const seen = new Set<string>()
  const voices: VoiceLibraryEntry[] = []

  const cpsFor = (vid: string) => calibrations.get(vid) ?? { cps: null, calibratedAt: null }

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
      const cal = cpsFor(voiceId)
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
        charsPerSecond: cal.cps,
        speedCalibratedAt: cal.calibratedAt,
      })
    }
  }

  for (const option of snapshot.builtin_voice_options) {
    const voiceId = asString(option.voice_id)
    if (!voiceId || seen.has(voiceId)) {
      continue
    }
    seen.add(voiceId)
    const cal = cpsFor(voiceId)
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
      charsPerSecond: cal.cps,
      speedCalibratedAt: cal.calibratedAt,
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
