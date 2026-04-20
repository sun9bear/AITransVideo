/**
 * API client for the Studio post-edit endpoints (T1-1..T1-9).
 *
 * Contract mirrors the Job API ``/job-api/jobs/{id}/...`` routes; all
 * requests flow through the Gateway which enforces the feature flag
 * and editing state checks.
 */

import { apiClient } from "@/lib/api/client"

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface EditingSegment {
  segment_id: string
  speaker_id?: string
  cn_text?: string
  source_text?: string
  start_ms?: number
  end_ms?: number
  voice_id?: string
  /** Canonical TTS provider key from DubbingSegment (new data). */
  tts_provider?: string
  /** Legacy drift key — older editor/segments.json wrote ``provider``
   * instead of ``tts_provider``. Ultrareview #2 fixed new writes but
   * existing task snapshots still carry the old field; consumers
   * should read ``tts_provider ?? provider``. */
  provider?: string
  alignment_method?: string
  /** UI hint extracted from manifest; may be absent for pre-migration segs. */
  duration_target_ms?: number
  duration_actual_ms?: number
  duration_diff_ratio?: number
  /** Slot duration from the last pipeline run. Frontend compares this
   * against ``draft_wav_duration_ms`` for D44 slot-mismatch warnings. */
  target_duration_ms?: number
  /** Actual duration of ``editor/editing/tts_segments_draft/{sid}.wav``
   * when present. Absent means no draft on disk (commit uses the
   * baseline wav which already matches target). γ DSP stretch will
   * time-warp a draft to match target regardless of how far off, but
   * extreme ratios (>1.5x / <0.67x) degrade audio quality — warn the
   * user at edit time so they can shorten the text before committing. */
  draft_wav_duration_ms?: number
  /** Passthrough for any unknown pipeline-maintained fields. */
  [key: string]: unknown
}

export type SegmentStatus =
  | "accepted"
  | "text_dirty"
  | "tts_loading"
  | "tts_dirty"
  | "tts_failed"
  | "voice_dirty"

export interface EditingSegmentsResponse {
  segments: EditingSegment[]
  segment_status: Record<string, SegmentStatus>
  total: number
  editing_touched_at: string | null
  edit_generation: number
}

export interface VoiceMapEntry {
  provider: string
  voice_id: string
}

export interface VoiceMapResponse {
  voice_map: Record<string, VoiceMapEntry>
}

export interface BatchRegenerateResponse {
  total: number
  succeeded_count: number
  failed_count: number
  succeeded_segment_ids: string[]
  failed_segment_ids: string[]
  failures: Array<{ segment_id: string; error: string }>
}

// D39 async batch re-TTS contract. POST returns immediately with a
// task_id; caller polls GET /status until stage is terminal.
export interface BatchRegenerateStartResponse {
  task_id: string
  status: "running"
}

export type BatchRegenerateStage =
  | "starting"
  | "running"
  | "completed"
  | "failed"

export interface BatchRegenerateStatus {
  task_id: string
  stage: BatchRegenerateStage
  total: number
  succeeded_count: number
  failed_count: number
  succeeded_segment_ids: string[]
  failed_segment_ids: string[]
  failures: Array<{ segment_id: string; error: string }>
  current_segment_id: string | null
  result: BatchRegenerateResponse | null
  error: string | null
  updated_at: string
  // When a newer batch has overwritten the status file, old poller
  // sees mismatch=true — stop polling and show a gentle warning.
  mismatch?: boolean
  actual_task_id?: string
}

export type CommitStrategy = "overwrite" | "copy_as_new"

export interface CommitOverwriteResponse {
  strategy: "overwrite"
  job_id: string
  edit_generation: number
  applied_draft_segment_ids: string[]
  segments_count: number
  voice_overrides_count?: number
}

export interface CommitCopyResponse {
  strategy: "copy_as_new"
  source_job_id: string
  new_job_id: string
  new_project_dir: string
  new_display_name: string
}

export type CommitResponse = CommitOverwriteResponse | CommitCopyResponse

// ---------------------------------------------------------------------------
// Calls
// ---------------------------------------------------------------------------

export async function enterEditing(jobId: string): Promise<void> {
  await apiClient.post(`/jobs/${jobId}/enter-edit`, { body: {} })
}

export async function cancelEditing(
  jobId: string,
  options: { reason?: string } = {},
): Promise<void> {
  await apiClient.post(`/jobs/${jobId}/editing/cancel`, {
    body: { reason: options.reason ?? "user_cancel" },
  })
}

