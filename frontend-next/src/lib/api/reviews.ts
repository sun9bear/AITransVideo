import { apiClient } from '@/lib/api/client'
import { getJob } from '@/lib/api/jobs'
import type { ApiWebUiStateResponse } from '@/types/api'
import type { JobSummary } from '@/types/jobs'
import type {
  NativeReviewStage,
  ReviewSpeakerOption,
  ReviewJobTransition,
  TranslationReviewApprovalInput,
  TranslationReviewItem,
  TranslationReviewResource,
  VoiceReviewApprovalInput,
} from '@/types/reviews'

export async function getTranslationReview(
  jobId: string,
): Promise<TranslationReviewResource> {
  const [reviewState, job] = await Promise.all([
    apiClient.get<{ job_id: string; status: string; review_gate: Record<string, unknown> | null; results: ApiWebUiStateResponse['results'] }>(`/jobs/${jobId}/review-state`),
    getJob(jobId),
  ])
  // Adapt the narrower Job API response to the legacy payload shape
  // Adapt narrower Job API review-state to legacy ApiWebUiStateResponse shape.
  // Only the fields actually used by toTranslationReviewResource are populated.
  const adapted = {
    meta: { title: '', config_path: '', project_root: '' },
    settings: {},
    job: { status: reviewState.status, review_gate: reviewState.review_gate, project_dir: reviewState.results?.project_dir },
    results: reviewState.results,
  } as unknown as ApiWebUiStateResponse
  return toTranslationReviewResource(adapted, job)
}

export interface SplitSegmentInput {
  jobId: string
  projectDir: string
  segmentId: string
  splitSourceIndex: number
  splitCnIndex: number
  speakerA: string
  speakerB: string
  stage: 'translation_review' | 'speaker_review'
  pendingSpeakerChanges?: Record<string, string>
}

export async function splitSegment(
  input: SplitSegmentInput,
): Promise<{ success: boolean }> {
  const body: Record<string, unknown> = {
    segment_id: input.segmentId,
    split_source_index: input.splitSourceIndex,
    split_cn_index: input.splitCnIndex,
    speaker_a: input.speakerA,
    speaker_b: input.speakerB,
    stage: input.stage,
  }

  if (input.pendingSpeakerChanges && Object.keys(input.pendingSpeakerChanges).length > 0) {
    body.pending_speaker_changes = input.pendingSpeakerChanges
  }

  const result = await apiClient.post<{ success: boolean; split_result: { success: boolean } }>(
    `/jobs/${input.jobId}/review/split-segment`,
    { body },
  )

  return { success: result.split_result?.success ?? result.success ?? false }
}

export async function approveTranslationConfigReview(
  jobId: string,
  input?: { selectedModel?: string; promptTemplate?: string | null },
): Promise<ReviewJobTransition> {
  const body: Record<string, unknown> = {}
  if (input?.selectedModel) body.selected_model = input.selectedModel
  if (input?.promptTemplate !== undefined) body.prompt_template = input.promptTemplate

  await apiClient.post<{ success: boolean }>(
    `/jobs/${jobId}/review/translation-config/approve`,
    { body },
  )
  return {
    job: await getJob(jobId),
  }
}

export async function approveTranslationReview(
  input: TranslationReviewApprovalInput,
): Promise<ReviewJobTransition> {
  const body: Record<string, unknown> = {
    segments: Object.fromEntries(
      Object.entries(input.segments).map(([segmentId, entry]) => [
        segmentId,
        {
          cn_text: entry.cnText,
          translation_confirmed: entry.translationConfirmed,
          rewrite_requested: entry.rewriteRequested,
          updated_at: entry.updatedAt,
        },
      ]),
    ),
  }

  if (input.segmentSpeakers && Object.keys(input.segmentSpeakers).length > 0) {
    body.segment_speakers = input.segmentSpeakers
  }
  if (input.speakerNames && Object.keys(input.speakerNames).length > 0) {
    body.speaker_names = input.speakerNames
  }

  await apiClient.post<{ success: boolean; job: Record<string, unknown> }>(
    `/jobs/${input.jobId}/review/translation/approve`,
    { body },
  )

  return {
    job: await getJob(input.jobId),
  }
}

export async function approveVoiceReview(
  input: VoiceReviewApprovalInput,
): Promise<ReviewJobTransition> {
  await apiClient.post<{ success: boolean }>(
    `/jobs/${input.jobId}/review/voice/approve`,
    {
      body: {
        project_dir: input.projectDir,
        voice_id_a: input.voiceIdA,
        voice_id_b: input.voiceIdB,
      },
    },
  )
  return {
    job: await getJob(input.jobId),
  }
}

export async function cancelJob(jobId: string): Promise<{ success: boolean }> {
  const result = await apiClient.post<{ success: boolean }>(`/jobs/${jobId}/cancel`, { body: {} })
  return { success: result.success ?? true }
}

