import { useState } from 'react'
import { Link } from 'react-router-dom'

import { ConfigSummaryCard } from '@/components/ConfigSummaryCard'
import { ApiClient } from '@/lib/api/client'
import { resolveWebUiBaseUrl } from '@/lib/api/config'
import { usePollingTask } from '@/lib/react/usePollingTask'
import type { ApiWebUiStateResponse } from '@/types/api'

const webUiApiClient = new ApiClient(resolveWebUiBaseUrl())

interface DashboardData {
  voiceCount: number
  clonedVoiceCount: number
  ttsModel: string | null
  ttsProvider: string | null
  translationModel: string | null
}

const recommendedVideos = [
  {
    label: '短视频单人讲解（推荐新手测试）',
    url: 'https://www.youtube.com/watch?v=Pqftm3o5RdQ',
  },
  {
    label: '技术讲解视频',
    url: 'https://www.youtube.com/watch?v=6JjOk78sxI8',
  },
] as const

export function SettingsPage() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [copiedUrl, setCopiedUrl] = useState<string | null>(null)

  const loadDashboard = async () => {
    try {
      const payload = await webUiApiClient.get<ApiWebUiStateResponse>('/api/state')
      const settings = payload.settings ?? {}
      const voiceLib = payload.results?.voice_library

      const clonedCount = Array.isArray(voiceLib?.speakers)
        ? voiceLib.speakers.reduce((sum: number, s: Record<string, unknown>) => {
            const voices = Array.isArray(s.voices) ? s.voices : []
            return sum + voices.filter((v: Record<string, unknown>) => v.voice_type === 'cloned').length
          }, 0)
        : 0

      setData({
        voiceCount: voiceLib?.voice_count ?? 0,
        clonedVoiceCount: clonedCount,
        ttsModel: asString(settings.tts_model_name) ?? asString(settings.tts_model),
        ttsProvider: asString(settings.tts_provider),
        translationModel: asString(settings.selected_translation_model_label)
          ?? asString(settings.selected_translation_model),
      })
    } catch {
      // Dashboard data is non-critical, silently fail
    }
  }

  usePollingTask(() => loadDashboard(), {
    intervalMs: 30000,
  })

  const handleCopyUrl = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url)
    } catch {
      const textArea = document.createElement('textarea')
      textArea.value = url
      document.body.appendChild(textArea)
      textArea.select()
      document.execCommand('copy')
      document.body.removeChild(textArea)
    }
    setCopiedUrl(url)
    setTimeout(() => {
      setCopiedUrl(null)
    }, 2000)
  }

  return (
    <div className="space-y-6">
      <section className="surface-card p-6 lg:p-8">
        <div className="space-y-3">
          <p className="eyebrow">工作台</p>
          <h2 className="section-title">工作台</h2>
          <p className="muted-copy max-w-3xl">
            快速了解当前工作台状态，或从这里开始你的第一个翻译任务。
          </p>
        </div>

        <div className="mt-6 flex flex-wrap gap-3">
          <Link className="primary-button" to="/translations/new">
            新建翻译
          </Link>
          <Link className="secondary-button" to="/tasks/current">
            查看当前任务
          </Link>
        </div>
      </section>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]">
        <div className="space-y-6">
          <ConfigSummaryCard
            description="完成以下四步即可拿到翻译配音视频。"
            items={[
              {
                label: '第一步',
                value: '填写 YouTube 链接并创建翻译任务',
                hint: '当前支持公开的 YouTube 视频链接。',
              },
              {
                label: '第二步',
                value: '按提示完成审核',
                hint: '系统会在关键节点暂停，请确认说话人、翻译和音色。',
              },
              {
                label: '第三步',
                value: '等待处理完成',
                hint: '配音、对齐和视频合成会自动完成。',
              },
              {
                label: '第四步',
                value: '在项目详情页下载结果',
                hint: '成品视频、配音音频和字幕文件都可以下载。',
              },
            ]}
            title="快速开始"
          />

          <section className="surface-card p-5">
            <div className="space-y-2">
              <h3 className="text-lg font-semibold text-ink-950">推荐测试视频</h3>
              <p className="muted-copy">
                以下视频已验证可以跑通完整流程，适合首次测试。
              </p>
            </div>
            <div className="mt-4 space-y-3">
              {recommendedVideos.map((video) => (
                <div
                  key={video.url}
                  className="flex flex-col gap-3 rounded-2xl border border-ink-950/8 bg-sand-50/60 p-4 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="space-y-1">
                    <p className="text-sm font-semibold text-ink-950">{video.label}</p>
                    <p className="text-xs text-ink-900/55 font-mono break-all">{video.url}</p>
                  </div>
                  <button
                    className={[
                      'shrink-0 rounded-full px-4 py-2 text-sm font-semibold transition',
                      copiedUrl === video.url
                        ? 'bg-mint-500/15 text-mint-700'
                        : 'bg-ink-950/8 text-ink-950 hover:bg-ink-950/15',
                    ].join(' ')}
                    onClick={() => handleCopyUrl(video.url)}
                    type="button"
                  >
                    {copiedUrl === video.url ? '已复制' : '复制链接'}
                  </button>
                </div>
              ))}
            </div>
          </section>
        </div>

        <div className="space-y-6">
          <ConfigSummaryCard
            description="当前工作台的运行配置概况。"
            items={[
              {
                label: '已保存音色',
                value: data
                  ? `${data.voiceCount} 个音色（${data.clonedVoiceCount} 个克隆）`
                  : '加载中...',
                hint: '前往"我的音色"查看详情并复用已有音色。',
              },
              {
                label: 'TTS 模型',
                value: data?.ttsModel ?? '由部署配置决定',
                hint: data?.ttsProvider ? `提供商：${data.ttsProvider}` : undefined,
              },
              {
                label: '翻译模型',
                value: data?.translationModel ?? '由部署配置决定',
              },
              {
                label: '使用模式',
                value: '单用户 · 单活跃任务 · 仅 YouTube 链接',
              },
            ]}
            title="工作台概览"
          />

          <section className="surface-card p-5">
            <div className="space-y-2">
              <h3 className="text-lg font-semibold text-ink-950">快捷入口</h3>
            </div>
            <div className="mt-4 flex flex-col gap-3">
              <Link
                className="block rounded-2xl border border-ink-950/8 bg-sand-50/60 px-4 py-3 text-sm font-semibold text-ink-950 transition hover:bg-sand-100/90"
                to="/voices"
              >
                我的音色 →
              </Link>
              <Link
                className="block rounded-2xl border border-ink-950/8 bg-sand-50/60 px-4 py-3 text-sm font-semibold text-ink-950 transition hover:bg-sand-100/90"
                to="/projects"
              >
                我的项目 →
              </Link>
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}

function asString(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) {
    return value.trim()
  }
  return null
}
