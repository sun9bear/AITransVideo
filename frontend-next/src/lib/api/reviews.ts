import { ApiClient } from '@/lib/api/client'
import { buildBackendUrl, resolveWebUiBaseUrl } from '@/lib/api/config'
import { getJob } from '@/lib/api/jobs'
import type { ApiWebUiStateResponse } from '@/types/api'
import type { JobSummary } from '@/types/jobs'
import type {
  NativeReviewStage,
  ReviewJobTransition,
  ReviewSpeakerOption,
  SpeakerReviewApprovalInput,
  SpeakerReviewItem,
  SpeakerReviewResource,
  TranslationReviewApprovalInput,
  TranslationReviewItem,
  TranslationReviewResource,
  VoiceReviewApprovalInput,
  VoiceReviewAvailableVoice,
  VoiceReviewDefaultBindingInput,
  VoiceReviewManualBindingInput,
  VoiceReviewResource,
  VoiceReviewSpeaker,
} from '@/types/reviews'

const webUiApiClient = new ApiClient(resolveWebUiBaseUrl())

export async function getSpeakerReview(
  jobId: string,
): Promise<SpeakerReviewResource> {
  const [payload, job] = await Promise.all([
    webUiApiClient.get<ApiWebUiStateResponse>('/api/state'),
    getJob(jobId),
  ])
  return toSpeakerReviewResource(payload, job)
}

export async function getTranslationReview(
  jobId: string,
): Promise<TranslationReviewResource> {
  const [payload, job] = await Promise.all([
    webUiApiClient.get<ApiWebUiStateResponse>('/api/state'),
    getJob(jobId),
  ])
  return toTranslationReviewResource(payload, job)
}

export async function getVoiceReview(
  jobId: string,
): Promise<VoiceReviewResource> {
  const [payload, job] = await Promise.all([
    webUiApiClient.get<ApiWebUiStateResponse>('/api/state'),
    getJob(jobId),
  ])
  return toVoiceReviewResource(payload, job)
}

export async function approveSpeakerReview(
  input: SpeakerReviewApprovalInput,
): Promise<ReviewJobTransition> {
  await webUiApiClient.post<ApiWebUiStateResponse>(
    '/api/review/speaker/approve',
    {
      body: {
        project_dir: input.projectDir,
        speaker_names: input.speakerNames,
        segment_speakers: input.segmentSpeakers,
        confirmations: Object.fromEntries(
          Object.entries(input.confirmations).map(([segmentId, entry]) => [
            segmentId,
            {
              speaker_confirmed: entry.speakerConfirmed,
              transcript_confirmed: entry.transcriptConfirmed,
              updated_at: entry.updatedAt,
            },
          ]),
        ),
      },
    },
  )

  return {
    job: await getJob(input.jobId),
  }
}

export interface SplitSegmentInput {
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
    project_dir: input.projectDir,
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

  const result = await webUiApiClient.post<{ split_result: { success: boolean } }>(
    '/api/review/split-segment',
    { body },
  )

  return { success: result.split_result?.success ?? false }
}

