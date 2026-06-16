/**
 * Voice catalog admin API client.
 *
 * Uses raw fetch('/api/admin/voices...') instead of the shared job-api client
 * because that client's baseUrl is '/job-api' (for the Job API proxy).
 * Admin endpoints go directly to the Gateway at '/api/admin/...'.
 * This matches the pattern used by admin/settings/page.tsx.
 */
import type {
  VoiceCatalogListResponse,
  VoiceCatalogDetailResponse,
  CreateVoiceRequest,
  UpdateVoiceRequest,
  ImportRequest,
  ImportPreviewResponse,
  ImportResultResponse,
  VerifyResponse,
  BatchVerifyResponse,
} from '@/types/voiceCatalog'

export interface VoiceCatalogListParams {
  provider?: string
  resourceId?: string
  gender?: string
  verified?: boolean
  matchable?: boolean
  labelFilter?: string
  archived?: boolean
  page?: number
  pageSize?: number
}

/** Error carrying the HTTP status so pages can branch on 401/403 (权限守卫). */
export class AdminApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'AdminApiError'
    this.status = status
  }
}

async function adminFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, { credentials: 'include', ...init })
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({ error: '请求失败' }))
    throw new AdminApiError(resp.status, body.error || body.detail || `请求失败 (${resp.status})`)
  }
  return resp.json() as Promise<T>
}

function adminPost<T>(path: string, body: unknown): Promise<T> {
  return adminFetch<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

function adminPatch<T>(path: string, body: unknown): Promise<T> {
  return adminFetch<T>(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

function adminDelete<T>(path: string): Promise<T> {
  return adminFetch<T>(path, { method: 'DELETE' })
}

export async function listVoices(
  params: VoiceCatalogListParams = {},
): Promise<VoiceCatalogListResponse> {
  const query = new URLSearchParams()
  if (params.provider) query.set('provider', params.provider)
  if (params.resourceId) query.set('resource_id', params.resourceId)
  if (params.gender) query.set('gender', params.gender)
  if (params.verified !== undefined) query.set('verified', String(params.verified))
  if (params.matchable !== undefined) query.set('matchable', String(params.matchable))
  if (params.labelFilter) query.set('label_filter', params.labelFilter)
  if (params.archived) query.set('archived', 'true')
  if (params.page) query.set('page', String(params.page))
  if (params.pageSize) query.set('page_size', String(params.pageSize))

  const qs = query.toString()
  return adminFetch<VoiceCatalogListResponse>(
    `/api/admin/voices${qs ? `?${qs}` : ''}`,
  )
}

export async function getVoiceDetail(
  voiceId: string,
): Promise<VoiceCatalogDetailResponse> {
  return adminFetch<VoiceCatalogDetailResponse>(
    `/api/admin/voices/${encodeURIComponent(voiceId)}`,
  )
}

// Phase 2: write operations

export async function createVoice(
  req: CreateVoiceRequest,
): Promise<{ voice: import('@/types/voiceCatalog').VoiceCatalogItem }> {
  return adminPost('/api/admin/voices', req)
}

export async function updateVoice(
  voiceId: string,
  req: UpdateVoiceRequest,
): Promise<{ voice: import('@/types/voiceCatalog').VoiceCatalogItem }> {
  return adminPatch(`/api/admin/voices/${encodeURIComponent(voiceId)}`, req)
}

export async function deleteVoice(
  voiceId: string,
): Promise<{ voice_id: string; archived: boolean }> {
  return adminDelete(`/api/admin/voices/${encodeURIComponent(voiceId)}`)
}

export async function verifyVoice(
  voiceId: string,
): Promise<VerifyResponse> {
  return adminPost(`/api/admin/voices/${encodeURIComponent(voiceId)}/verify`, {})
}

export async function verifyBatch(
  voiceIds: string[],
): Promise<BatchVerifyResponse> {
  return adminPost('/api/admin/voices/verify-batch', { voice_ids: voiceIds })
}

export async function importVoices(
  req: ImportRequest,
): Promise<ImportPreviewResponse | ImportResultResponse> {
  return adminPost('/api/admin/voices/import', req)
}

// Phase 4: labeling

export async function finalizeLabel(
  voiceId: string,
): Promise<{ voice_id: string; label_type: string; merged: Record<string, unknown>; ok: boolean }> {
  return adminPost(`/api/admin/voices/${encodeURIComponent(voiceId)}/label/finalize`, {})
}

export async function batchFinalizeLabels(
  voiceIds: string[],
): Promise<{ succeeded: string[]; failed: Array<{ voice_id: string; error: string }> }> {
  return adminPost('/api/admin/voices/label/batch-finalize', { voice_ids: voiceIds })
}

export async function getLabelStatus(): Promise<{
  total_voices: number
  label_counts: Record<string, number>
  coverage: Record<string, string>
}> {
  return adminFetch('/api/admin/voices/label/status')
}

export async function triggerTextLabeling(
  voiceIds: string[],
): Promise<{ written: string[]; errors: Array<{ voice_id: string; error: string }>; source_run_id: string | null }> {
  return adminPost('/api/admin/voices/label/trigger-text', { voice_ids: voiceIds })
}

export async function triggerAudioLabeling(
  voiceIds: string[],
  roundName: string,
): Promise<{ written: string[]; errors: Array<{ voice_id: string; error: string }>; source_run_id: string | null }> {
  return adminPost(`/api/admin/voices/label/trigger-audio/${roundName}`, { voice_ids: voiceIds })
}

// Async task API

export async function submitLabelTask(
  voiceIds: string[],
  taskType: 'trigger-text' | 'trigger-audio',
  roundName?: string,
): Promise<{ task_id: string; status: string; total: number }> {
  return adminPost('/api/admin/voices/label/tasks', {
    voice_ids: voiceIds,
    task_type: taskType,
    round_name: roundName,
  })
}

export async function pollLabelTask(taskId: string): Promise<{
  task_id: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  progress: { completed: number; total: number; current_batch: number }
  result: { written: string[]; errors: Array<Record<string, string>> } | null
  error: string | null
}> {
  return adminFetch(`/api/admin/voices/label/tasks/${taskId}`)
}
