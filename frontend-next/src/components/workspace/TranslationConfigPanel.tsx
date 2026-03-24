"use client"

import { useState } from 'react'

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

interface TranslationConfigPanelProps {
  jobId: string
  onAdvanced: () => void
}

export function TranslationConfigPanel({ jobId, onAdvanced }: TranslationConfigPanelProps) {
  const [configData, setConfigData] = useState<TranslationConfigData | null>(null)
  const [selectedModel, setSelectedModel] = useState('')
  const [promptTemplate, setPromptTemplate] = useState('')
  const [savePrompt, setSavePrompt] = useState(false)
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)
  const [submitState, setSubmitState] = useState<'error' | 'idle' | 'submitted' | 'submitting'>('idle')
  const [submitMessage, setSubmitMessage] = useState<string | null>(null)

  const loadConfig = async (silent = false) => {
    if (!jobId || submitState === 'submitted') return
    if (!silent) setIsLoading(true)
    try {
      const data = await getTranslationConfigReview(jobId)
      setConfigData(data)
      if (!selectedModel && data.currentModel) setSelectedModel(data.currentModel)
      if (!promptTemplate && data.currentPromptTemplate) setPromptTemplate(data.currentPromptTemplate)
      setPageError(null)
    } catch (error) {
      setPageError(getErrorMessage(error))
    } finally {
      setIsLoading(false)
    }
  }

  usePollingTask(() => loadConfig(!isLoading), { intervalMs: 10000, enabled: submitState !== 'submitted' })

  const handleSubmit = async () => {
    if (!configData) return
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
      onAdvanced()
    } catch (error) {
      setSubmitState('error')
      setSubmitMessage(getErrorMessage(error))
    }
  }

  if (isLoading && !configData && !pageError) {
    return <PanelLoading message="正在读取翻译配置信息..." />
  }
  if (pageError && !configData) {
    return <PanelError message={pageError} />
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          选择翻译模型并确认提示词，然后开始翻译。
        </p>
        <button
          className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:opacity-50"
          disabled={submitState === 'submitting'}
          onClick={() => { void handleSubmit() }}
          type="button"
        >
          {submitState === 'submitting' ? '确认中...' : '✓ 确认并开始翻译'}
        </button>
      </div>

      {pageError ? <ErrorBanner message={`刷新失败: ${pageError}`} /> : null}
      {submitMessage && submitState === 'error' ? <ErrorBanner message={submitMessage} /> : null}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(240px,0.3fr)]">
        <div className="space-y-5">
          {/* Model select */}
          <div>
            <span className="text-xs font-medium text-white/40 mb-2 block">翻译模型</span>
            <div className="group rounded-xl border border-white/8 bg-white/5 transition hover:border-primary/30 focus-within:border-primary/40">
              <select
                className="w-full rounded-xl bg-transparent px-4 py-3 text-sm text-white/90 focus:outline-none"
                disabled={submitState === 'submitting'}
                onChange={(e) => setSelectedModel(e.target.value)}
                value={selectedModel}
              >
                {configData?.availableModels.map((model) => (
                  <option key={model.alias} value={model.alias}>
                    {model.model_name} ({model.provider})
                  </option>
                ))}
                {!configData?.availableModels.length ? <option value={selectedModel}>{selectedModel}</option> : null}
              </select>
            </div>
          </div>

          {/* Prompt template */}
          <div>
            <span className="text-xs font-medium text-white/40 mb-2 block">翻译提示词</span>
            <div className="group rounded-xl border border-white/8 bg-white/5 transition hover:border-primary/30 focus-within:border-primary/40">
              <textarea
                className="w-full min-h-[300px] rounded-xl bg-transparent px-4 py-3 font-mono text-xs leading-relaxed text-white/90 placeholder:text-white/30 focus:outline-none"
                disabled={submitState === 'submitting'}
                onChange={(e) => setPromptTemplate(e.target.value)}
                value={promptTemplate}
              />
            </div>
            <p className="mt-2 text-xs text-white/30">
              提示词中的 __GROUPS_JSON__ 等占位符会在翻译时自动替换，请勿删除。
            </p>
          </div>

          {/* Save prompt checkbox */}
          <label className="flex items-center gap-3">
            <input
              checked={savePrompt}
              className="h-4 w-4 rounded border-white/20 bg-white/5"
              disabled={submitState === 'submitting'}
              onChange={(e) => setSavePrompt(e.target.checked)}
              type="checkbox"
            />
            <span className="text-sm text-white/50">保存提示词到配置（下次新任务自动使用）</span>
          </label>
        </div>

        {/* Sidebar */}
        <div>
          <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-5">
            <h3 className="text-lg font-semibold text-white/90">任务概览</h3>
            <div className="mt-3 space-y-2 text-sm text-white/50">
              <p>待翻译段落：{configData?.segmentCount ?? '-'} 段</p>
              <p>当前模型：{selectedModel || '-'}</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function PanelLoading({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="mb-4 h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      <p className="text-sm text-muted-foreground">{message}</p>
    </div>
  )
}

function PanelError({ message }: { message: string }) {
  return (
    <div className="rounded-2xl border border-red-500/20 bg-red-500/8 p-6 text-center">
      <p className="text-sm text-red-400">{message}</p>
    </div>
  )
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-2xl border border-red-500/20 bg-red-500/8 p-4 text-sm text-red-400">
      {message}
    </div>
  )
}

function getErrorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '请求失败，请稍后重试。'
}
