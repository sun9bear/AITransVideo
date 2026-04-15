export interface VoiceCatalogLabelStatus {
  text: boolean
  audio_round1: boolean
  audio_round2: boolean
  audio_round3: boolean
  final: boolean
}

export interface VoiceCatalogVerifyDimension {
  verified: boolean
  at: string | null
  error: string | null
}

export interface VoiceCatalogFinalLabel {
  age_group: string | null
  persona_style: string | null
  energy_level: string | null
  pitch_level: string | null
  warmth: string | null
  maturity: string | null
  delivery_style: string | null
  texture_tags: string[] | null
  childlike: boolean | null
}

export interface VoiceCatalogItem {
  voice_id: string
  provider: string
  provider_config: Record<string, unknown>
  display_name: string
  gender: string | null
  language: string
  scene: string | null
  matchable: boolean
  verify_status: Record<string, VoiceCatalogVerifyDimension>
  is_verified: boolean
  is_seed: boolean
  verify_attempts: number
  source: string
  archived_at: string | null
  notes: string | null
  created_at: string | null
  updated_at: string | null
  label_status: VoiceCatalogLabelStatus
  final_label: VoiceCatalogFinalLabel | null
  // Voice speed calibration (migration 012, 2026-04-14).
  // `chars_per_second` is the scalar "best guess" across all calibrated models;
  // `chars_per_second_by_model` keeps per-model values, e.g.
  //   { "speech-2.8-turbo": 4.13, "speech-2.8-hd": 4.05 }
  // Both are null until offline calibration has been run.
  chars_per_second?: number | null
  chars_per_second_by_model?: Record<string, number> | null
  speed_calibrated_at?: string | null
}

export interface VoiceCatalogLabel {
  id: number
  voice_id: string
  label_type: string
  source_run_id: string | null
  is_current: boolean
  age_group: string | null
  persona_style: string | null
  energy_level: string | null
  pitch_level: string | null
  warmth: string | null
  authority: string | null
  intimacy: string | null
  brightness: string | null
  maturity: string | null
  delivery_style: string | null
  texture_tags: string[] | null
  childlike: boolean | null
  labeled_by: string | null
  labeled_at: string | null
  superseded_at: string | null
}

export interface VoiceCatalogListResponse {
  items: VoiceCatalogItem[]
  total: number
  page: number
  page_size: number
}

export interface VoiceCatalogDetailResponse {
  voice: VoiceCatalogItem
  labels: VoiceCatalogLabel[]
}

// Phase 2: write operations

export interface CreateVoiceRequest {
  voice_id: string
  provider: string
  provider_config?: Record<string, unknown>
  display_name: string
  gender?: string | null
  language?: string
  scene?: string | null
  matchable?: boolean
  notes?: string | null
}

export interface UpdateVoiceRequest {
  display_name?: string
  gender?: string | null
  language?: string
  scene?: string | null
  matchable?: boolean
  provider_config?: Record<string, unknown>
  notes?: string | null
}

export interface ImportRequest {
  text: string
  provider: string
  dry_run: boolean
}

export interface ImportPreviewEntry {
  voice_id: string
  provider: string
  display_name: string
  gender?: string | null
  scene?: string | null
  status: 'will_create' | 'skip_duplicate'
}

export interface ImportPreviewResponse {
  dry_run: true
  entries: ImportPreviewEntry[]
  total: number
}

export interface ImportResultResponse {
  dry_run: false
  created: string[]
  skipped: string[]
  errors: Array<{ voice_id: string; error: string }>
}

export interface VerifyResponse {
  voice_id: string
  verify_status: Record<string, VoiceCatalogVerifyDimension>
  voice: VoiceCatalogItem
}

export interface BatchVerifyResponse {
  results: Array<{
    voice_id: string
    verify_status?: Record<string, VoiceCatalogVerifyDimension>
    error?: string
  }>
}
