import type { ErrorSummary, JobSummary, PublicStage, ReviewGate } from '@/types/jobs'
import type { AppTranslator } from '@/features/jobs/i18n'

const reviewStageTabs = {
  speaker_review: 'review',
  translation_config_review: 'translation-config',
  translation_review: 'translation',
  voice_review: 'voice-library',
  voice_selection_review: 'voice-selection',
} as const

type ReviewStage =
  | 'speaker_review'
  | 'translation_config_review'
  | 'translation_review'
  | 'voice_review'
  | 'voice_selection_review'

/**
 * UI-05：后端 EN 进度串 → app.progress.* 字典键的映射。lookup 仍以后端 EN 串为键
 * （与改造前 sanitizedProgressMessages 完全一致），命中后由 translator 解析为本地化文案
 * （zh 字节一致，en 自然英文）。**保留 null 过滤语义**：含 Web UI/fallback/legacy 仍返回 null。
 */
// value 类型 = translator 接受的 namespaced 键（窄于 string），让 `t(key)` 满足 next-intl 严格键类型。
type AppMessageKey = Parameters<AppTranslator>[0]

const PROGRESS_MESSAGE_KEYS = new Map<string, AppMessageKey>([
  ['Job completed successfully.', 'progress.completed'],
  ['Job queued.', 'progress.queued'],
  ['Reviewing speaker labels...', 'progress.reviewingSpeakers'],
  ['Starting process-backed localization job.', 'progress.starting'],
])

export function getStageLabel(t: AppTranslator, stage: PublicStage | null) {
  if (!stage) {
    return t('stage.none')
  }

  return t(`stage.${stage}`)
}

export function getReviewPrompt(
  t: AppTranslator,
  reviewGate: ReviewGate | null,
  stage: PublicStage | null,
) {
  const normalizedStage = normalizeReviewStage(reviewGate?.stage) ?? normalizeReviewStage(stage)
  const message = sanitizeUserFacingMessage(t, normalizeText(reviewGate?.message), normalizedStage)

  return {
    message: message ?? normalizedStageDescription(t, normalizedStage),
    stage: normalizedStage,
    title: normalizedStage ? getStageLabel(t, normalizedStage) : t('review.pendingTitle'),
  }
}

export function getReviewAction(
  t: AppTranslator,
  reviewGate: ReviewGate | null,
  stage: PublicStage | null,
) {
  return getReviewActionForJob(t, reviewGate, stage, null)
}

export function getReviewActionForJob(
  t: AppTranslator,
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
  const stageLabel = normalizedStage ? getStageLabel(t, normalizedStage) : t('review.genericStage')

  return {
    description: normalizedStage
      ? t('review.descriptionForStage', { stageLabel })
      : t('review.descriptionGeneric'),
    fallbackHref,
    href,
    label: normalizedStage ? t('review.actionForStage', { stageLabel }) : t('review.actionGeneric'),
    nativeRoute,
    tab: normalizedStage ? reviewStageTabs[normalizedStage] : 'run',
  }
}

export function getErrorSummaryMessage(t: AppTranslator, errorSummary: ErrorSummary | null) {
  const message = normalizeText(errorSummary?.message)
  const errorType = normalizeText(errorSummary?.error_type)
  const stage = normalizeText(errorSummary?.stage)

  if (message) {
    return getUserFacingProgressMessage(t, message) ?? message
  }

  return [stage, errorType].filter(Boolean).join(' / ') || t('error.noDetail')
}

export function getErrorCategory(t: AppTranslator, errorSummary: ErrorSummary | null): {
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
      label: t('error.ingestion.label'),
      suggestion: t('error.ingestion.suggestion'),
    }
  }

  if (errorType.includes('voiceclone') || errorType.includes('voice_clone')) {
    return {
      label: t('error.voiceclone.label'),
      suggestion: t('error.voiceclone.suggestion'),
    }
  }

  if (
    stage.includes('translation') ||
    errorType.includes('translation')
  ) {
    return {
      label: t('error.translation.label'),
      suggestion: t('error.translation.suggestion'),
    }
  }

  if (
    errorType.includes('alignment') ||
    errorType.includes('dsp') ||
    errorType.includes('duration') ||
    stage.includes('draft')
  ) {
    return {
      label: t('error.alignment.label'),
      suggestion: t('error.alignment.suggestion'),
    }
  }

  return {
    label: t('error.generic.label'),
    suggestion: t('error.generic.suggestion'),
  }
}

export function getJobDisplayTitle(
  t: AppTranslator,
  job: Pick<JobSummary, 'projectDir' | 'sourceRef' | 'title'>,
) {
  const sourceVideoId = extractYoutubeVideoId(job.sourceRef)
  const normalizedTitle = discardGeneratedJobId(normalizeText(job.title))
  const projectSlug = extractProjectSlug(job.projectDir)
  const safeProjectSlug = discardGeneratedJobId(projectSlug)
  const slugTitle = safeProjectSlug ? humanizeSlug(safeProjectSlug) : null

  if (slugTitle && (!normalizedTitle || normalizedTitle === sourceVideoId)) {
    return slugTitle
  }

  // R5 content passthrough：job 标题/slug 本身是 content 透传，只有「未命名视频」fallback 是 chrome。
  return normalizedTitle ?? slugTitle ?? t('title.untitled')
}

export function getJobSecondaryLabel(
  t: AppTranslator,
  job: Pick<JobSummary, 'projectDir' | 'sourceRef'>,
) {
  const sourceVideoId = extractYoutubeVideoId(job.sourceRef)
  if (sourceVideoId) {
    // chrome 前缀本地化；id 本身是 content 透传。
    return t('secondary.youtubePrefix', { id: sourceVideoId })
  }

  const projectSlug = extractProjectSlug(job.projectDir)
  if (projectSlug) {
    return t('secondary.projectPrefix', { id: humanizeSlug(projectSlug) ?? projectSlug })
  }

  return job.sourceRef
}

export function getUserFacingProgressMessage(
  t: AppTranslator,
  message: string | null | undefined,
) {
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

  const key = PROGRESS_MESSAGE_KEYS.get(normalizedMessage)
  return key ? t(key) : normalizedMessage
}

export function getReviewPageMessage(
  t: AppTranslator,
  stage: ReviewStage,
  message: string | null | undefined,
) {
  return sanitizeUserFacingMessage(t, message, stage) ?? t(`reviewStageDescription.${stage}`)
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
  t: AppTranslator,
  message: string | null | undefined,
  stage: ReviewStage | null,
) {
  const normalizedMessage = getUserFacingProgressMessage(t, message)
  if (!normalizedMessage) {
    return null
  }

  if (normalizedMessage.includes('旧版') || normalizedMessage.includes('审核页')) {
    return normalizedStageDescription(t, stage)
  }

  return normalizedMessage
}

function normalizedStageDescription(t: AppTranslator, stage: ReviewStage | null) {
  if (!stage) {
    return t('review.needsReviewFirst')
  }

  return t(`reviewStageDescription.${stage}`)
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

function discardGeneratedJobId(value: string | null) {
  if (!value) {
    return null
  }
  return /^job[_-][a-f0-9]{24,}$/i.test(value) ? null : value
}
