import type { ErrorSummary, JobSummary, PublicStage, ReviewGate } from '@/types/jobs'

const stageLabels = {
  draft: '草稿与配音',
  failed: '处理失败',
  ingestion: '输入准备',
  legacy_process_output: '输出完成',
  media_understanding: '媒体理解',
  speaker_review: '说话人审核',
  translation_config_review: '翻译配置',
  translation_review: '翻译审核',
  voice_review: '音色确认',
  voice_selection_review: '音色选择',
} as const satisfies Record<PublicStage, string>

const reviewStageDescriptions = {
  speaker_review: '请先确认说话人名称和片段归属，然后继续下一步。',
  translation_config_review: '请选择翻译模型并确认提示词，然后开始翻译。',
  translation_review: '请先确认翻译内容，然后继续配音与后续处理。',
  voice_review: '请先确认每位说话人的音色绑定，然后继续下一步。',
  voice_selection_review: '请为每位说话人选择或克隆配音音色，然后继续下一步。',
} as const

const reviewStageTabs = {
  speaker_review: 'review',
  translation_config_review: 'translation-config',
  translation_review: 'translation',
  voice_review: 'voice-library',
  voice_selection_review: 'voice-selection',
} as const

const sanitizedProgressMessages = new Map<string, string>([
  ['Job completed successfully.', '任务已完成。'],
  ['Job queued.', '任务已进入队列。'],
  ['Reviewing speaker labels...', '正在处理说话人审核结果。'],
  ['Starting process-backed localization job.', '任务已开始处理。'],
])

type ReviewStage = keyof typeof reviewStageDescriptions

export function getStageLabel(stage: PublicStage | null) {
  if (!stage) {
    return '待开始'
  }

  return stageLabels[stage]
}

export function getReviewPrompt(reviewGate: ReviewGate | null, stage: PublicStage | null) {
  const normalizedStage = normalizeReviewStage(reviewGate?.stage) ?? normalizeReviewStage(stage)
  const message = sanitizeUserFacingMessage(normalizeText(reviewGate?.message), normalizedStage)

  return {
    message: message ?? normalizedStageDescription(normalizedStage),
    stage: normalizedStage,
    title: normalizedStage ? getStageLabel(normalizedStage) : '等待审核',
  }
}

export function getReviewAction(reviewGate: ReviewGate | null, stage: PublicStage | null) {
  return getReviewActionForJob(reviewGate, stage, null)
}

export function getReviewActionForJob(
  reviewGate: ReviewGate | null,
  stage: PublicStage | null,
  jobId: string | null,
) {
  // Check reviewGate.tab to detect mismatched stage (e.g., voice_review stage but translation-config tab)
  const gateTab = typeof reviewGate?.tab === 'string' ? reviewGate.tab : null
  const tabToStage: Record<string, ReviewStage> = {
    'translation-config': 'translation_config_review',
    'review': 'speaker_review',
    'translation': 'translation_review',
    'voice': 'voice_review',
    'voice-selection': 'voice_selection_review',
  }
  const stageFromTab = gateTab ? tabToStage[gateTab] : null
  const gateStage = normalizeReviewStage(reviewGate?.stage)
  // If gate tab suggests a different stage than gate stage, prefer tab (fixes pipeline mismatch)
  const resolvedGateStage = (stageFromTab && gateStage && stageFromTab !== gateStage)
    ? stageFromTab
    : gateStage
  const normalizedStage = resolvedGateStage ?? normalizeReviewStage(stage)
  const fallbackHref = '/workspace'
  const nativeRoute = buildNativeReviewRoute(normalizedStage, jobId)
  const href = nativeRoute ?? fallbackHref
  const stageLabel = normalizedStage ? getStageLabel(normalizedStage) : '审核'

  return {
    description: normalizedStage
      ? `当前任务正在等待${stageLabel}，请先完成处理。`
      : '当前任务正在等待审核处理。',
    fallbackHref,
    href,
    label: normalizedStage ? `处理${stageLabel}` : '继续处理审核',
    nativeRoute,
    tab: normalizedStage ? reviewStageTabs[normalizedStage] : 'run',
  }
}

export function getErrorSummaryMessage(errorSummary: ErrorSummary | null) {
  const message = normalizeText(errorSummary?.message)
  const errorType = normalizeText(errorSummary?.error_type)
  const stage = normalizeText(errorSummary?.stage)

  if (message) {
    return getUserFacingProgressMessage(message) ?? message
  }

  return [stage, errorType].filter(Boolean).join(' / ') || '当前没有更多失败说明。'
}

