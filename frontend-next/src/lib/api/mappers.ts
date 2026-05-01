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
  'editor.subtitles': '中文字幕',
  'editor.subtitles_en': '英文字幕',
  'editor.subtitles_bilingual': '双语字幕',
  'editor.tts_segments_zip': '分段配音音频（ZIP）',
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
    available: '中文短句字幕，剪映风格。',
    unavailable: '中文字幕还没有生成。',
  },
  'editor.subtitles_en': {
    available: '英文短句字幕。',
    unavailable: '英文字幕还没有生成。',
  },
  'editor.subtitles_bilingual': {
    available: '中英双语对照字幕。',
    unavailable: '双语字幕还没有生成。',
  },
  'editor.tts_segments_zip': {
    available: '每段配音的独立音频文件，打包下载。',
    unavailable: '分段配音音频还没有生成。',
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
  'editor.tts_segments_zip',
  'editor.subtitles',
  'editor.subtitles_en',
  'editor.subtitles_bilingual',
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
    // Prefer display_name (user-friendly, migration 015); fallback to
    // derivation from source_ref for pre-migration / pre-T0-4 jobs.
    title: payload.display_name ?? buildJobTitle(payload),
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
    serviceMode: payload.service_mode,
    // --- Post-edit infra ---
    displayName: payload.display_name ?? null,
    expiresAt: payload.expires_at ?? null,
    editingTouchedAt: payload.editing_touched_at ?? null,
    copyOfJobId: payload.copy_of_job_id ?? null,
    rootJobId: payload.root_job_id ?? null,
    editGeneration: payload.edit_generation ?? 0,
    roleSnapshot: payload.role_snapshot ?? null,
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

  const items = orderedArtifacts.map((artifact) => ({
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

  // Add TTS segments zip entry (virtual — not in backend artifacts, on-demand zip)
  const hasDubbedAudio = filteredArtifacts.some(
    (a) => a.key === 'editor.dubbed_audio_complete' && a.exists,
  )
  if (hasDubbedAudio && payload.job_id) {
    const ttsKey = 'editor.tts_segments_zip' as const
    const ttsItem = {
      available: true,
      description: downloadDescriptions[ttsKey].available,
      downloadUrl: buildResultDownloadUrl({
        downloadKey: ttsKey,
        jobId: payload.job_id,
        projectDir: payload.project_dir,
      }),
      key: ttsKey,
      label: downloadLabels[ttsKey],
    }
    // Insert after dubbed_audio_complete
    const dubbedIdx = items.findIndex((i) => i.key === 'editor.dubbed_audio_complete')
    if (dubbedIdx >= 0) {
      items.splice(dubbedIdx + 1, 0, ttsItem)
    } else {
      items.push(ttsItem)
    }
  }

  return items
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
  if (payload.source_type === 'local_video' && payload.source_ref) {
    const filename = payload.source_ref.split(/[\\/]/).pop()?.trim()
    if (filename) {
      const dotIndex = filename.lastIndexOf('.')
      const stem = dotIndex > 0 ? filename.slice(0, dotIndex) : filename
      if (stem.trim()) {
        return stem.trim()
      }
    }
  }

  return '未命名视频'
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
  // γ commit sets JobRecord.current_stage='alignment' (STAGE_ALIGNMENT
  // from src/services/jobs/models.py) for resume-from-alignment runs.
  // The UI stepper has no dedicated "alignment" step; "draft" (草稿与
  // 配音) is the user-facing label that covers both TTS generation and
  // alignment. Without this mapping, currentStage="alignment" normalizes
  // to null and buildStageProgress falls back to "step 1 ingestion".
  alignment: 'draft',
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
  voice_selection_review: 'voice_selection_review',
} as const satisfies Record<string, PublicStage>

const statusMap = {
  cancelled: 'cancelled',
  editing: 'editing',
  failed: 'failed',
  purged: 'purged',
  queued: 'queued',
  running: 'running',
  succeeded: 'succeeded',
  waiting_for_review: 'waiting_for_review',
} as const satisfies Record<string, JobStatus>
