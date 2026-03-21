import { useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { ConfigSummaryCard } from '@/components/ConfigSummaryCard'
import { EmptyState } from '@/components/EmptyState'
import { StatusBadge } from '@/components/StatusBadge'
import { getJobDisplayTitle, getStageLabel } from '@/features/jobs/presentation'
import { ApiError } from '@/lib/api/client'
import { getCurrentJob, submitTranslationJob } from '@/lib/api/jobs'
import { getVoiceLibrary, type VoiceLibraryEntry } from '@/lib/api/voiceLibrary'
import { usePollingTask } from '@/lib/react/usePollingTask'
import type { JobSummary } from '@/types/jobs'

const speakerOptions = [
  { label: '自动', value: 'auto' },
  { label: '1 人', value: '1' },
  { label: '2 人', value: '2' },
] as const

const MANUAL_INPUT = '__manual__'

export function NewTranslationPage() {
  const navigate = useNavigate()
  const [youtubeUrl, setYoutubeUrl] = useState('')
  const [speakers, setSpeakers] = useState<'1' | '2' | 'auto'>('auto')
  const [voiceA, setVoiceA] = useState('')
  const [voiceB, setVoiceB] = useState('')
  const [voiceAMode, setVoiceAMode] = useState<'select' | 'manual'>('select')
  const [voiceBMode, setVoiceBMode] = useState<'select' | 'manual'>('select')
  const [savedVoices, setSavedVoices] = useState<VoiceLibraryEntry[]>([])
  const [activeJob, setActiveJob] = useState<JobSummary | null>(null)
  const [isLoadingGuard, setIsLoadingGuard] = useState(true)
  const [guardError, setGuardError] = useState<string | null>(null)
  const [submitState, setSubmitState] = useState<'error' | 'idle' | 'submitting' | 'success'>(
    'idle',
  )
  const [submitMessage, setSubmitMessage] = useState<string | null>(null)

  const validationError = validateYoutubeUrl(youtubeUrl)
  const isBlockedByActiveJob = Boolean(activeJob)

  const loadActiveJob = async (silent = false) => {
    if (!silent) {
      setIsLoadingGuard(true)
    }

    try {
      const nextJob = await getCurrentJob()
      setActiveJob(nextJob)
      setGuardError(null)
    } catch (error) {
      setGuardError(getErrorMessage(error))
    } finally {
      setIsLoadingGuard(false)
    }
  }

  usePollingTask(() => loadActiveJob(!isLoadingGuard), {
    intervalMs: 5000,
  })

  useEffect(() => {
    getVoiceLibrary()
      .then((lib) => {
        setSavedVoices(lib.voices)
      })
      .catch(() => {
        // Voice library is non-critical for creation
      })
  }, [])

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    if (validationError) {
      setSubmitState('error')
      setSubmitMessage(validationError)
      return
    }

    setSubmitState('submitting')
    setSubmitMessage(null)

    try {
      const createdJob = await submitTranslationJob({
        speakers,
        voiceA: normalizeOptionalText(voiceA),
        voiceB: normalizeOptionalText(voiceB),
        youtubeUrl: youtubeUrl.trim(),
      })

      setActiveJob(createdJob)
      setSubmitState('success')
      setSubmitMessage(`任务已创建：${getJobDisplayTitle(createdJob)}`)
      navigate('/tasks/current')
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await loadActiveJob(true)
      }

      setSubmitState('error')
      setSubmitMessage(getErrorMessage(error))
    }
  }

  return (
    <div className="space-y-6">
      <section className="surface-card p-6 lg:p-8">
        <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-3">
            <h2 className="section-title">新建翻译</h2>
            <p className="muted-copy max-w-3xl">
              先填写 YouTube 链接与任务参数，再直接创建任务。创建成功后会自动跳转到“当前任务”继续跟进。
            </p>
          </div>
          {activeJob ? <StatusBadge status={activeJob.status} /> : null}
        </div>
      </section>

      {guardError ? (
        <section className="notice-panel border border-coral-500/20 bg-coral-500/8">
          <p className="text-sm font-semibold text-coral-700">读取当前任务失败</p>
          <p className="mt-2 text-sm text-coral-700/85">{guardError}</p>
        </section>
      ) : null}

      {activeJob ? (
        <section className="surface-card p-6">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="space-y-2">
              <p className="eyebrow">当前已有任务</p>
              <h3 className="text-xl font-semibold text-ink-950">
                当前已有进行中的任务
              </h3>
              <p className="text-sm leading-6 text-ink-900/70">
                这一版仍按单活跃任务运行。请先处理当前任务，或等待它完成后再创建新的翻译任务。
              </p>
              <p className="text-sm text-ink-900/62">
                {getJobDisplayTitle(activeJob)} · {getStageLabel(activeJob.currentStage)}
              </p>
            </div>
            <Link className="primary-button" to="/tasks/current">
              查看当前任务
            </Link>
          </div>
        </section>
      ) : (
        <section className="surface-card p-5">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div className="space-y-1">
              <p className="eyebrow">当前任务状态</p>
              <h3 className="text-lg font-semibold text-ink-950">可以直接创建新任务</h3>
              <p className="muted-copy">
                当前没有进行中的任务，填写链接后即可开始。
              </p>
            </div>
            <span className="rounded-full bg-mint-500/12 px-4 py-2 text-sm font-semibold text-mint-700">
              {isLoadingGuard ? '正在检查当前任务...' : '当前可创建'}
            </span>
          </div>
        </section>
      )}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.08fr)_minmax(320px,0.92fr)]">
        <form className="surface-card space-y-6 p-6" onSubmit={handleSubmit}>
          <div className="space-y-2">
            <h3 className="text-xl font-semibold text-ink-950">任务输入</h3>
            <p className="text-sm leading-6 text-ink-900/70">
              首屏只保留当前创建任务必须填写的信息，避免在创建前分散注意力。
            </p>
          </div>

          <div className="space-y-2">
            <label className="form-label" htmlFor="youtube-url">
              YouTube 链接
            </label>
            <input
              className="form-input"
              disabled={isBlockedByActiveJob || submitState === 'submitting'}
              id="youtube-url"
              onChange={(event) => {
                setYoutubeUrl(event.target.value)
                if (submitState !== 'idle') {
                  setSubmitState('idle')
                  setSubmitMessage(null)
                }
              }}
              placeholder="https://www.youtube.com/watch?v=..."
              type="url"
              value={youtubeUrl}
            />
            <p
              className={[
                'text-sm',
                validationError ? 'text-coral-700' : 'text-ink-900/60',
              ].join(' ')}
            >
              {validationError ?? '当前只支持有效的 YouTube 链接。'}
            </p>
          </div>

          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-2">
              <label className="form-label" htmlFor="speakers">
                说话人数
              </label>
              <select
                className="form-input"
                disabled={isBlockedByActiveJob || submitState === 'submitting'}
                id="speakers"
                onChange={(event) => {
                  setSpeakers(event.target.value as '1' | '2' | 'auto')
                }}
                value={speakers}
              >
                {speakerOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>

            <VoiceField
              disabled={isBlockedByActiveJob || submitState === 'submitting'}
              id="voice-a"
              label="音色 A"
              mode={voiceAMode}
              onChange={setVoiceA}
              onModeChange={setVoiceAMode}
              savedVoices={savedVoices}
              value={voiceA}
            />

            <VoiceField
              disabled={isBlockedByActiveJob || submitState === 'submitting'}
              id="voice-b"
              label="音色 B"
              mode={voiceBMode}
              onChange={setVoiceB}
              onModeChange={setVoiceBMode}
              savedVoices={savedVoices}
              value={voiceB}
            />
          </div>

          {savedVoices.length > 0 ? (
            <p className="text-sm text-ink-900/60">
              选择已有音色可节省克隆费用（每次克隆 ¥9.9）。
            </p>
          ) : null}

          {submitMessage ? (
            <div
              className={[
                'notice-panel',
                submitState === 'error'
                  ? 'border border-coral-500/20 bg-coral-500/8 text-coral-700'
                  : 'border border-mint-500/20 bg-mint-500/8 text-mint-700',
              ].join(' ')}
            >
              {submitMessage}
            </div>
          ) : null}

          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <button
              className="primary-button"
              disabled={
                Boolean(validationError) ||
                isBlockedByActiveJob ||
                submitState === 'submitting' ||
                isLoadingGuard
              }
              type="submit"
            >
              {submitState === 'submitting' ? '创建中...' : '创建任务'}
            </button>
            {submitState === 'success' ? (
              <Link className="secondary-button" to="/tasks/current">
                查看当前任务
              </Link>
            ) : null}
            {isLoadingGuard ? (
              <span className="text-sm text-ink-900/60">正在检查是否已有进行中的任务...</span>
            ) : null}
          </div>
        </form>

        <div className="space-y-6">
          <ConfigSummaryCard
            description="创建页首屏只保留任务能否立即开始以及下一步会发生什么。"
            items={[
              {
                label: '输入类型',
                value: 'YouTube 链接',
                hint: '当前只支持公开视频链接。',
              },
              {
                label: '任务模式',
                value: '单活跃任务',
                hint: '创建前会先检查当前是否已有进行中的任务。',
              },
              {
                label: '音色输入',
                value: '按任务填写',
                hint: '音色 A / 音色 B 可留空，交给后端现有逻辑处理。',
              },
              {
                label: '创建后下一步',
                value: '自动跳转到当前任务',
                hint: '审核和进展都会集中在当前任务页提示。',
              },
            ]}
            title="创建前说明"
          />

          {!activeJob && !guardError && !isLoadingGuard ? (
            <EmptyState
              description="首屏已经满足创建任务所需信息，填写链接后即可开始。"
              title="准备就绪"
            />
          ) : null}
        </div>
      </div>
    </div>
  )
}