export async function commitEditing(
  jobId: string,
  strategy: CommitStrategy,
  options: { copy_display_name?: string } = {},
): Promise<CommitResponse> {
  const body: Record<string, unknown> = { strategy }
  if (strategy === "copy_as_new" && options.copy_display_name) {
    body.copy_display_name = options.copy_display_name
  }
  return apiClient.post<CommitResponse>(`/jobs/${jobId}/editing/commit`, { body })
}

export async function getEditingSegments(
  jobId: string,
): Promise<EditingSegmentsResponse> {
  return apiClient.get<EditingSegmentsResponse>(
    `/jobs/${jobId}/editing/segments`,
  )
}

export async function patchSegmentText(
  jobId: string,
  segmentId: string,
  patch: {
    cn_text?: string
    translation_confirmed?: boolean
    rewrite_requested?: boolean
    /** 2026-04-20: speaker reassignment. Backend propagates voice_id +
     *  tts_provider from the new speaker's baseline + clears any
     *  stale voice_map override + flags voice_dirty. The response
     *  mirrors the normal PATCH shape — treat exactly like cn_text. */
    speaker_id?: string
  },
): Promise<{ segment: EditingSegment; segment_status: Record<string, SegmentStatus> }> {
  return apiClient.post(
    `/jobs/${jobId}/segments/${segmentId}/update`,
    { body: patch },
  )
}

export async function markSegmentStatus(
  jobId: string,
  segmentId: string,
  status: SegmentStatus,
): Promise<{ segment_status: Record<string, SegmentStatus> }> {
  return apiClient.post(
    `/jobs/${jobId}/segments/${segmentId}/status`,
    { body: { status } },
  )
}

export async function regenerateSegmentTts(
  jobId: string,
  segmentId: string,
): Promise<{
  segment_id: string
  draft_audio_path: string
  size_bytes: number
  segment_status: Record<string, SegmentStatus>
}> {
  return apiClient.post(
    `/jobs/${jobId}/segments/${segmentId}/regenerate-tts`,
    { body: {} },
  )
}

export async function acceptSegmentDraft(
  jobId: string,
  segmentId: string,
): Promise<{ segment_id: string; action: "accepted"; segment_status: Record<string, SegmentStatus> }> {
  return apiClient.post(
    `/jobs/${jobId}/segments/${segmentId}/accept-draft`,
    { body: {} },
  )
}

export async function discardSegmentDraft(
  jobId: string,
  segmentId: string,
): Promise<{ segment_id: string; action: "discarded"; segment_status: Record<string, SegmentStatus> }> {
  return apiClient.post(
    `/jobs/${jobId}/segments/${segmentId}/discard-draft`,
    { body: {} },
  )
}

export async function regenerateAllDirtyTts(
  jobId: string,
): Promise<BatchRegenerateStartResponse> {
  // D39 async: returns immediately with a task_id. Caller polls via
  // ``getRegenerateAllStatus`` until stage is completed / failed.
  return apiClient.post<BatchRegenerateStartResponse>(
    `/jobs/${jobId}/regenerate-all-tts`,
    { body: {} },
  )
}

export async function getRegenerateAllStatus(
  jobId: string,
  taskId: string,
): Promise<BatchRegenerateStatus> {
  const encoded = encodeURIComponent(taskId)
  return apiClient.get<BatchRegenerateStatus>(
    `/jobs/${jobId}/regenerate-all-tts/status?task_id=${encoded}`,
  )
}

export async function getVoiceMap(jobId: string): Promise<VoiceMapResponse> {
  return apiClient.get<VoiceMapResponse>(`/jobs/${jobId}/editing/voice-map`)
}

export async function setVoiceOverride(
  jobId: string,
  segmentId: string,
  provider: string,
  voiceId: string,
): Promise<{ segment_id: string; provider: string; voice_id: string }> {
  return apiClient.post(
    `/jobs/${jobId}/editing/voice-map`,
    { body: { segment_id: segmentId, provider, voice_id: voiceId } },
  )
}

export async function clearVoiceOverride(
  jobId: string,
  segmentId: string,
): Promise<{ segment_id: string; cleared: boolean }> {
  return apiClient.post(
    `/jobs/${jobId}/editing/voice-map`,
    { body: { segment_id: segmentId, action: "clear" } },
  )
}
