import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { EmptyState } from '@/components/EmptyState'
import { getReviewPageMessage } from '@/features/jobs/presentation'
import { ApiError } from '@/lib/api/client'
import { approveTranslationConfigReview, getTranslationConfigReview } from '@/lib/api/reviews'
import { usePollingTask } from '@/lib/react/usePollingTask'

interface TranslationConfigData {
  jobId: string
  projectDir: string
  segmentCount: number
  availableModels: Array<{ alias: string; provider: string; model_name: string }>
  currentModel: string
  currentPromptTemplate: string
}

export function TranslationConfigReviewPage() {
  const { jobId } = useParams<{ jobId: string }>()
  const [configData, setConfigData] = useState<TranslationConfigData | null>(null)
  const [selectedModel, setSelectedModel] = useState('')
  const [promptTemplate, setPromptTemplate] = useState('')
  const [savePrompt, setSavePrompt] = useState(false)
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)
  const [submitState, setSubmitState] = useState<'error' | 'idle' | 'submitted' | 'submitting'>(
    'idle',
  )
  const [submitMessage, setSubmitMessage] = useState<string | null>(null)

  const loadConfig = async (silent = false) => {
    if (!jobId || submitState === 'submitted') {
      return
    }

    if (!silent) {
      setIsLoading(true)
    }

    try {
      const data = await getTranslationConfigReview(jobId)
      setConfigData(data)
      if (!selectedModel && data.currentModel) {
        setSelectedModel(data.currentModel)
      }
      if (!promptTemplate && data.currentPromptTemplate) {
        setPromptTemplate(data.currentPromptTemplate)
      }
      setPageError(null)
    } catch (error) {
      setPageError(getErrorMessage(error))
    } finally {
      setIsLoading(false)
    }
  }

  usePollingTask(() => loadConfig(!isLoading), {
    intervalMs: 10000,
    enabled: submitState !== 'submitted',
  })

  const handleSubmit = async () => {
    if (!configData) {
      return
    }

    setSubmitState('submitting')
    setSubmitMessage(null)

    try {
      await approveTranslationConfigReview({
        jobId: configData.jobId,
        projectDir: configData.projectDir,
        selectedModel,
        promptTemplate,
        savePrompt,
      })
      setSubmitState('submitted')
      setSubmitMessage('翻译配置已确认，正在开始翻译...')
    } catch (error) {
      setSubmitState('error')
      setSubmitMessage(getErrorMessage(error))
    }
  }

  if (!jobId) {
    return <EmptyState description="缺少任务编号。" title="无法加载翻译配置" />
  }

  if (isLoading && !configData && !pageError) {
    return <EmptyState description="正在读取翻译配置信息。" title="翻译配置加载中" />
  }

  if (pageError && !configData) {
    return (
      <EmptyState
        actionLabel="返回当前任务"
        actionTo="/tasks/current"
        description={pageError}
        title="无法读取翻译配置"
      />
    )
  }

  if (submitState === 'submitted') {
    return (
      <div className="space-y-6">
        <section className="notice-panel border border-mint-500/20 bg-mint-500/8">
          <p className="text-sm font-semibold text-mint-700">翻译配置已确认</p>
          <p className="mt-2 text-sm text-mint-700/85">{submitMessage}</p>
        </section>
        <div className="flex flex-wrap gap-3">
          <Link className="primary-button" to="/tasks/current">
            查看当前任务
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <section className="surface-card p-6 lg:p-8">
        <div className="space-y-3">
          <p className="eyebrow">翻译配置</p>
          <h2 className="section-title">确认翻译配置</h2>
          <p className="muted-copy max-w-3xl">
            {getReviewPageMessage('translation_config_review', null)}
          </p>
        </div>
      </section>

      {pageError ? (
        <section className="notice-panel border border-coral-500/20 bg-coral-500/8">
          <p className="text-sm font-semibold text-coral-700">刷新失败</p>
          <p className="mt-2 text-sm text-coral-700/85">{pageError}</p>
        </section>
      ) : null}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.08fr)_minmax(320px,0.92fr)]">
        <div className="space-y-6">
          <section className="surface-card space-y-5 p-6">
            <div className="space-y-2">
              <label className="form-label" htmlFor="translation-model">
                翻译模型
              </label>
              <select
                className="form-input"
                disabled={submitState === 'submitting'}
                id="translation-model"
                onChange={(e) => setSelectedModel(e.target.value)}
                value={selectedModel}
              >
                {configData?.availableModels.map((model) => (
                  <option key={model.alias} value={model.alias}>
                    {model.model_name} ({model.provider})
                  </option>
                ))}
                {(!configData?.availableModels.length) ? (
                  <option value={selectedModel}>{selectedModel}</option>
                ) : null}
              </select>
            </div>

            <div className="space-y-2">
              <label className="form-label" htmlFor="prompt-template">
                翻译提示词
              </label>
              <textarea
                className="form-input min-h-[300px] font-mono text-xs leading-relaxed"
                disabled={submitState === 'submitting'}
                id="prompt-template"
                onChange={(e) => setPromptTemplate(e.target.value)}
                value={promptTemplate}
              />
              <p className="text-sm text-ink-900/60">
                提示词中的 __GROUPS_JSON__ 等占位符会在翻译时自动替换，请勿删除。
              </p>
            </div>

            <div className="flex items-center gap-3">
              <input
                checked={savePrompt}
                className="h-4 w-4 rounded border-ink-950/20"
                disabled={submitState === 'submitting'}
                id="save-prompt"
                onChange={(e) => setSavePrompt(e.target.checked)}
                type="checkbox"
              />
              <label className="text-sm text-ink-900/70" htmlFor="save-prompt">
                保存提示词到配置（下次新任务自动使用）
              </label>
            </div>

            {submitMessage && submitState === 'error' ? (
              <div className="notice-panel border border-coral-500/20 bg-coral-500/8 text-coral-700">
                {submitMessage}
              </div>
            ) : null}

            <div className="flex flex-wrap gap-3">
              <button
                className="primary-button"
                disabled={submitState === 'submitting'}
                onClick={handleSubmit}
                type="button"
              >
                {submitState === 'submitting' ? '确认中...' : '确认并开始翻译'}
              </button>
              <Link className="secondary-button" to="/tasks/current">
                返回当前任务
              </Link>
            </div>
          </section>
        </div>

        <div className="space-y-6">
          <section className="surface-card p-5">
            <div className="space-y-3">
              <h3 className="text-lg font-semibold text-ink-950">任务概览</h3>
              <div className="space-y-2 text-sm text-ink-900/68">
                <p>待翻译段落：{configData?.segmentCount ?? '-'} 段</p>
                <p>当前模型：{selectedModel || '-'}</p>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}

function getErrorMessage(error: unknown) {
  if (error instanceof ApiError) {
    return error.message
  }

  if (error instanceof Error) {
    return error.message
  }

  return '请求失败，请稍后重试。'
}