export async function deleteJob(jobId: string): Promise<{ success: boolean }> {
  // Use job-scoped DELETE via Gateway intercept (ownership + quota + PG cleanup)
  const resp = await fetch(`/job-api/jobs/${encodeURIComponent(jobId)}`, {
    method: 'DELETE',
    credentials: 'include',
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: '删除失败' }))
    throw new Error(err.error || err.detail || '删除失败')
  }
  return { success: true }
}

export async function previewSegmentForJob(
  jobId: string,
  params: {
    segmentId: number
    sourceStartMs: number
    sourceEndMs: number
    cnText: string
    voiceId: string
  },
): Promise<{ ttsAudioBase64: string; sourceAudioBase64: string }> {
  const result = await apiClient.post<{
    tts_audio_base64: string
    source_audio_base64: string
    source_format: string
    tts_format: string
  }>(`/jobs/${jobId}/review/preview-segment`, {
    body: {
      segment_id: params.segmentId,
      source_start_ms: params.sourceStartMs,
      source_end_ms: params.sourceEndMs,
      cn_text: params.cnText,
      voice_id: params.voiceId,
    },
  })
  return {
    ttsAudioBase64: result.tts_audio_base64,
    sourceAudioBase64: result.source_audio_base64,
  }
}

// Source-only audio preview (no TTS). Passes empty voice_id so backend
// skips TTS synthesis and only extracts the source clip via ffmpeg.
export async function previewSourceAudioForJob(
  jobId: string,
  params: {
    segmentId: number
    sourceStartMs: number
    sourceEndMs: number
  },
): Promise<{ sourceAudioBase64: string }> {
  const result = await apiClient.post<{
    source_audio_base64: string
    source_format: string
  }>(`/jobs/${jobId}/review/preview-segment`, {
    body: {
      segment_id: params.segmentId,
      source_start_ms: params.sourceStartMs,
      source_end_ms: params.sourceEndMs,
      cn_text: '',
      voice_id: '',
    },
  })
  return { sourceAudioBase64: result.source_audio_base64 }
}

// ---------------------------------------------------------------------------
// Internal helpers used by getTranslationReview
// ---------------------------------------------------------------------------

function toTranslationReviewResource(
  payload: ApiWebUiStateResponse,
  expectedJob: JobSummary,
): TranslationReviewResource {
  const job = toExpectedReviewJob(payload, expectedJob, 'translation_review')
  const section = payload.results.translation_review
  const items = payload.results.translation_review.items.map<TranslationReviewItem>((item) => ({
    cnText: item.cn_text,
    displayName: item.display_name,
    reviewUpdatedAt: normalizeText(item.review_updated_at),
    rewriteRequested: Boolean(item.rewrite_requested),
    segmentId: String(item.segment_id),
    sourceText: item.source_text,
    speakerId: item.speaker_id,
    translationConfirmed: Boolean(item.translation_confirmed),
    startMs: typeof item.start_ms === 'number' ? item.start_ms : 0,
    endMs: typeof item.end_ms === 'number' ? item.end_ms : 0,
  }))
  const payloadOptions = getSpeakerOptions(payload)
  // Read reviewer-identified speaker names from translation_review payload
  const reviewerNames: Record<string, string> = (section as unknown as Record<string, unknown>)?.speaker_names as Record<string, string> ?? {}
  const speakerOptions = mergeSpeakerOptions(payloadOptions, items.map((item) => ({
    speakerId: item.speakerId,
    displayName: reviewerNames[item.speakerId] || item.displayName,
  })))
  const projectDir = resolveProjectDir(payload, job.projectDir)

  return {
    activeMessage: resolveActiveReviewMessage(payload),
    defaultPageSize: resolveDefaultPageSize(section?.default_page_size),
    fallbackHref: '/workspace',
    items,
    job,
    pageSizeOptions: resolvePageSizeOptions(section?.page_size_options),
    projectDir,
    speakerOptions,
  }
}

function toExpectedReviewJob(
  payload: ApiWebUiStateResponse,
  expectedJob: JobSummary,
  expectedStage: NativeReviewStage,
) {
  const currentStage = resolveExpectedRouteReviewStage(expectedJob)
  const activeStage = resolveActiveReviewStage(payload)

  if (expectedJob.status !== 'waiting_for_review' || currentStage !== expectedStage) {
    throw new Error(`当前任务没有等待处理的 ${getReviewStageLabel(expectedStage)}。`)
  }

  const expectedProjectDir = normalizeText(expectedJob.projectDir)
  const snapshotProjectDir = resolveProjectDir(payload, null, false)
  if (
    snapshotProjectDir &&
    expectedProjectDir &&
    !projectDirEquals(snapshotProjectDir, expectedProjectDir)
  ) {
    throw new Error('当前审核快照还没有切换到这个任务，请稍后刷新后再试。')
  }

  if (activeStage && activeStage !== expectedStage) {
    throw new Error(`当前任务没有等待处理的 ${getReviewStageLabel(expectedStage)}。`)
  }

  return expectedJob
}

