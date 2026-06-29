// UI-05（方案 §0.2 / Task 2.1）：状态显示文案迁入 messages/{zh,en}/app.json 的 `status.*`，
// 由消费方 `t('status.' + status)`（status-badge.tsx / log-viewer.tsx）解析。此处只保留
// 状态键的真源——`JobStatus` 类型与运行时遍历用键集仍以本数组为单一来源。
//   - purged（2026-04-21）：gateway project_cleanup 7d TTL soft-delete。DB 记录保留做历史；
//     project_dir 已在磁盘清理。下载 / 修改入口应当禁用。
//   - archiving/archived/restoring：Pan backup statuses（plan 2026-05-14 Task 1.5 / design 2026-05-13 §4.1）。
export const JOB_STATUSES = [
  'cancelled',
  'editing',
  'failed',
  'queued',
  'running',
  'succeeded',
  'waiting_for_review',
  'purged',
  'archiving',
  'archived',
  'restoring',
] as const

export type JobStatus = (typeof JOB_STATUSES)[number]

export type PublicStage =
  | 'draft'
  | 'failed'
  | 'ingestion'
  | 'legacy_process_output'
  | 'media_understanding'
  | 'speaker_review'
  | 'translation_config_review'
  | 'translation_review'
  | 'voice_review'
  | 'voice_selection_review'

export type ReviewGate = Record<string, unknown>

export type ErrorSummary = Record<string, unknown>

export type JobEventLevel = 'error' | 'info' | 'warn'

export type JobEventType = 'log' | 'status'

export type StageProgressState = 'complete' | 'current' | 'error' | 'upcoming'

export type ArtifactCategorySummary = {
  existingCount: number
  name: string
  totalCount: number
}

export type ResultOutput = {
  category: string
  exists: boolean
  key: string
  name: string
  path?: string | null
}

export interface JobSummary {
  id: string
  title: string
  sourceRef: string
  sourceType: string
  outputTarget: string
  speakers: string
  voiceA: string | null
  voiceB: string | null
  /** Language direction (PR-A part 2 §5). Nullable — pre-migration-036 jobs and
   *  upstream records without the fields fall back to the GA default pair. */
  sourceLanguage?: string | null
  targetLanguage?: string | null
  languagePair?: string | null
  status: JobStatus
  currentStage: PublicStage | null
  progressMessage: string | null
  createdAt: string
  updatedAt: string
  startedAt: string | null
  completedAt: string | null
  projectDir: string | null
  manifestPath: string | null
  reviewGate: ReviewGate | null
  errorSummary: ErrorSummary | null
  fallbackSummary: Record<string, unknown> | null
  /**
   * Service mode — Express 只暴露配音视频，Studio 全量，Smart 比
   * Studio 多一个 SmartAutoDecisionPanel（自动决策摘要）。
   * 驱动 ResultMediaCard / ResultDownloadList / SmartAutoDecisionPanel
   * 的 UI 分支。
   */
  serviceMode?: 'express' | 'studio' | 'smart' | 'free'
  /**
   * P3e-4c: true when this is a 智能版 3 分钟预览 teaser job
   * (``smart_state.smart_preview_mode``). The result surface renders
   * ``SmartPreviewResultCard`` (teaser stream player + 转完整 CTA) instead of
   * the normal media/download UI — downloads / edit / export are 403'd
   * server-side (stream-only, P3e-3d). Absent / false on every non-preview job.
   */
  smartPreviewMode?: boolean
  /**
   * Post-edit infra fields (plan 2026-04-18 §3.3). All nullable because
   * jobs created before migration 015 have NULL for these columns until
   * backfill / first-touch populates them.
   */
  displayName?: string | null
  expiresAt?: string | null
  editingTouchedAt?: string | null
  copyOfJobId?: string | null
  rootJobId?: string | null
  editGeneration?: number
  roleSnapshot?: string | null
}

export interface JobLogEntry {
  id: string
  createdAt: string
  eventType: JobEventType
  level: JobEventLevel
  message: string
  stage: PublicStage | null
  status: JobStatus | null
}

export interface StageProgressItem {
  key: PublicStage
  label: string
  description: string
  state: StageProgressState
}

export const DOWNLOADABLE_ARTIFACT_KEYS = [
  'manifest.file',
  'translation.segments',
  'editor.subtitles',
  'editor.subtitles_en',
  'editor.subtitles_bilingual',
  'editor.dubbed_audio_complete',
  'editor.tts_segments_zip',
  'publish.dubbed_video',
] as const

