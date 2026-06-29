import { getStageLabel } from '@/features/jobs/presentation'
import type { AppTranslator } from '@/features/jobs/i18n'
import type { JobStatus, PublicStage, StageProgressItem } from '@/types/jobs'

// UI-05：阶段描述文案迁入 messages/{zh,en}/app.json 的 `stageDescription.*`，
// 由 buildStageProgress 用 `t('stageDescription.' + key)` 解析。此处只保留阶段顺序（key 列表）。
const stageSequence = [
  'ingestion',
  'media_understanding',
  'speaker_review',
  'voice_review',
  'translation_config_review',
  'translation_review',
  'voice_selection_review',
  'draft',
  'legacy_process_output',
] as const satisfies readonly PublicStage[]

// 序列内的阶段键（窄于 PublicStage：不含 failed）——保证 `stageDescription.${key}`
// 落在 app.json 实际存在的键集合内，满足 next-intl 严格键类型。
type StageKey = (typeof stageSequence)[number]

export function buildStageProgress(
  t: AppTranslator,
  status: JobStatus,
  currentStage: PublicStage | null,
): StageProgressItem[] {
  const itemFor = (key: StageKey, state: StageProgressItem['state']): StageProgressItem => ({
    key,
    description: t(`stageDescription.${key}`),
    label: getStageLabel(t, key),
    state,
  })

  if ((status === 'queued' || status === 'running') && currentStage === null) {
    return stageSequence.map((key, index) => itemFor(key, index === 0 ? 'current' : 'upcoming'))
  }

  if (status === 'succeeded') {
    return stageSequence.map((key) => itemFor(key, 'complete'))
  }

  const activeIndex = currentStage
    ? stageSequence.findIndex((key) => key === currentStage)
    : -1

  return stageSequence.map((key, index) => {
    if (status === 'failed' && index === activeIndex) {
      return itemFor(key, 'error')
    }

    if (activeIndex >= 0) {
      if (index < activeIndex) {
        return itemFor(key, 'complete')
      }

      if (index === activeIndex) {
        return itemFor(key, 'current')
      }
    }

    return itemFor(key, 'upcoming')
  })
}
