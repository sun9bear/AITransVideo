import { useState } from 'react'

import { EmptyState } from '@/components/EmptyState'
import { ApiError } from '@/lib/api/client'
import { getVoiceLibrary, type VoiceLibraryEntry, type VoiceLibrarySummary } from '@/lib/api/voiceLibrary'
import { usePollingTask } from '@/lib/react/usePollingTask'

export function VoiceLibraryPage() {
  const [library, setLibrary] = useState<VoiceLibrarySummary | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)

  const loadLibrary = async (silent = false) => {
    if (!silent) {
      setIsLoading(true)
    }

    try {
      const next = await getVoiceLibrary()
      setLibrary(next)
      setPageError(null)
    } catch (error) {
      setPageError(getErrorMessage(error))
    } finally {
      setIsLoading(false)
    }
  }

  usePollingTask(() => loadLibrary(!isLoading), {
    intervalMs: 15000,
  })

  if (isLoading && !library && !pageError) {
    return (
      <EmptyState
        description="正在读取音色库信息。"
        title="我的音色加载中"
      />
    )
  }

  if (pageError && !library) {
    return (
      <EmptyState
        actionLabel="新建翻译"
        actionTo="/translations/new"
        description={pageError}
        title="无法读取音色库"
      />
    )
  }

  if (!library || library.voices.length === 0) {
    return (
      <EmptyState
        actionLabel="新建翻译"
        actionTo="/translations/new"
        description="还没有保存的音色。完成第一个翻译任务后，克隆的音色会出现在这里。"
        title="我的音色"
      />
    )
  }

  return (
    <div className="space-y-6">
      <section className="surface-card p-6 lg:p-8">
        <div className="space-y-3">
          <p className="eyebrow">音色管理</p>
          <h2 className="section-title">我的音色</h2>
          <p className="muted-copy max-w-3xl">
            复用已有音色可节省克隆费用（每次克隆 ¥9.9）。点击复制按钮获取 Voice ID，在新建翻译时粘贴到音色字段即可。
          </p>
        </div>

        <div className="mt-6 grid gap-4 md:grid-cols-3">
          <MetricTile label="音色总数" value={String(library.voices.length)} />
          <MetricTile label="克隆音色" value={String(library.clonedVoiceCount)} />
          <MetricTile label="内置音色" value={String(library.builtinVoiceCount)} />
        </div>
      </section>

      {pageError ? (
        <section className="notice-panel border border-coral-500/20 bg-coral-500/8">
          <p className="text-sm font-semibold text-coral-700">刷新音色库失败</p>
          <p className="mt-2 text-sm text-coral-700/85">{pageError}</p>
        </section>
      ) : null}

      <section className="space-y-4">
        {library.voices.map((voice) => (
          <VoiceCard key={voice.voiceId} voice={voice} />
        ))}
      </section>
    </div>
  )
}

function VoiceCard({ voice }: { voice: VoiceLibraryEntry }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(voice.voiceId)
      setCopied(true)
      setTimeout(() => {
        setCopied(false)
      }, 2000)
    } catch {
      // Fallback: select text for manual copy
      const textArea = document.createElement('textarea')
      textArea.value = voice.voiceId
      document.body.appendChild(textArea)
      textArea.select()
      document.execCommand('copy')
      document.body.removeChild(textArea)
      setCopied(true)
      setTimeout(() => {
        setCopied(false)
      }, 2000)
    }
  }

  const displayName = voice.label ?? voice.voiceId
  const isCloned = voice.voiceType === 'cloned'

  return (
    <article className="surface-card p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <h3 className="text-lg font-semibold text-ink-950">{displayName}</h3>
            <span
              className={[
                'rounded-full px-3 py-1 text-xs font-semibold',
                isCloned
                  ? 'bg-sky-500/12 text-sky-700'
                  : 'bg-sand-500/15 text-ink-900/70',
              ].join(' ')}
            >
              {isCloned ? '克隆' : '内置'}
            </span>
            {voice.verificationStatus === 'verified' ? (
              <span className="rounded-full bg-mint-500/12 px-3 py-1 text-xs font-semibold text-mint-700">
                已验证
              </span>
            ) : null}
          </div>

          <div className="space-y-1 text-sm text-ink-900/68">
            <p>
              Voice ID：
              <code className="rounded bg-ink-950/5 px-2 py-0.5 text-xs font-mono">
                {voice.voiceId}
              </code>
            </p>
            {voice.speakerName ? <p>所属说话人：{voice.speakerName}</p> : null}
            {voice.platform ? <p>平台：{voice.platform}</p> : null}
            {voice.createdAt ? <p>创建时间：{formatDateTime(voice.createdAt)}</p> : null}
            {voice.notes ? <p>备注：{voice.notes}</p> : null}
          </div>
        </div>

        <button
          className={[
            'rounded-full px-5 py-2.5 text-sm font-semibold transition',
            copied
              ? 'bg-mint-500/15 text-mint-700'
              : 'bg-ink-950 text-white hover:bg-ink-950/85',
          ].join(' ')}
          onClick={handleCopy}
          type="button"
        >
          {copied ? '已复制' : '复制 Voice ID'}
        </button>
      </div>
    </article>
  )
}

function MetricTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-3xl border border-ink-950/8 bg-sand-50/75 px-4 py-4">
      <p className="eyebrow">{label}</p>
      <p className="mt-2 text-sm font-semibold text-ink-950">{value}</p>
    </div>
  )
}

function formatDateTime(value: string) {
  const parsed = new Date(value)

  if (Number.isNaN(parsed.getTime())) {
    return value
  }

  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed)
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