function VoiceField({
  disabled,
  id,
  label,
  mode,
  onChange,
  onModeChange,
  savedVoices,
  value,
}: {
  disabled: boolean
  id: string
  label: string
  mode: 'manual' | 'select'
  onChange: (value: string) => void
  onModeChange: (mode: 'manual' | 'select') => void
  savedVoices: VoiceLibraryEntry[]
  value: string
}) {
  const clonedVoices = savedVoices.filter((v) => v.voiceType === 'cloned')
  const builtinVoices = savedVoices.filter((v) => v.voiceType !== 'cloned')
  const hasVoices = savedVoices.length > 0

  if (!hasVoices || mode === 'manual') {
    return (
      <div className="space-y-2">
        <label className="form-label" htmlFor={id}>
          {label}
        </label>
        <input
          className="form-input"
          disabled={disabled}
          id={id}
          onChange={(event) => {
            onChange(event.target.value)
          }}
          placeholder="可留空"
          type="text"
          value={value}
        />
        {hasVoices ? (
          <button
            className="text-xs text-sky-700 hover:underline"
            onClick={() => {
              onModeChange('select')
              onChange('')
            }}
            type="button"
          >
            从已有音色选择
          </button>
        ) : null}
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <label className="form-label" htmlFor={id}>
        {label}
      </label>
      <select
        className="form-input"
        disabled={disabled}
        id={id}
        onChange={(event) => {
          if (event.target.value === MANUAL_INPUT) {
            onModeChange('manual')
            onChange('')
            return
          }
          onChange(event.target.value)
        }}
        value={value}
      >
        <option value="">可留空（自动处理）</option>
        {clonedVoices.length > 0 ? (
          <optgroup label="克隆音色">
            {clonedVoices.map((v) => (
              <option key={v.voiceId} value={v.voiceId}>
                {v.label ?? v.voiceId}
                {v.speakerName ? ` (${v.speakerName})` : ''}
              </option>
            ))}
          </optgroup>
        ) : null}
        {builtinVoices.length > 0 ? (
          <optgroup label="内置音色">
            {builtinVoices.map((v) => (
              <option key={v.voiceId} value={v.voiceId}>
                {v.label ?? v.voiceId}
              </option>
            ))}
          </optgroup>
        ) : null}
        <option value={MANUAL_INPUT}>手动输入 Voice ID...</option>
      </select>
    </div>
  )
}

function validateYoutubeUrl(value: string) {
  const normalizedValue = value.trim()

  if (!normalizedValue) {
    return '请输入 YouTube 链接。'
  }

  try {
    const url = new URL(normalizedValue)
    const host = url.hostname.toLowerCase()
    const isYoutubeHost =
      host === 'youtu.be' ||
      host.endsWith('youtube.com') ||
      host.endsWith('youtube-nocookie.com')

    if (!['http:', 'https:'].includes(url.protocol) || !isYoutubeHost) {
      return '当前只支持有效的 YouTube 链接。'
    }

    return null
  } catch {
    return '请输入有效的链接。'
  }
}

function normalizeOptionalText(value: string) {
  const normalizedValue = value.trim()
  return normalizedValue || undefined
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
