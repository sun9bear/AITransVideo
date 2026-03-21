export const JOB_STATUS_LABELS = {
  cancelled: '已取消',
  failed: '已失败',
  queued: '待开始',
  running: '处理中',
  succeeded: '已完成',
  waiting_for_review: '等待审核',
} as const

export type JobStatus = keyof typeof JOB_STATUS_LABELS

export type PublicStage =
  | 'draft'
  | 'failed'
  | 'ingestion'
  | 'legacy_process_output'
  | 'media_understanding'
  | 'speaker_review'
  | 'translation_review'
  | 'voice_review'

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
  'editor.dubbed_audio_complete',
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
  speakers: '1' | '2' | 'auto'
  voiceA?: string
  voiceB?: string
}

export const ACTIVE_JOB_STATUSES: readonly JobStatus[] = [
  'queued',
  'running',
  'waiting_for_review',
] as const
