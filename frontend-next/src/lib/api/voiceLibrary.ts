// apiClient not needed — voice library now uses /gateway/user-voices directly.

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

interface UserVoicesResponse {
  voices?: Array<Record<string, unknown>>
}

interface CalibrateSpeedResponse {
  ok: boolean
  voice?: Record<string, unknown>
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
  // Single source of truth: /gateway/user-voices returns the user's
  // personal voice library with label, cps, and all metadata directly
  // from the user_voices table. No longer merging with the legacy
  // /voice-library endpoint — that was causing label edits to revert
  // on refresh (PATCH writes user_voices but /voice-library reads from
  // a different data source).
  const resp = await fetch('/gateway/user-voices', { credentials: 'include' })
  if (!resp.ok) throw new Error(`fetch user-voices failed: ${resp.status}`)
  const data = (await resp.json()) as { voices?: Array<Record<string, unknown>> }
  return toVoiceLibrarySummaryFromGateway(data?.voices ?? [])
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
/**
 * Update the display label of a user voice.
 */
export async function updateVoiceLabel(voiceId: string, label: string): Promise<{ ok: boolean; voice?: Record<string, unknown> }> {
  const resp = await fetch(
    `/gateway/user-voices/${encodeURIComponent(voiceId)}`,
    {
      method: 'PATCH',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label }),
    },
  )
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data?.message || data?.error || `PATCH failed (${resp.status})`)
  return data
}

/**
 * Add a new voice to the user's personal library.
 */
export async function addUserVoice(params: {
  voice_id: string
  label: string
  tts_provider?: string
  platform?: string
}): Promise<{ ok: boolean; voice?: Record<string, unknown> }> {
  const resp = await fetch('/gateway/user-voices', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data?.message || data?.error || `POST failed (${resp.status})`)
  return data
}

export interface ProbeResult {
  ok: boolean
  audio_base64?: string
  audio_format?: string
  text?: string
  voice_id?: string
  error?: string
  message?: string
}

/**
 * Probe a voice_id — synthesize a short sample to verify usability
 * and let the user hear how it sounds. Cost: ~CNY 0.006 per call.
 */
export async function probeVoice(voiceId: string, label?: string, ttsProvider?: string): Promise<ProbeResult> {
  const resp = await fetch('/gateway/user-voices/probe', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      voice_id: voiceId,
      label: label || undefined,
      tts_provider: ttsProvider || undefined,
    }),
  })
  const data = (await resp.json().catch(() => ({}))) as ProbeResult
  if (!resp.ok || !data?.ok) {
    throw new Error(data?.message || data?.error || `probe failed (${resp.status})`)
  }
  return data
}

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

function toVoiceLibrarySummaryFromGateway(
  voices: Array<Record<string, unknown>>,
): VoiceLibrarySummary {
  const entries: VoiceLibraryEntry[] = []
  for (const v of voices) {
    const voiceId = asString(v.voice_id)
    if (!voiceId) continue
    entries.push({
      voiceId,
      voiceType: asString(v.voice_type),
      provider: asString(v.provider),
      ttsProvider: asString(v.tts_provider),
      platform: asString(v.platform),
      label: asString(v.label),
      createdAt: asString(v.created_at),
      notes: asString(v.notes),
      verificationStatus: null,
      speakerName: asString(v.source_speaker_id),
      speakerId: asString(v.source_speaker_id),
      charsPerSecond: typeof v.chars_per_second === 'number' ? v.chars_per_second : null,
      speedCalibratedAt: asString(v.speed_calibrated_at),
    })
  }
  // Sort: newest first
  entries.sort((a, b) => {
    const at = a.createdAt ? Date.parse(a.createdAt) : 0
    const bt = b.createdAt ? Date.parse(b.createdAt) : 0
    return bt - at
  })
  const clonedCount = entries.filter((e) => e.voiceType === 'cloned').length
  return {
    speakerCount: 0,
    voiceCount: entries.length,
    builtinVoiceCount: entries.length - clonedCount,
    clonedVoiceCount: clonedCount,
    voices: entries,
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