export function getErrorCategory(errorSummary: ErrorSummary | null): {
  label: string
  suggestion: string
} {
  const errorType = normalizeText(errorSummary?.error_type)?.toLowerCase() ?? ''
  const stage = normalizeText(errorSummary?.stage)?.toLowerCase() ?? ''
  const message = normalizeText(errorSummary?.message)?.toLowerCase() ?? ''

  if (
    stage.includes('ingestion') ||
    errorType.includes('transcription') ||
    errorType.includes('upload') ||
    message.includes('transcription') ||
    message.includes('upload')
  ) {
    return {
      label: '音频上传或转录失败',
      suggestion: '长视频容易上传失败，建议使用 10 分钟以内的视频重试。',
    }
  }

  if (errorType.includes('voiceclone') || errorType.includes('voice_clone')) {
    return {
      label: '音色克隆失败',
      suggestion: '可能是音频质量不足，建议检查原视频音频是否清晰。',
    }
  }

  if (
    stage.includes('translation') ||
    errorType.includes('translation')
  ) {
    return {
      label: '翻译生成失败',
      suggestion: '可能是模型服务临时不可用，建议稍后重试。',
    }
  }

  if (
    errorType.includes('alignment') ||
    errorType.includes('dsp') ||
    errorType.includes('duration') ||
    stage.includes('draft')
  ) {
    return {
      label: '时长对齐失败',
      suggestion: '配音时长与原始片段差距过大，可尝试重新创建任务。',
    }
  }

  return {
    label: '处理失败',
    suggestion: '请查看项目详情了解更多信息。',
  }
}

export function getJobDisplayTitle(job: Pick<JobSummary, 'projectDir' | 'sourceRef' | 'title'>) {
  const sourceVideoId = extractYoutubeVideoId(job.sourceRef)
  const normalizedTitle = normalizeText(job.title)
  const projectSlug = extractProjectSlug(job.projectDir)
  const slugTitle = projectSlug ? humanizeSlug(projectSlug) : null

  if (slugTitle && (!normalizedTitle || normalizedTitle === sourceVideoId)) {
    return slugTitle
  }

  return normalizedTitle ?? slugTitle ?? sourceVideoId ?? '未命名视频'
}

export function getJobSecondaryLabel(job: Pick<JobSummary, 'projectDir' | 'sourceRef'>) {
  const sourceVideoId = extractYoutubeVideoId(job.sourceRef)
  if (sourceVideoId) {
    return `YouTube 视频 · ${sourceVideoId}`
  }

  const projectSlug = extractProjectSlug(job.projectDir)
  if (projectSlug) {
    return `项目记录 · ${humanizeSlug(projectSlug) ?? projectSlug}`
  }

  return job.sourceRef
}

export function getUserFacingProgressMessage(message: string | null | undefined) {
  const normalizedMessage = normalizeText(message)
  if (!normalizedMessage) {
    return null
  }

  if (
    normalizedMessage.includes('Web UI') ||
    normalizedMessage.includes('fallback') ||
    normalizedMessage.includes('legacy')
  ) {
    return null
  }

  return sanitizedProgressMessages.get(normalizedMessage) ?? normalizedMessage
}

export function getReviewPageMessage(stage: ReviewStage, message: string | null | undefined) {
  return sanitizeUserFacingMessage(message, stage) ?? reviewStageDescriptions[stage]
}

function buildNativeReviewRoute(stage: ReviewStage | null, jobId: string | null) {
  if (!jobId) {
    return null
  }

  // All review stages now route to the unified workspace
  if (stage === 'speaker_review' || stage === 'translation_config_review' || stage === 'translation_review' || stage === 'voice_review' || stage === 'voice_selection_review') {
    return `/workspace/${jobId}`
  }

  return null
}

function sanitizeUserFacingMessage(
  message: string | null | undefined,
  stage: ReviewStage | null,
) {
  const normalizedMessage = getUserFacingProgressMessage(message)
  if (!normalizedMessage) {
    return null
  }

  if (normalizedMessage.includes('旧版') || normalizedMessage.includes('审核页')) {
    return normalizedStageDescription(stage)
  }

  return normalizedMessage
}

function normalizedStageDescription(stage: ReviewStage | null) {
  if (!stage) {
    return '当前任务需要先完成审核，然后才能继续。'
  }

  return reviewStageDescriptions[stage]
}

function normalizeReviewStage(value: unknown): ReviewStage | null {
  if (
    value === 'speaker_review' ||
    value === 'translation_config_review' ||
    value === 'translation_review' ||
    value === 'voice_review' ||
    value === 'voice_selection_review'
  ) {
    return value
  }

  return null
}

function extractYoutubeVideoId(sourceRef: string | null | undefined) {
  if (!sourceRef) {
    return null
  }

  try {
    const url = new URL(sourceRef)
    if (url.hostname === 'youtu.be') {
      return normalizeText(url.pathname.replace(/^\/+/, ''))
    }

    return normalizeText(url.searchParams.get('v'))
  } catch {
    return null
  }
}

function extractProjectSlug(projectDir: string | null | undefined) {
  const normalizedPath = normalizeText(projectDir)
  if (!normalizedPath) {
    return null
  }

  const segments = normalizedPath.replaceAll('\\', '/').split('/').filter(Boolean)
  return normalizeText(segments.at(-1))
}

function humanizeSlug(value: string) {
  const words = value
    .split(/[_-]+/)
    .map((part) => part.trim())
    .filter(Boolean)

  if (words.length === 0) {
    return null
  }

  return words
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

function normalizeText(value: unknown) {
  if (typeof value !== 'string') {
    return null
  }

  const normalizedValue = value.trim()
  return normalizedValue || null
}
