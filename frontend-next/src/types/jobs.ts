export const JOB_STATUS_LABELS = {
  cancelled: '已取消',
  editing: '修改中',
  failed: '已失败',
  queued: '待开始',
  running: '处理中',
  succeeded: '已完成',
  waiting_for_review: '等待审核',
  // 2026-04-21: gateway project_cleanup 7d TTL soft-delete. DB 记录
  // 保留做历史；project_dir 已在磁盘清理。下载 / 修改入口应当禁用。
  purged: '已清理',
  // Pan backup statuses (plan 2026-05-14 Task 1.5 / design 2026-05-13 §4.1)
  archiving: '归档中',
  archived: '已归档',
  restoring: '恢复中',
} as const

export type JobStatus = keyof typeof JOB_STATUS_LABELS

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
  serviceMode?: 'express' | 'studio' | 'smart'
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
  service_mode?: 'express' | 'studio' | 'smart'
  /**
   * Phase 4.3a PR3 — Express auto-clone consent (user opt-in checkbox).
   * Only meaningful for ``service_mode === 'express'``. Defaults to false
   * (no clone). When true, ``submitTranslationJob`` sends
   * ``express_consent.auto_voice_clone = true`` + a client timestamp. The
   * submit layer forces this to false for non-express modes so a stale
   * checkbox can never trigger a paid clone (spec §2.6).
   */
  expressAutoVoiceClone?: boolean
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
