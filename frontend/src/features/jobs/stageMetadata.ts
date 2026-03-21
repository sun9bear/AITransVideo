import { getStageLabel } from '@/features/jobs/presentation'
import type { JobStatus, PublicStage, StageProgressItem } from '@/types/jobs'

const stageSequence = [
  {
    description: '确认输入来源与可处理边界。',
    key: 'ingestion',
  },
  {
    description: '提取媒体信息并建立后续处理上下文。',
    key: 'media_understanding',
  },
  {
    description: '仅在需要时出现，确认说话人后才能继续。',
    key: 'speaker_review',
  },
  {
    description: '选择翻译模型和提示词。',
    key: 'translation_config_review',
  },
  {
    description: '仅在需要时出现，确认翻译后才能继续。',
    key: 'translation_review',
  },
  {
    description: '仅在需要时出现，确认音色后才能继续。',
    key: 'voice_review',
  },
  {
    description: '生成草稿、配音和结果摘要。',
    key: 'draft',
  },
  {
    description: '产出可查看结果和可下载文件。',
    key: 'legacy_process_output',
  },
] as const satisfies readonly {
  description: string
  key: PublicStage
}[]

export function buildStageProgress(
  status: JobStatus,
  currentStage: PublicStage | null,
): StageProgressItem[] {
  if ((status === 'queued' || status === 'running') && currentStage === null) {
    return stageSequence.map((stage, index) => ({
      ...stage,
      label: getStageLabel(stage.key),
      state: index === 0 ? 'current' : 'upcoming',
    }))
  }

  if (status === 'succeeded') {
    return stageSequence.map((stage) => ({
      ...stage,
      label: getStageLabel(stage.key),
      state: 'complete',
    }))
  }

  const activeIndex = currentStage
    ? stageSequence.findIndex((stage) => stage.key === currentStage)
    : -1

  return stageSequence.map((stage, index) => {
    if (status === 'failed' && index === activeIndex) {
      return {
        ...stage,
        label: getStageLabel(stage.key),
        state: 'error',
      }
    }

    if (activeIndex >= 0) {
      if (index < activeIndex) {
        return {
          ...stage,
          label: getStageLabel(stage.key),
          state: 'complete',
        }
      }

      if (index === activeIndex) {
        return {
          ...stage,
          label: getStageLabel(stage.key),
          state: 'current',
        }
      }
    }

    return {
      ...stage,
      label: getStageLabel(stage.key),
      state: 'upcoming',
    }
  })
}
