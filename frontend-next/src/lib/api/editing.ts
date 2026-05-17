/**
 * API client for the Studio post-edit endpoints (T1-1..T1-9).
 *
 * Contract mirrors the Job API ``/job-api/jobs/{id}/...`` routes; all
 * requests flow through the Gateway which enforces the feature flag
 * and editing state checks.
 */

import { ApiError, apiClient } from "@/lib/api/client"

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type TtsLengthGuidanceSeverity = "ok" | "mild" | "warning" | "severe" | "unknown"

export interface TtsLengthGuidance {
  current_chars: number
  target_duration_ms: number
  chars_per_second: number | null
  chars_per_second_source: string
  suggested_target_chars: number | null
  suggested_min_chars: number | null
  suggested_max_chars: number | null
  estimated_duration_ms: number | null
  estimated_ratio: number | null
  severity: TtsLengthGuidanceSeverity
}

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
  /** Warning-only pre-synthesis estimate for whether edited text fits
   * the original slot duration for this voice speed. Regenerate remains
   * allowed even when severity is warning/severe; publish DSP-fits the
   * resulting audio if the real duration still differs. */
  tts_length_guidance?: TtsLengthGuidance
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
  tts_model_key?: string
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
  // 2026-04-21 D39: user-initiated cancel mid-run. Carries partial
  // counts in ``result`` (same shape as "completed" summary) plus
  // ``cancelled: true`` so the UI can distinguish "all done" from
  // "stopped early with N segments actually synthesised".
  | "cancelled"

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
  /** D39: set to True by ``cancelRegenerateAll`` — the running worker
   *  observes this on its next per-segment tick and transitions stage
   *  to ``'cancelled'``. Exposed so the UI can optimistically grey out
   *  the cancel button between the click and the status flip. */
  cancel_requested?: boolean
  // When a newer batch has overwritten the status file, old poller
  // sees mismatch=true — stop polling and show a gentle warning.
  mismatch?: boolean
  actual_task_id?: string
}

export type CommitStrategy = "overwrite" | "copy_as_new"

export interface UnsyncedTextSegment {
  segment_id: string
  status?: SegmentStatus | string
  display_name?: string
  speaker_id?: string
  current_cn_text?: string
  audio_cn_text?: string
  current_source_text?: string
  audio_source_text?: string
}

export interface EditingAudioSyncRequiredPayload {
  code: "editing_audio_sync_required"
  message?: string
  error?: string
  unsynced_segments: UnsyncedTextSegment[]
}

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

