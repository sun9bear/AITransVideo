import { buildBackendUrl, resolveJobApiBaseUrl } from '@/lib/api/config'
import { DOWNLOADABLE_ARTIFACT_KEYS, type DownloadableArtifactKey } from '@/types/jobs'

type BuildResultDownloadUrlInput = {
  downloadKey: DownloadableArtifactKey
  jobId?: string | null
  projectDir?: string | null
}

export function buildResultDownloadUrl(
  input: BuildResultDownloadUrlInput,
) {
  const downloadKey = input.downloadKey
  const jobId = input.jobId?.trim() ?? ''

  if (!jobId) {
    return null
  }

  if (!DOWNLOADABLE_ARTIFACT_KEYS.includes(downloadKey)) {
    return null
  }

  // TTS segments zip has its own endpoint
  if (downloadKey === 'editor.tts_segments_zip') {
    return buildBackendUrl(
      resolveJobApiBaseUrl(),
      `/jobs/${jobId}/tts-segments-zip`,
    )
  }

  return buildBackendUrl(
    resolveJobApiBaseUrl(),
    `/jobs/${jobId}/download/${downloadKey}`,
  )
}

/**
 * Build URL for streaming media (video/audio) — no Content-Disposition: attachment.
 */
export function buildStreamUrl(jobId: string, kind: 'video' | 'audio' | 'poster'): string {
  return buildBackendUrl(
    resolveJobApiBaseUrl(),
    `/jobs/${jobId}/stream/${kind}`,
  )
}

/**
 * Build URL for the single-segment draft TTS audio preview.
 * Served as Range-aware audio/wav; safe to use as an <audio src={...}>.
 * Returns 404 when no draft exists for that segment (plan §7.4).
 */
export function buildDraftAudioUrl(jobId: string, segmentId: string): string {
  return buildBackendUrl(
    resolveJobApiBaseUrl(),
    `/jobs/${jobId}/segments/${segmentId}/draft-audio`,
  )
}

/**
 * Build URL for materials availability check.
 */
export function buildMaterialsAvailabilityUrl(jobId: string): string {
  return buildBackendUrl(
    resolveJobApiBaseUrl(),
    `/jobs/${jobId}/materials-availability`,
  )
}

/**
 * Build URL for materials pack download (Gateway-native endpoint).
 */
export function buildMaterialsPackUrl(jobId: string): string {
  return `/api/jobs/${jobId}/materials-pack`
}

/**
 * Build URL for triggering video generation on a completed job.
 */
export function buildGenerateVideoUrl(jobId: string): string {
  return buildBackendUrl(
    resolveJobApiBaseUrl(),
    `/jobs/${jobId}/generate-video`,
  )
}

export interface MaterialsAvailability {
  source_video: boolean
  dubbed_video: boolean
  dubbed_audio: boolean
  segments: boolean
  subtitles_zh: boolean
  subtitles_en: boolean
  subtitles_bilingual: boolean
}

export async function fetchMaterialsAvailability(jobId: string): Promise<MaterialsAvailability> {
  const url = buildMaterialsAvailabilityUrl(jobId)
  const res = await fetch(url, { credentials: 'include' })
  if (!res.ok) throw new Error('Failed to fetch materials availability')
  return res.json()
}

/**
 * Background task URLs (Gateway-native, async).
 */
export function buildTaskCreateUrl(jobId: string): string {
  return `/api/jobs/${jobId}/tasks`
}

export function buildTaskStatusUrl(jobId: string, taskId: string): string {
  return `/api/jobs/${jobId}/tasks/${taskId}`
}

export function buildTaskDownloadUrl(jobId: string, taskId: string): string {
  return `/api/jobs/${jobId}/tasks/${taskId}/download`
}

/**
 * Compute stable SHA-256 hex fingerprint of a params object.
 *
 * Matches the backend's ``compute_params_fingerprint`` in
 * ``gateway/background_task_queue.py`` — both sides use
 * ``sort_keys + ensure_ascii=False`` equivalent JSON canonicalization.
 *
 * Uses the Web Crypto API; requires a secure context (HTTPS or localhost).
 */
export async function computeParamsFingerprint(
  params: Record<string, unknown>,
): Promise<string> {
  const canonical = canonicalJson(params)
  const bytes = new TextEncoder().encode(canonical)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

/** JSON.stringify with sorted object keys (deterministic). */
function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    return JSON.stringify(value)
  }
  if (Array.isArray(value)) {
    return '[' + value.map((v) => canonicalJson(v)).join(',') + ']'
  }
  const obj = value as Record<string, unknown>
  const keys = Object.keys(obj).sort()
  const parts = keys.map((k) => JSON.stringify(k) + ':' + canonicalJson(obj[k]))
  return '{' + parts.join(',') + '}'
}
