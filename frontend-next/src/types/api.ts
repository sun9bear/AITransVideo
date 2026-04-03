export type ApiJobStatus =
  | 'cancelled'
  | 'failed'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'waiting_for_review'

export type ApiJobEventType = 'log' | 'status'

export type ApiJobEventLevel = 'error' | 'info' | 'warn'

export interface ApiJobRecord {
  job_id: string
  job_type: string
  source_type: string
  source_ref: string
  output_target: string
  speakers: string
  voice_a: string | null
  voice_b: string | null
  status: ApiJobStatus
  current_stage: string | null
  progress_message: string | null
  created_at: string
  updated_at: string
  started_at: string | null
  completed_at: string | null
  project_dir: string | null
  manifest_path: string | null
  review_gate: Record<string, unknown> | null
  error_summary: Record<string, unknown> | null
  fallback_summary: Record<string, unknown> | null
}

export interface ApiJobListResponse {
  jobs: ApiJobRecord[]
}

export interface ApiJobEvent {
  job_id: string
  event_type: ApiJobEventType
  created_at: string
  message: string | null
  stage: string | null
  status: ApiJobStatus | null
  level: ApiJobEventLevel
  payload: Record<string, unknown>
}

export interface ApiJobLogsResponse {
  job_id: string
  events: ApiJobEvent[]
  lines: string[]
}

export interface ApiManifestSummary {
  available: boolean
  artifact_count: number
}

export interface ApiResultOutput {
  name: string
  key: string
  category: string | null
  path?: string | null
  exists: boolean
}

export interface ApiArtifactCategoryBucket {
  name: string
  count: number
  existing_count: number
}

export interface ApiArtifactSummary {
  total_count: number
  existing_count: number
  categories: ApiArtifactCategoryBucket[]
}

export interface ApiJobResultSummary {
  job_id: string
  status: ApiJobStatus
  manifest: ApiManifestSummary
  outputs: ApiResultOutput[]
  artifacts: ApiArtifactSummary
  review_gate: Record<string, unknown> | null
  error_summary: Record<string, unknown> | null
  fallback_summary: Record<string, unknown> | null
  project_dir: string | null
  manifest_path: string | null
}

export interface ApiArtifactEntry {
  key: string
  category: string
  declared_path: string
  path: string
  exists: boolean
}

export interface ApiJobArtifactsResponse {
  job_id: string
  status: ApiJobStatus
  manifest: ApiManifestSummary
  artifacts: ApiArtifactEntry[]
  review_gate: Record<string, unknown> | null
  error_summary: Record<string, unknown> | null
  fallback_summary: Record<string, unknown> | null
  project_dir: string | null
  manifest_path: string | null
}

export interface ApiWebUiReviewStageSnapshot {
  stage: string
  tab: string
  status: string
  updated_at: string | null
  approved_at: string | null
  payload: Record<string, unknown>
}

export interface ApiWebUiReviewFlowSnapshot {
  path: string | null
  load_error: string | null
  active_stage: string | null
  active_review: ApiWebUiReviewStageSnapshot | null
  stages: Record<string, ApiWebUiReviewStageSnapshot>
}

export interface ApiWebUiSpeakerOption {
  speaker_id?: string
  value?: string
  display_name?: string
  label?: string
}

export interface ApiWebUiReviewItem {
  segment_id: number | string
  speaker_id: string
  display_name: string
  source_text: string
  cn_text: string
  tts_cn_text: string
  speaker_confirmed: boolean
  transcript_confirmed: boolean
  translation_confirmed: boolean
  rewrite_requested: boolean
  review_updated_at: string
  start_ms: number
  end_ms: number
  actual_duration_ms: number
  target_duration_ms: number
  needs_review: boolean
  rewrite_count: number
}

export interface ApiWebUiReviewSection {
  total_items: number
  default_page_size?: number
  page_size_options?: number[]
  speaker_options: Array<{
    value?: string
    label?: string
  }>
  items: ApiWebUiReviewItem[]
}

export interface ApiWebUiVoiceLibraryVoice {
  voice_id: string | null
  voice_type: string | null
  provider: string | null
  tts_provider: string | null
  platform: string | null
  label: string | null
  created_at: string | null
  source_audio_path: string | null
  notes: string | null
  verification_status: string | null
  last_verified_at: string | null
  last_verification_success: boolean | null
  last_verification_audio_path: string | null
  last_verification_error: string | null
}

export interface ApiWebUiVoiceReviewSpeakerSnapshot {
  speaker_id: string
  speaker_label: string | null
  speaker_name: string | null
  voice_arg_name: string | null
  sample_path: string | null
  sample_duration_s: number
  silence_ratio: number
  default_voice_id: string | null
  default_voice_type: string | null
  resolved_status: string | null
  resolved_source: string | null
  resolved_voice_id: string | null
  resolved_voice_type: string | null
  resolved_label: string | null
  available_voices: ApiWebUiVoiceLibraryVoice[]
}

export interface ApiWebUiVoiceReviewSnapshot {
  stage: string
  status: string
  message: string | null
  reason: string | null
  speakers: ApiWebUiVoiceReviewSpeakerSnapshot[]
  volcengine_2_0_voices?: Array<{
    voice_id: string
    display_name: string
    gender: string
    age_group: string
    persona_style: string
  }>
}

export interface ApiWebUiVoiceLibrarySnapshot {
  path: string | null
  exists: boolean
  load_error: string | null
  speaker_count: number
  voice_count: number
  builtin_voice_count: number
  project_default_builtin_voice: Record<string, unknown> | null
  builtin_voice_options: Record<string, unknown>[]
  active_review: ApiWebUiVoiceReviewSnapshot | null
  current_project_speakers: Record<string, unknown>[]
  speakers: Record<string, unknown>[]
}

export interface ApiWebUiResultsSnapshot {
  available: boolean
  project_dir: string | null
  transcript_review: ApiWebUiReviewSection
  translation_review: ApiWebUiReviewSection
  review_flow: ApiWebUiReviewFlowSnapshot
  voice_library: ApiWebUiVoiceLibrarySnapshot
}

export interface ApiWebUiStateResponse {
  meta: Record<string, unknown>
  settings: Record<string, unknown>
  job: ApiJobRecord
  results: ApiWebUiResultsSnapshot
}