function resolveActiveReviewStage(payload: ApiWebUiStateResponse) {
  const activeStage = normalizeText(payload.results.review_flow.active_stage)
  if (
    activeStage === 'speaker_review' ||
    activeStage === 'translation_review' ||
    activeStage === 'voice_review' ||
    activeStage === 'voice_selection_review'
  ) {
    return activeStage
  }

  const gateStage = normalizeText(payload.job.review_gate?.stage)
  if (
    gateStage === 'speaker_review' ||
    gateStage === 'translation_review' ||
    gateStage === 'voice_review' ||
    gateStage === 'voice_selection_review'
  ) {
    return gateStage
  }

  return null
}

function resolveActiveReviewMessage(payload: ApiWebUiStateResponse) {
  return (
    normalizeText(payload.results.review_flow.active_review?.payload?.message) ??
    normalizeText(payload.results.review_flow.active_review?.payload?.detail) ??
    normalizeText(payload.results.voice_library.active_review?.message) ??
    normalizeText(payload.job.review_gate?.message) ??
    jobProgressMessage(payload)
  )
}

function jobProgressMessage(payload: ApiWebUiStateResponse) {
  const jobSnapshot = payload.job as unknown as Record<string, unknown>
  return (
    normalizeText(jobSnapshot.progress_message) ??
    normalizeText(jobSnapshot.current_message)
  )
}

function resolveProjectDir(
  payload: ApiWebUiStateResponse,
  fallbackProjectDir: string | null,
): string
function resolveProjectDir(
  payload: ApiWebUiStateResponse,
  fallbackProjectDir: string | null,
  requireValue: false,
): string | null
function resolveProjectDir(
  payload: ApiWebUiStateResponse,
  fallbackProjectDir: string | null,
  requireValue = true,
) {
  const jobSnapshot = payload.job as unknown as Record<string, unknown>
  const projectDir =
    normalizeText(payload.results.project_dir) ??
    normalizeText(jobSnapshot.project_dir) ??
    normalizeText(fallbackProjectDir)

  if (!projectDir && requireValue) {
    throw new Error('当前 review 缺少可用的项目目录。')
  }

  return projectDir
}

function getSpeakerOptions(payload: ApiWebUiStateResponse) {
  const stagePayload = payload.results.review_flow.stages.speaker_review?.payload
  const rawOptions = Array.isArray(stagePayload?.speaker_options)
    ? stagePayload.speaker_options
    : []

  return rawOptions.map<ReviewSpeakerOption | null>((option) => {
    if (!option || typeof option !== 'object') {
      return null
    }

    const speakerId = normalizeText(
      'speaker_id' in option
        ? option.speaker_id
        : 'value' in option
          ? option.value
          : null,
    )
    if (!speakerId) {
      return null
    }

    const displayName =
      normalizeText(
        'display_name' in option
          ? option.display_name
          : 'label' in option
            ? option.label
            : null,
      ) ?? speakerId

    return {
      displayName,
      id: speakerId,
    }
  })
}

function mergeSpeakerOptions(
  stageOptions: Array<ReviewSpeakerOption | null>,
  items: Array<{ speakerId: string; displayName: string }>,
) {
  const deduped = new Map<string, ReviewSpeakerOption>()

  for (const option of stageOptions) {
    if (!option) {
      continue
    }

    deduped.set(option.id, option)
  }

  for (const item of items) {
    if (!deduped.has(item.speakerId)) {
      deduped.set(item.speakerId, {
        displayName: item.displayName || item.speakerId,
        id: item.speakerId,
      })
    }
  }

  return Array.from(deduped.values())
}

function getReviewStageLabel(stage: NativeReviewStage) {
  if (stage === 'speaker_review') {
    return '说话人审核'
  }

  if (stage === 'voice_review') {
    return '音色确认'
  }

  if (stage === 'voice_selection_review') {
    return '音色选择'
  }

  return '翻译审核'
}

function resolveExpectedRouteReviewStage(job: JobSummary) {
  return normalizeNativeReviewStage(job.reviewGate?.stage) ?? normalizeNativeReviewStage(job.currentStage)
}

function resolveDefaultPageSize(value: unknown) {
  const numericValue = Number(value)
  if (!Number.isInteger(numericValue) || numericValue <= 0) {
    return 20
  }

  return numericValue
}

function resolvePageSizeOptions(value: unknown) {
  const resolved = new Set<number>([20, 30, 50, 100])

  if (Array.isArray(value)) {
    for (const rawOption of value) {
      const numericValue = Number(rawOption)
      if (Number.isInteger(numericValue) && numericValue > 0) {
        resolved.add(numericValue)
      }
    }
  }

  return Array.from(resolved).sort((left, right) => left - right)
}

function normalizeNativeReviewStage(value: unknown): NativeReviewStage | null {
  if (value === 'speaker_review' || value === 'translation_review' || value === 'voice_review' || value === 'voice_selection_review') {
    return value
  }

  return null
}

function projectDirEquals(left: string, right: string) {
  return normalizeProjectDir(left) === normalizeProjectDir(right)
}

function normalizeProjectDir(value: string) {
  return value.trim().replaceAll('\\', '/').replace(/\/+$/, '').toLowerCase()
}

function normalizeText(value: unknown) {
  if (typeof value !== 'string') {
    return null
  }

  const normalized = value.trim()
  return normalized || null
}
