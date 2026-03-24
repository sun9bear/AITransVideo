import { buildResultDownloadUrl } from '@/lib/api/downloads'
import type {
  ApiArtifactEntry,
  ApiArtifactSummary,
  ApiJobArtifactsResponse,
  ApiJobEvent,
  ApiJobRecord,
  ApiJobResultSummary,
  ApiResultOutput,
} from '@/types/api'
import {
  DOWNLOADABLE_ARTIFACT_KEYS,
  type ArtifactCategorySummary,
  type JobLogEntry,
  type JobStatus,
  type JobSummary,
  type ProjectResultSummary,
  type PublicStage,
  type ResultDownloadItem,
  type ResultOutput,
} from '@/types/jobs'

const downloadLabels = {
  'editor.dubbed_audio_complete': '完整配音音频',
  'editor.subtitles': '字幕文件',
  'manifest.file': '项目清单（JSON）',
  'publish.dubbed_video': '成品视频',
  'translation.segments': '翻译分段（JSON）',
} as const

const downloadDescriptions = {
  'editor.dubbed_audio_complete': {
    available: '适合单独回听完整配音结果。',
    unavailable: '完整配音音频还没有生成。',
  },
  'editor.subtitles': {
    available: '适合单独查看或继续编辑字幕。',
    unavailable: '字幕文件还没有生成。',
  },
  'manifest.file': {
    available: '用于查看当前项目清单。',
    unavailable: '项目清单还没有生成。',
  },
  'publish.dubbed_video': {
    available: '适合直接查看或交付最终成品。',
    unavailable: '成品视频还没有生成。',
  },
  'translation.segments': {
    available: '用于补充核对分段翻译内容。',
    unavailable: '翻译分段文件还没有生成。',
  },
} as const

const downloadDisplayOrder = [
  'publish.dubbed_video',
  'editor.dubbed_audio_complete',
  'editor.subtitles',
  'translation.segments',
  'manifest.file',
] as const

export function toJobSummary(payload: ApiJobRecord): JobSummary {
  return {
    id: payload.job_id,
    sourceRef: payload.source_ref,
    sourceType: payload.source_type,
    outputTarget: payload.output_target,
    speakers: payload.speakers,
    title: buildJobTitle(payload),
    voiceA: payload.voice_a,
    voiceB: payload.voice_b,
    status: normalizeStatus(payload.status),
    currentStage: normalizeStage(payload.current_stage),
    progressMessage: payload.progress_message,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
    startedAt: payload.started_at,
    completedAt: payload.completed_at,
    projectDir: payload.project_dir,
    manifestPath: payload.manifest_path,
    reviewGate: payload.review_gate,
    errorSummary: payload.error_summary,
    fallbackSummary: payload.fallback_summary,
  }
}

export function toJobLogEntries(events: readonly ApiJobEvent[]): JobLogEntry[] {
  return events.map((event, index) => ({
    id: `${event.job_id}-${event.created_at}-${index}`,
    createdAt: event.created_at,
    eventType: event.event_type,
    level: event.level,
    message: event.message ?? '(empty log event)',
    stage: normalizeStage(event.stage),
    status: event.status ? normalizeStatus(event.status) : null,
  }))
}

export function toProjectResultSummary(payload: ApiJobResultSummary): ProjectResultSummary {
  return {
    artifactCount: payload.manifest.artifact_count,
    artifactSummary: toArtifactSummary(payload.artifacts),
    jobId: payload.job_id,
    manifestAvailable: payload.manifest.available,
    projectDir: payload.project_dir,
    manifestPath: payload.manifest_path,
    outputs: payload.outputs.map(toResultOutput),
    status: normalizeStatus(payload.status),
    errorSummary: payload.error_summary,
    fallbackSummary: payload.fallback_summary,
  }
}

export function toResultDownloadItems(payload: ApiJobArtifactsResponse): ResultDownloadItem[] {
  const filteredArtifacts = payload.artifacts.filter(
    (
      artifact,
    ): artifact is ApiArtifactEntry & {
      key: (typeof DOWNLOADABLE_ARTIFACT_KEYS)[number]
    } =>
      DOWNLOADABLE_ARTIFACT_KEYS.includes(
        artifact.key as (typeof DOWNLOADABLE_ARTIFACT_KEYS)[number],
      ),
  )

  const orderedArtifacts = downloadDisplayOrder
    .map((key) => filteredArtifacts.find((artifact) => artifact.key === key))
    .filter(
      (
        artifact,
      ): artifact is ApiArtifactEntry & {
        key: (typeof DOWNLOADABLE_ARTIFACT_KEYS)[number]
      } => Boolean(artifact),
    )

  return orderedArtifacts.map((artifact) => ({
    available: artifact.exists,
    description: artifact.exists
      ? downloadDescriptions[artifact.key].available
      : downloadDescriptions[artifact.key].unavailable,
    downloadUrl: artifact.exists
      ? buildResultDownloadUrl({
          downloadKey: artifact.key,
          jobId: payload.job_id,
          projectDir: payload.project_dir,
        })
      : null,
    key: artifact.key,
    label: downloadLabels[artifact.key],
  }))
}

function toResultOutput(payload: ApiResultOutput): ResultOutput {
  return {
    category: payload.category ?? 'other',
    exists: payload.exists,
    key: payload.key,
    name: payload.name,
    path: payload.path ?? null,
  }
}

function toArtifactSummary(payload: ApiArtifactSummary): {
  categories: ArtifactCategorySummary[]
  existingCount: number
  totalCount: number
} {
  return {
    categories: payload.categories.map((category) => ({
      existingCount: category.existing_count,
      name: category.name,
      totalCount: category.count,
    })),
    existingCount: payload.existing_count,
    totalCount: payload.total_count,
  }
}

function buildJobTitle(payload: ApiJobRecord) {
  if (!payload.source_ref) {
    return payload.job_id
  }

  try {
    const url = new URL(payload.source_ref)
    return url.searchParams.get('v') ?? url.hostname
  } catch {
    return payload.source_ref
  }
}

function normalizeStage(value: string | null): PublicStage | null {
  if (!value) {
    return null
  }

  return value in publicStageMap
    ? publicStageMap[value as keyof typeof publicStageMap]
    : null
}

function normalizeStatus(value: string): JobStatus {
  return value in statusMap ? statusMap[value as keyof typeof statusMap] : 'queued'
}

const publicStageMap = {
  cancelled: 'failed',
  completed: 'legacy_process_output',
  draft: 'draft',
  failed: 'failed',
  ingestion: 'ingestion',
  legacy_process_output: 'legacy_process_output',
  media_understanding: 'media_understanding',
  speaker_review: 'speaker_review',
  translation_config_review: 'translation_config_review',
  translation_review: 'translation_review',
  voice_review: 'voice_review',
} as const satisfies Record<string, PublicStage>

const statusMap = {
  cancelled: 'cancelled',
  failed: 'failed',
  queued: 'queued',
  running: 'running',
  succeeded: 'succeeded',
  waiting_for_review: 'waiting_for_review',
} as const satisfies Record<string, JobStatus>