export type DownloadableArtifactKey = (typeof DOWNLOADABLE_ARTIFACT_KEYS)[number]

export interface ResultDownloadItem {
  available: boolean
  description: string
  downloadUrl: string | null
  key: DownloadableArtifactKey
  label: string
}

export interface ProjectResultSummary {
  jobId: string
  status: JobStatus
  manifestAvailable: boolean
  artifactCount: number
  projectDir: string | null
  manifestPath: string | null
  outputs: ResultOutput[]
  artifactSummary: {
    categories: ArtifactCategorySummary[]
    existingCount: number
    totalCount: number
  }
  errorSummary: ErrorSummary | null
  fallbackSummary: Record<string, unknown> | null
}

export interface ProjectDetailResource {
  job: JobSummary
  result: ProjectResultSummary
  downloads: ResultDownloadItem[]
}

export interface CreateTranslationJobInput {
  youtubeUrl: string
  speakers: string
  voiceA?: string
  voiceB?: string
  /**
   * Language direction (PR-A part 2 §3/§7). Optional canonical codes
   * (e.g. 'en' / 'zh-CN'); omitted → GA default en->zh-CN. Only the default +
   * admin-enabled allowlisted pairs are accepted by the gateway (others 403).
   * The submit layer sends these only when a non-default pair is chosen.
   */
  sourceLanguage?: string
  targetLanguage?: string
  transcriptionMethod?: 'assemblyai' | 'gemini'
  sourceType?: 'youtube_url' | 'local_video'
  localFilePath?: string
  /**
   * Original filename of the uploaded file (before server-side sanitisation
   * into ``localFilePath``). Used by the gateway's display_name orchestrator
   * to derive a friendly task title via "truncated filename stem". Optional
   * — falls through to the "上传视频 YYYY-MM-DD NNN" branch if omitted.
   */
  localFileName?: string
  service_mode?: 'express' | 'studio' | 'smart' | 'free'
  /**
   * Phase 4.3a PR3 — Express auto-clone consent (user opt-in checkbox).
   * Only meaningful for ``service_mode === 'express'``. Defaults to false
   * (no clone). When true, ``submitTranslationJob`` sends
   * ``express_consent.auto_voice_clone = true`` + a client timestamp. The
   * submit layer forces this to false for non-express modes so a stale
   * checkbox can never trigger a paid clone (spec §2.6).
   */
  expressAutoVoiceClone?: boolean
  /**
   * Smart paid auto-clone confirmation. Only meaningful for
   * ``service_mode === 'smart'`` after the UI has loaded the authoritative
   * clone price and the user has checked the paid add-on confirmation box.
   * The submit layer forces false for non-smart modes and for unloaded prices.
   */
  smartPaidCloneConfirmed?: boolean
  /**
   * Phase 2a LAUNCH GATE (《民法典》1023): free voice-rights attestation. Only
   * meaningful for ``service_mode === 'free'``. Maps to
   * ``free_consent.voice_rights_confirmed``; the gateway HARD-fails without it.
   * The submit layer forces this false for non-free modes.
   */
  freeVoiceRightsConfirmed?: boolean
  /**
   * D7 匿名预览转完整：被认领的 preview_id。设置时 ``submitTranslationJob`` 注入
   * ``reuse_anonymous_preview_id``，服务端凭 ``claim_user_id`` 反查认领的**完整原始
   * 上传**覆盖 source、走正常付费流程（plan §6.5）。youtubeUrl/localFilePath 可留空
   * （后端 D7 block 在任何源校验之前覆盖 source）。
   */
  reuseAnonPreviewId?: string
}

export const ACTIVE_JOB_STATUSES: readonly JobStatus[] = [
  'queued',
  'running',
  'waiting_for_review',
  'editing',
] as const

/**
 * Subset of ACTIVE_JOB_STATUSES that require a live worker process. UI polling
 * may still include `editing` (via ACTIVE_JOB_STATUSES), but reap-stale or
 * "is a pipeline running" checks should use this narrower set.
 * Mirrors backend WORKER_ACTIVE_STATUSES in src/services/jobs/models.py.
 */
export const WORKER_ACTIVE_JOB_STATUSES: readonly JobStatus[] = [
  'queued',
  'running',
] as const
