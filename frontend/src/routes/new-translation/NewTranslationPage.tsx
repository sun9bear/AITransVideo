import { useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { StatusBadge } from '@/components/StatusBadge'
import { getJobDisplayTitle, getStageLabel } from '@/features/jobs/presentation'
import { ApiError } from '@/lib/api/client'
import { estimateCosts, formatCostCny } from '@/lib/cost/estimator'
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
  const [sourceType, setSourceType] = useState<'youtube_url' | 'local_file'>('youtube_url')
  const [youtubeUrl, setYoutubeUrl] = useState('')
  const [uploadedFilePath, setUploadedFilePath] = useState('')
  const [uploadFileName, setUploadFileName] = useState('')
  const [isUploading, setIsUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState('')
  const [speakers, setSpeakers] = useState<'1' | '2' | 'auto'>('auto')
  const [voiceA, setVoiceA] = useState('')
  const [voiceB, setVoiceB] = useState('')
  const [transcriptionMethod, setTranscriptionMethod] = useState<'assemblyai' | 'gemini'>('assemblyai')
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

  const validationError = sourceType === 'youtube_url'
    ? validateYoutubeUrl(youtubeUrl)
    : (!uploadedFilePath ? '请先上传视频文件。' : null)
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
        youtubeUrl: sourceType === 'youtube_url' ? youtubeUrl.trim() : '',
        sourceType,
        localFilePath: sourceType === 'local_file' ? uploadedFilePath : undefined,
        transcriptionMethod: sourceType === 'local_file' ? 'assemblyai' : transcriptionMethod,
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

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(260px,0.35fr)]">
        <form className="surface-card space-y-6 p-6" onSubmit={handleSubmit}>
          <div className="space-y-2">
            <h3 className="text-xl font-semibold text-ink-950">任务输入</h3>
            <p className="text-sm leading-6 text-ink-900/70">
              首屏只保留当前创建任务必须填写的信息，避免在创建前分散注意力。
            </p>
          </div>

          <div className="space-y-3">
            <div className="flex gap-3">
              <button
                className={[
                  'rounded-full px-4 py-2 text-sm font-semibold transition',
                  sourceType === 'youtube_url'
                    ? 'bg-ink-950 text-white'
                    : 'bg-ink-950/5 text-ink-900/70 hover:bg-ink-950/10',
                ].join(' ')}
                onClick={() => setSourceType('youtube_url')}
                type="button"
              >
                YouTube 链接
              </button>
              <button
                className={[
                  'rounded-full px-4 py-2 text-sm font-semibold transition',
                  sourceType === 'local_file'
                    ? 'bg-ink-950 text-white'
                    : 'bg-ink-950/5 text-ink-900/70 hover:bg-ink-950/10',
                ].join(' ')}
                onClick={() => setSourceType('local_file')}
                type="button"
              >
                上传视频
              </button>
            </div>

            {sourceType === 'youtube_url' ? (
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
            ) : (
              <div className="space-y-2">
                <label className="form-label" htmlFor="video-upload">
                  选择视频文件
                </label>
                {uploadedFilePath ? (
                  <div className="flex items-center gap-3 rounded-2xl border border-mint-500/20 bg-mint-500/5 px-4 py-3">
                    <span className="text-sm font-medium text-mint-700">{uploadFileName}</span>
                    <button
                      className="text-xs text-ink-900/50 hover:text-coral-700"
                      onClick={() => {
                        setUploadedFilePath('')
                        setUploadFileName('')
                      }}
                      type="button"
                    >
                      移除
                    </button>
                  </div>
                ) : (
                  <input
                    accept="video/*"
                    className="form-input"
                    disabled={isBlockedByActiveJob || submitState === 'submitting' || isUploading}
                    id="video-upload"
                    onChange={async (event) => {
                      const file = event.target.files?.[0]
                      if (!file) return
                      setIsUploading(true)
                      setUploadProgress(`正在上传 ${file.name}...`)
                      try {
                        const formData = new FormData()
                        formData.append('file', file)
                        const response = await fetch('/web-ui-api/api/upload-video', {
                          method: 'POST',
                          body: formData,
                        })
                        if (!response.ok) {
                          const err = await response.json().catch(() => ({ error: '上传失败' }))
                          throw new Error(err.error || '上传失败')
                        }
                        const result = await response.json()
                        setUploadedFilePath(result.file_path)
                        setUploadFileName(file.name)
                        setUploadProgress('')
                      } catch (err) {
                        setUploadProgress(err instanceof Error ? err.message : '上传失败')
                      } finally {
                        setIsUploading(false)
                      }
                    }}
                    type="file"
                  />
                )}
                {uploadProgress ? (
                  <p className="text-sm text-ink-900/60">{uploadProgress}</p>
                ) : !uploadedFilePath ? (
                  <p className="text-sm text-ink-900/60">
                    支持 MP4、MOV、AVI 等常见视频格式，最大 2GB。
                  </p>
                ) : null}
              </div>
            )}
          </div>

          <div className="grid gap-4 md:grid-cols-4">
            <div className="space-y-2">
              <label className="form-label" htmlFor="transcription-method">
                转录方案
              </label>
              <select
                className="form-input"
                disabled={isBlockedByActiveJob || submitState === 'submitting'}
                id="transcription-method"
                onChange={(event) => {
                  setTranscriptionMethod(event.target.value as 'assemblyai' | 'gemini')
                }}
                value={transcriptionMethod}
              >
                <option value="assemblyai">AssemblyAI（音频上传转录）</option>
                <option value="gemini">Gemini 3.1 多模态（≤30分钟，无需上传）</option>
              </select>
            </div>

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
          <CostEstimatePanel transcriptionMethod={transcriptionMethod} />
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

function CostEstimatePanel({
  transcriptionMethod,
}: {
  transcriptionMethod: 'assemblyai' | 'gemini'
}) {
  const estimates = [3, 10, 30]

  return (
    <section className="surface-card p-5">
      <div className="space-y-2">
        <h3 className="text-lg font-semibold text-ink-950">费用预估</h3>
        <p className="text-sm text-ink-900/60">
          根据视频时长和当前选择的模型预估，仅供参考。
        </p>
      </div>
      <div className="mt-4 space-y-4">
        {estimates.map((minutes) => {
          const result = estimateCosts({
            videoDurationMinutes: minutes,
            transcriptionMethod,
            needsVoiceClone: true,
            speakerCount: 1,
          })
          return (
            <div key={minutes} className="rounded-2xl border border-ink-950/8 bg-sand-50/60 p-3">
              <p className="text-sm font-semibold text-ink-950">{minutes} 分钟视频</p>
              <div className="mt-2 space-y-1">
                {result.stages.map((stage) => (
                  <div key={stage.stage} className="flex justify-between text-xs text-ink-900/60">
                    <span>{stage.label}（{stage.model}）</span>
                    <span>{formatCostCny(stage.estimatedCostCny)}</span>
                  </div>
                ))}
                <div className="mt-1 flex justify-between border-t border-ink-950/8 pt-1 text-sm font-semibold text-ink-950">
                  <span>预估总计</span>
                  <span>{formatCostCny(result.totalCny)}</span>
                </div>
              </div>
            </div>
          )
        })}
      </div>
      <p className="mt-3 text-xs text-ink-900/45">
        实际费用取决于视频内容和处理结果，以上仅为参考值。已有音色可跳过克隆费用。
      </p>
    </section>
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