export async function revertUnsyncedTextSegments(
  jobId: string,
  segmentIds: string[],
): Promise<{
  reverted_segment_ids: string[]
  segments: EditingSegment[]
  segment_status: Record<string, SegmentStatus>
  editing_touched_at?: string | null
}> {
  return apiClient.post(`/jobs/${jobId}/editing/revert-unsynced-text`, {
    body: { segment_ids: segmentIds },
  })
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
    /** 2026-04-21: source_text (English) editable in editing mode.
     *  Backend marks text_dirty and does NOT auto-retranslate — user
     *  is responsible for updating cn_text before the next re-TTS. */
    source_text?: string
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

/**
 * Split one segment into two at the user's chosen character positions.
 * Backend re-shuffles segments.json + marks both new ids text_dirty.
 * Returns the two new segments + the refreshed total count + status map
 * so callers can patch local state in one shot rather than re-fetching.
 */
export async function splitEditingSegment(
  jobId: string,
  segmentId: string,
  body: {
    split_source_index: number
    split_cn_index: number
    speaker_a: string
    speaker_b: string
  },
): Promise<{
  replaced_segment_id: string
  new_segments: EditingSegment[]
  total_count: number
  segment_status: Record<string, SegmentStatus>
}> {
  return apiClient.post(
    `/jobs/${jobId}/segments/${segmentId}/split`,
    { body },
  )
}

/**
 * Phase 2b — word-level timing data for smart split-prefill (plan §5.4).
 * Returns words whose [start, end] falls within the segment's range.
 * `available: false` when the project doesn't have a raw transcript
 * file (degrade to mid-point default).
 */
export interface WordContextWord {
  text: string
  start: number  // ms
  end: number    // ms
  speaker: string | null  // ASR speaker label (A/B/C/...)
}

export interface WordContextResponse {
  segment_id: string
  words: WordContextWord[]
  available: boolean
}

export async function getSegmentWordContext(
  jobId: string,
  segmentId: string,
): Promise<WordContextResponse> {
  return apiClient.get(
    `/jobs/${jobId}/segments/${segmentId}/word-context`,
  )
}

/**
 * Phase 2a — atomic N-cut split (plan 2026-05-17 §5.6).
 * Backed by a write-ahead journal on the server for all-or-nothing
 * recovery across segments / status / voice_map files. cuts must be
 * strictly increasing in both indices; speaker_ids length must equal
 * cuts.length + 1 (one speaker per resulting piece).
 */
export async function splitEditingSegmentMany(
  jobId: string,
  segmentId: string,
  body: {
    cuts: Array<{ source_index: number; cn_index: number }>
    speaker_ids: string[]
  },
): Promise<{
  replaced_segment_id: string
  new_segments: EditingSegment[]
  total_count: number
  segment_status: Record<string, SegmentStatus>
}> {
  return apiClient.post(
    `/jobs/${jobId}/segments/${segmentId}/split-many`,
    { body },
  )
}

/**
 * Base64-encoded WAV slice of the source audio for one editing segment.
 * Response is small enough (10-30 KB per 1-5s of mono 16k audio) to inline
 * into a ``data:audio/wav;base64,...`` URL on the browser side.
 */
/**
 * Prepare the source-audio preview cache and return metadata (no bytes).
 * The WAV lives at ``{project_dir}/editor/editing/preview_cache/{sid}.wav``
 * and is served via GET /segments/{sid}/preview-source-audio.
 *
 * 2026-04-21 redesign: the old flow returned 1.3 MB base64 JSON, which
 * tripped ``RemoteProtocolError`` on the gateway Uvicorn ↔ httpx proxy
 * for long segments (30+ seconds). Stream URL + ``<audio src>`` lets
 * the browser do Range-aware fetching natively, bypassing the JSON
 * body pathology entirely.
 */
export async function previewEditingSegmentSource(
  jobId: string,
  segmentId: string,
): Promise<{
  segment_id: string
  mime_type: string
  start_ms: number
  end_ms: number
  duration_ms: number
  size_bytes: number
}> {
  return apiClient.post(
    `/jobs/${jobId}/segments/${segmentId}/preview-source`,
    { body: {} },
  )
}

/**
 * Build the ``<audio src>`` URL for a segment's prepared preview cache.
 * The ``_ts`` query param cache-busts the browser between different
 * POST invocations — editing timestamps or source changes produce a
 * new cache file that the browser must refetch.
 */
export function buildPreviewSourceStreamUrl(
  jobId: string,
  segmentId: string,
  nonce: string | number = Date.now(),
): string {
  return `/job-api/jobs/${jobId}/segments/${segmentId}/preview-source-audio?_ts=${nonce}`
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

/**
 * D39 plan §7.10: signal the running batch worker to stop between
 * segments. Server responds ``{cancelled: bool}`` — True = flag was
 * written and worker will land on ``stage='cancelled'`` on its next
 * tick; False = wrong task_id / already terminal.
 *
 * Idempotent; safe to call multiple times.
 */
export async function cancelRegenerateAll(
  jobId: string,
  taskId: string,
): Promise<{ success: boolean; cancelled: boolean }> {
  const encoded = encodeURIComponent(taskId)
  return apiClient.post(
    `/jobs/${jobId}/regenerate-all-tts/cancel?task_id=${encoded}`,
    { body: {} },
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
  ttsModelKey?: string,
  voiceReuse = false,
): Promise<{ segment_id: string; provider: string; voice_id: string; tts_model_key?: string }> {
  const body: Record<string, string | boolean> = {
    segment_id: segmentId,
    provider,
    voice_id: voiceId,
  }
  if (ttsModelKey) body.tts_model_key = ttsModelKey
  if (voiceReuse) body.voice_reuse = true
  return apiClient.post(
    `/jobs/${jobId}/editing/voice-map`,
    { body },
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

// ---------------------------------------------------------------------------
// Editing-mode speakers (Task 6 — plan 2026-05-09-studio-editing-add-speaker)
// ---------------------------------------------------------------------------

/**
 * Merged baseline + editing speaker view returned by
 * ``GET /jobs/{id}/editing/speakers``. Baseline rows have ``source =
 * "baseline"`` and may omit ``color`` / ``created_at`` / ``voice_profile``
 * since pre-Task-3 tasks never wrote those fields. Editing rows always
 * carry ``profile_status`` and any inferred ``voice_profile`` once the
 * background retry pipeline finishes (Task 4 / Task 5).
 */
export interface EditingSpeaker {
  speaker_id: string
  display_name: string
  color?: string | null
  source: "baseline" | "editing"
  created_at?: string
  profile_status: "pending_segments" | "inferring" | "ready" | "failed"
  profile_error?: string | null
  voice_profile?: Record<string, unknown> | null
}

/**
 * Thrown when ``createEditingSpeaker`` hits a 409 with body
 * ``{ code: "display_name_conflict" }``. Dialog catches this to show the
 * "名字已被使用" inline error without surfacing the raw ApiError message.
 */
export class DisplayNameConflict extends Error {
  constructor() {
    super("display_name_conflict")
    this.name = "DisplayNameConflict"
  }
}

export async function listEditingSpeakers(
  jobId: string,
): Promise<EditingSpeaker[]> {
  const body = await apiClient.get<{ speakers: EditingSpeaker[] }>(
    `/jobs/${jobId}/editing/speakers`,
  )
  return body.speakers
}

export async function createEditingSpeaker(
  jobId: string,
  displayName: string,
): Promise<EditingSpeaker> {
  try {
    return await apiClient.post<EditingSpeaker>(
      `/jobs/${jobId}/editing/speakers`,
      { body: { display_name: displayName } },
    )
  } catch (e: unknown) {
    // Backend raises JobConflictError → 409 with payload
    // ``{ code: "display_name_conflict", message: "..." }``. ApiError
    // exposes ``status: number`` + ``payload: unknown`` (see
    // lib/api/client.ts).
    if (e instanceof ApiError && e.status === 409) {
      const payload = e.payload as { code?: string } | null
      if (payload?.code === "display_name_conflict") {
        throw new DisplayNameConflict()
      }
    }
    throw e
  }
}

export interface RetryProfileResponse {
  speaker_id: string
  /** ``pending_segments`` = real retry kicked off; ``unknown`` = speaker_id
   *  not in editing/baseline (200, no-op) — Task 5 contract. */
  status: "pending_segments" | "unknown"
  scheduled: boolean
}

export async function retryEditingSpeakerProfile(
  jobId: string,
  speakerId: string,
): Promise<RetryProfileResponse> {
  return apiClient.post<RetryProfileResponse>(
    `/jobs/${jobId}/editing/speakers/${speakerId}/retry-profile`,
    { body: {} },
  )
}