export async function approveTranslationReview(
  input: TranslationReviewApprovalInput,
): Promise<ReviewJobTransition> {
  const body: Record<string, unknown> = {
    project_dir: input.projectDir,
    segments: Object.fromEntries(
      Object.entries(input.segments).map(([segmentId, entry]) => [
        segmentId,
        {
          cn_text: entry.cnText,
          tts_cn_text: entry.ttsCnText,
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

  await webUiApiClient.post<ApiWebUiStateResponse>(
    '/api/review/translation/approve',
    { body },
  )

  return {
    job: await getJob(input.jobId),
  }
}

export async function bindVoiceReviewDefault(
  input: VoiceReviewDefaultBindingInput,
): Promise<VoiceReviewResource> {
  await webUiApiClient.post<ApiWebUiStateResponse>(
    '/api/voice-library/set-default',
    {
      body: {
        speaker_id: input.speakerId,
        voice_id: input.voiceId,
      },
    },
  )

  return getVoiceReview(input.jobId)
}

export async function registerVoiceReviewManual(
  input: VoiceReviewManualBindingInput,
): Promise<VoiceReviewResource> {
  await webUiApiClient.post<ApiWebUiStateResponse>(
    '/api/voice-library/register-manual',
    {
      body: {
        sample_path: input.samplePath,
        speaker_id: input.speakerId,
        speaker_name: input.speakerName,
        voice_id: input.voiceId,
      },
    },
  )

  return getVoiceReview(input.jobId)
}

export async function approveVoiceReview(
  input: VoiceReviewApprovalInput & { voiceIdA?: string; voiceIdB?: string },
): Promise<ReviewJobTransition> {
  await webUiApiClient.post<ApiWebUiStateResponse>(
    '/api/review/voice/approve',
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

export async function previewVoice(
  voiceId: string,
  speakerId?: string,
): Promise<{ audioBase64: string; audioFormat: string }> {
  const result = await webUiApiClient.post<{
    success: boolean
    audio_base64: string
    audio_format: string
  }>('/api/review/voice/preview', {
    body: {
      voice_id: voiceId,
      speaker_id: speakerId ?? 'preview',
    },
  })

  return {
    audioBase64: result.audio_base64,
    audioFormat: result.audio_format,
  }
}

export async function cloneVoiceForReview(
  speakerId: string,
  speakerName: string,
  samplePath: string,
  projectDir?: string,
): Promise<{ voiceId: string }> {
  const body: Record<string, unknown> = {
    speaker_id: speakerId,
    speaker_name: speakerName,
  }
  if (samplePath) body.sample_path = samplePath
  if (projectDir) body.project_dir = projectDir

  const result = await webUiApiClient.post<{
    success: boolean
    voice_id: string
  }>('/api/review/voice/clone', { body })

  return { voiceId: result.voice_id }
}

export async function cancelCurrentJob(): Promise<{ success: boolean }> {
  const result = await webUiApiClient.post<{ success: boolean }>('/api/job/cancel', { body: {} })
  return { success: result.success ?? true }
}

export async function deleteJob(jobId: string): Promise<{ success: boolean }> {
  // Call gateway's /api/job/delete directly (not via webUiApiClient)
  // so the gateway can also clean up PostgreSQL records
  const resp = await fetch('/api/job/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ job_id: jobId }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: '删除失败' }))
    throw new Error(err.detail || '删除失败')
  }
  return { success: true }
}

export async function getWebUiActiveStage(): Promise<string | null> {
  try {
    const payload = await webUiApiClient.get<ApiWebUiStateResponse>('/api/state')
    const activeStage = payload.results?.review_flow?.active_stage
    if (typeof activeStage === 'string' && activeStage) return activeStage
    const gateStage = payload.job?.review_gate?.stage
    if (typeof gateStage === 'string' && gateStage) return gateStage
    return null
  } catch {
    return null
  }
}

export function buildLegacyReviewFallbackUrl() {
  return buildBackendUrl(resolveWebUiBaseUrl(), '/')
}

function toSpeakerReviewResource(
  payload: ApiWebUiStateResponse,
  expectedJob: JobSummary,
): SpeakerReviewResource {
  const job = toExpectedReviewJob(payload, expectedJob, 'speaker_review')
  const section = payload.results.transcript_review
  const items = payload.results.transcript_review.items.map<SpeakerReviewItem>((item) => ({
    displayName: item.display_name,
    reviewUpdatedAt: normalizeText(item.review_updated_at),
    segmentId: String(item.segment_id),
    sourceText: item.source_text,
    speakerConfirmed: Boolean(item.speaker_confirmed),
    speakerId: item.speaker_id,
    transcriptConfirmed: Boolean(item.transcript_confirmed),
    transcriptText: item.cn_text,
  }))
  const payloadOptions = getSpeakerOptions(payload)
  const speakerOptions = mergeSpeakerOptions(payloadOptions, items)
  const projectDir = resolveProjectDir(payload, job.projectDir)

  return {
    activeMessage: resolveActiveReviewMessage(payload),
    defaultPageSize: resolveDefaultPageSize(section?.default_page_size),
    fallbackHref: buildLegacyReviewFallbackUrl(),
    items,
    job,
    pageSizeOptions: resolvePageSizeOptions(section?.page_size_options),
    projectDir,
    speakerOptions,
  }
}

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
    ttsCnText: item.tts_cn_text || item.cn_text,
    startMs: typeof item.start_ms === 'number' ? item.start_ms : 0,
    endMs: typeof item.end_ms === 'number' ? item.end_ms : 0,
  }))
  const payloadOptions = getSpeakerOptions(payload)
  const speakerOptions = mergeSpeakerOptions(payloadOptions, items.map((item) => ({
    speakerId: item.speakerId,
    displayName: item.displayName,
  })))
  const projectDir = resolveProjectDir(payload, job.projectDir)

  return {
    activeMessage: resolveActiveReviewMessage(payload),
    defaultPageSize: resolveDefaultPageSize(section?.default_page_size),
    fallbackHref: buildLegacyReviewFallbackUrl(),
    items,
    job,
    pageSizeOptions: resolvePageSizeOptions(section?.page_size_options),
    projectDir,
    speakerOptions,
  }
}

function toVoiceReviewResource(
  payload: ApiWebUiStateResponse,
  expectedJob: JobSummary,
): VoiceReviewResource {
  const job = toExpectedReviewJob(payload, expectedJob, 'voice_review')
  const voiceReview = payload.results.voice_library.active_review

  if (!voiceReview) {
    throw new Error('当前 voice review 快照不可用。')
  }

  const projectDir = resolveProjectDir(payload, job.projectDir)
  const speakers = voiceReview.speakers.map<VoiceReviewSpeaker>((speaker) => ({
    speakerId: speaker.speaker_id,
    speakerLabel: normalizeText(speaker.speaker_label),
    speakerName: normalizeText(speaker.speaker_name) ?? speaker.speaker_id,
    voiceArgName: normalizeText(speaker.voice_arg_name),
    samplePath: normalizeText(speaker.sample_path),
    sampleDurationS: Number.isFinite(speaker.sample_duration_s)
      ? speaker.sample_duration_s
      : 0,
    silenceRatio: Number.isFinite(speaker.silence_ratio) ? speaker.silence_ratio : 0,
    defaultVoiceId: normalizeText(speaker.default_voice_id),
    defaultVoiceType: normalizeText(speaker.default_voice_type),
    resolvedStatus: normalizeText(speaker.resolved_status),
    resolvedSource: normalizeText(speaker.resolved_source),
    resolvedVoiceId: normalizeText(speaker.resolved_voice_id),
    resolvedVoiceType: normalizeText(speaker.resolved_voice_type),
    resolvedLabel: normalizeText(speaker.resolved_label),
    availableVoices: speaker.available_voices
      .map<VoiceReviewAvailableVoice | null>((voice) => {
        const voiceId = normalizeText(voice.voice_id)
        if (!voiceId) {
          return null
        }

        return {
          voiceId,
          voiceType: normalizeText(voice.voice_type),
          provider: normalizeText(voice.provider),
          ttsProvider: normalizeText(voice.tts_provider),
          platform: normalizeText(voice.platform),
          label: normalizeText(voice.label),
          createdAt: normalizeText(voice.created_at),
          sourceAudioPath: normalizeText(voice.source_audio_path),
          notes: normalizeText(voice.notes),
          verificationStatus: normalizeText(voice.verification_status),
          lastVerifiedAt: normalizeText(voice.last_verified_at),
          lastVerificationSuccess:
            typeof voice.last_verification_success === 'boolean'
              ? voice.last_verification_success
              : null,
          lastVerificationAudioPath: normalizeText(voice.last_verification_audio_path),
          lastVerificationError: normalizeText(voice.last_verification_error),
        }
      })
      .filter((voice): voice is VoiceReviewAvailableVoice => voice !== null),
  }))

  return {
    activeMessage: normalizeText(voiceReview.message) ?? resolveActiveReviewMessage(payload),
    fallbackHref: buildLegacyReviewFallbackUrl(),
    job,
    projectDir,
    reason: normalizeText(voiceReview.reason),
    speakers,
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
    activeStage === 'voice_review'
  ) {
    return activeStage
  }

  const gateStage = normalizeText(payload.job.review_gate?.stage)
  if (
    gateStage === 'speaker_review' ||
    gateStage === 'translation_review' ||
    gateStage === 'voice_review'
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
  if (value === 'speaker_review' || value === 'translation_review' || value === 'voice_review') {
    return value
  }

  return null
}

function projectDirEquals(left: string, right: string) {
  return normalizeProjectDir(left) === normalizeProjectDir(right)
}

export async function getTranslationConfigReview(jobId: string) {
  const [payload, job] = await Promise.all([
    webUiApiClient.get<ApiWebUiStateResponse>('/api/state'),
    getJob(jobId),
  ])

  const reviewFlow = payload.results?.review_flow
  const configStage = reviewFlow?.stages?.translation_config_review
  const stagePayload = (configStage?.payload ?? {}) as Record<string, unknown>

  return {
    jobId: job.id,
    projectDir: job.projectDir ?? '',
    segmentCount: typeof stagePayload.segment_count === 'number' ? stagePayload.segment_count : 0,
    availableModels: Array.isArray(stagePayload.available_models)
      ? (stagePayload.available_models as Array<{ alias: string; provider: string; model_name: string }>)
      : [],
    currentModel: typeof stagePayload.current_model === 'string' ? stagePayload.current_model : '',
    currentPromptTemplate: typeof stagePayload.current_prompt_template === 'string'
      ? stagePayload.current_prompt_template
      : '',
  }
}

export async function approveTranslationConfigReview(input: {
  jobId: string
  projectDir: string
  selectedModel: string
  promptTemplate: string
  savePrompt: boolean
}) {
  return webUiApiClient.post('/api/review/translation-config/approve', {
    body: {
      project_dir: input.projectDir,
      selected_model: input.selectedModel,
      prompt_template: input.promptTemplate,
      save_prompt: input.savePrompt,
    },
  })
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
