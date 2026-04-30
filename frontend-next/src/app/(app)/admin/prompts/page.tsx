"use client"

import { useEffect, useState } from 'react'
import { toast } from 'sonner'

interface PromptData {
  pass1: string
  pass2: string
  pass3: string
  translate: string
  rewrite: string
  probe_translate: string
  content_compliance: string
}

interface ModelOption {
  value: string
  label: string
  cost_hint: string
  cost_rank: number
  supports_audio?: boolean
}

interface ModelWithStatus extends ModelOption {
  provider: string
  enabled: boolean
}

interface ModelsData {
  studio: Record<string, string>
  express: Record<string, string>
}

interface HistoryVersion {
  saved_at: string
  label: string
  prompts: PromptData
  models?: ModelsData
}

const PROMPT_KEYS = ['pass1', 'pass2', 'pass3', 'translate', 'rewrite', 'probe_translate', 'content_compliance'] as const
type PromptKey = typeof PROMPT_KEYS[number]
const EXPRESS_PROMPT_KEYS = ['pass1', 'pass2', 'pass3', 'translate', 'rewrite', 'probe_translate', 'content_compliance'] as const

const PROMPT_LABELS: Record<PromptKey, { title: string; desc: string; vars: string }> = {
  pass1: {
    title: 'Pass 1 - 说话人识别',
    desc: '听音频 + 读转录稿，识别说话人身份、纠正标注错误、拆分混合发言段落',
    vars: '{video_title} {video_url} {line_count} {transcript_body}',
  },
  pass2: {
    title: 'Pass 2 - 文本修正',
    desc: '纯文本处理，修正 ASR 错误、拆分过长段落、提取术语表',
    vars: '{video_title} {line_count} {transcript_body} {speakers_json}',
  },
  pass3: {
    title: 'Pass 3 - 音色画像',
    desc: '听每个说话人的音频片段，生成 TTS 选音匹配的音色画像',
    vars: '{video_title} {speakers_json} {speaker_ids}',
  },
  translate: {
    title: '翻译提示词',
    desc: '将英文转录稿翻译成中文口播文本，控制时长匹配',
    vars: '__VIDEO_TITLE__ __YOUTUBE_URL__ __GROUPS_JSON__ __GLOSSARY_SECTION__ __SPEAKER_INSTRUCTION__',
  },
  rewrite: {
    title: '重写提示词',
    desc: 'TTS 文本长度调整，扩写或缩写以匹配目标配音时长',
    vars: '__TTS_CN_TEXT__ __SOURCE_TEXT__ __CURRENT_CHARS__ __TARGET_CHARS__ __DIRECTION_DESC__',
  },
  probe_translate: {
    title: '探针翻译提示词',
    desc: '探针段落预翻译，用于 TTS 校准和音色试听（无字数约束，仅按时长引导）',
    vars: '__VIDEO_TITLE__ __YOUTUBE_URL__ __GROUPS_JSON__ __GLOSSARY_SECTION__ __SPEAKER_INSTRUCTION__',
  },
  content_compliance: {
    title: '内容合规审核提示词',
    desc: '本地规则未明确命中后，调用大模型进行第二层语义审核',
    vars: '__VIDEO_TITLE__ __VIDEO_DESCRIPTION__ __SOURCE_TYPE__ __SOURCE_REF__ __LOCAL_FINDINGS_JSON__ __TRANSCRIPT_BODY__',
  },
}

const PROVIDER_LABELS: Record<string, string> = {
  deepseek: 'DeepSeek',
  openai: 'OpenAI',
  mimo: 'MiMo',
}

const EMPTY_PROMPTS: PromptData = { pass1: '', pass2: '', pass3: '', translate: '', rewrite: '', probe_translate: '', content_compliance: '' }

const API_BASE = '/api/admin/review-prompts'

type MainTab = 'models' | 'prompts' | 'keys'
type ModeTab = 'studio' | 'express'

export default function PromptsPage() {
  const [prompts, setPrompts] = useState<PromptData>({ ...EMPTY_PROMPTS })
  const [defaults, setDefaults] = useState<PromptData>({ ...EMPTY_PROMPTS })
  const [models, setModels] = useState<ModelsData>({ studio: {}, express: {} })
  const [defaultModels, setDefaultModels] = useState<ModelsData>({ studio: {}, express: {} })
  const [availableModels, setAvailableModels] = useState<Record<string, ModelOption[]>>({})
  const [allModels, setAllModels] = useState<ModelWithStatus[]>([])
  const [providerKeys, setProviderKeys] = useState<Record<string, string>>({})
  const [apiKeyStatus, setApiKeyStatus] = useState<Record<string, boolean>>({})
  const [geminiConfigured, setGeminiConfigured] = useState(false)
  const [history, setHistory] = useState<HistoryVersion[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saveLabel, setSaveLabel] = useState('')
  const [mainTab, setMainTab] = useState<MainTab>('models')
  const [modeTab, setModeTab] = useState<ModeTab>('studio')
  const [editingKeys, setEditingKeys] = useState<Record<string, string>>({})
  const [editingKeyProviders, setEditingKeyProviders] = useState<Set<string>>(new Set())

  const refreshAll = async () => {
    const resp = await fetch(API_BASE, { credentials: 'include' })
    const data = await resp.json()
    setPrompts({ ...EMPTY_PROMPTS, ...data.prompts })
    setDefaults({ ...EMPTY_PROMPTS, ...data.defaults })
    setModels(data.models || { studio: {}, express: {} })
    setDefaultModels(data.default_models || { studio: {}, express: {} })
    setAvailableModels(data.available_models || {})
    setAllModels(data.all_models || [])
    setProviderKeys(data.provider_api_keys || {})
    setApiKeyStatus(data.api_key_status || {})
    setGeminiConfigured(data.gemini_configured ?? false)
    setHistory(data.history || [])
  }

  useEffect(() => {
    refreshAll()
      .catch(() => toast.error('加载失败'))
      .finally(() => setLoading(false))
  }, [])

  // --- Model toggle ---
  const handleToggleModel = async (modelValue: string, enabled: boolean) => {
    try {
      const resp = await fetch('/api/admin/model-toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ model: modelValue, enabled }),
      })
      if (!resp.ok) throw new Error(await resp.text())
      await refreshAll()
      toast.success(`${modelValue} 已${enabled ? '启用' : '禁用'}`)
    } catch (e) {
      toast.error('操作失败: ' + (e instanceof Error ? e.message : String(e)))
    }
  }

  // --- Model selection per prompt ---
  const setModelForPrompt = (mode: ModeTab, key: string, value: string) => {
    setModels(prev => ({
      ...prev,
      [mode]: { ...prev[mode], [key]: value },
    }))
  }

  // --- Save (models + prompts) ---
  const handleSave = async () => {
    setSaving(true)
    try {
      const keysPayload: Record<string, string> = {}
      for (const provider of editingKeyProviders) {
        keysPayload[provider] = editingKeys[provider] ?? ''
      }
      const resp = await fetch(API_BASE, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          prompts,
          models,
          provider_api_keys: editingKeyProviders.size > 0 ? keysPayload : undefined,
          label: saveLabel || undefined,
        }),
      })
      if (!resp.ok) throw new Error(await resp.text())
      setSaveLabel('')
      setEditingKeys({})
      setEditingKeyProviders(new Set())
      await refreshAll()
      toast.success('配置已保存')
    } catch (e) {
      toast.error('保存失败: ' + (e instanceof Error ? e.message : String(e)))
    } finally {
      setSaving(false)
    }
  }

  // --- Restore ---
  const handleRestore = async (index: number) => {
    const version = history[index]
    if (!confirm(`确定还原到「${version.label}」？当前配置将被保存到历史。`)) return
    try {
      const resp = await fetch(`${API_BASE}/restore`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ index }),
      })
      if (!resp.ok) throw new Error(await resp.text())
      await refreshAll()
      toast.success(`已还原到「${version.label}」`)
    } catch {
      toast.error('还原失败')
    }
  }

  // --- Delete history ---
  const handleDeleteHistory = async (index: number) => {
    const version = history[index]
    if (!confirm(`确定删除历史版本「${version.label}」？`)) return
    try {
      const resp = await fetch(`${API_BASE}/history/${index}`, {
        method: 'DELETE',
        credentials: 'include',
      })
      if (!resp.ok) throw new Error(await resp.text())
      const data = await resp.json()
      setHistory(data.history || [])
      toast.success('已删除')
    } catch {
      toast.error('删除失败')
    }
  }

  const promptKeysForMode = modeTab === 'studio' ? PROMPT_KEYS : EXPRESS_PROMPT_KEYS

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <div className="text-muted-foreground">加载中...</div>
      </div>
    )
  }

  return (
    <div className="max-w-5xl mx-auto space-y-8 p-6">
      <div>
        <h1 className="text-2xl font-bold font-heading">模型管理</h1>
        <p className="text-muted-foreground mt-1">
          管理大模型上下线、各阶段模型选择、提示词配置和 API Key。
        </p>
      </div>

      {/* === Top-level tabs === */}
      <div className="flex gap-1 rounded-lg border border-border bg-muted p-1 w-fit">
        {([
          { key: 'models' as const, label: '模型管理' },
          { key: 'prompts' as const, label: '提示词管理' },
          { key: 'keys' as const, label: 'API Key 管理' },
        ]).map(tab => (
          <button
            key={tab.key}
            onClick={() => setMainTab(tab.key)}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
              mainTab === tab.key
                ? 'bg-background text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* ================================================================ */}
      {/* TAB 1: 模型管理 — 上下线 + 各阶段模型选择                           */}
      {/* ================================================================ */}
      {mainTab === 'models' && (
        <div className="space-y-8">
          {/* Model availability */}
          <div className="rounded-lg border border-border bg-card p-5 space-y-4">
            <h2 className="text-lg font-semibold">模型列表</h2>
            <p className="text-sm text-muted-foreground">
              控制每个模型是否上线。禁用的模型不会出现在下拉列表和自动降级链中。
            </p>
            <div className="space-y-2">
              {allModels.map(m => (
                <div key={m.value} className="flex items-center justify-between rounded-md border border-border bg-background px-4 py-3">
                  <div className="flex items-center gap-3">
                    <span className="text-sm font-medium">{m.label}</span>
                    <span className="text-xs text-muted-foreground">{m.cost_hint}</span>
                    {m.supports_audio && (
                      <span className="text-xs px-1.5 py-0.5 rounded bg-muted/60 text-muted-foreground">音频</span>
                    )}
                  </div>
                  <button
                    onClick={() => handleToggleModel(m.value, !m.enabled)}
                    className={`text-xs px-3 py-1 rounded-md font-medium transition-colors ${
                      m.enabled
                        ? 'bg-[color:var(--bamboo)]/10 text-[color:var(--bamboo)] hover:bg-[color:var(--bamboo)]/20'
                        : 'bg-[color:var(--cinnabar)]/10 text-[color:var(--cinnabar)] hover:bg-[color:var(--cinnabar)]/20'
                    }`}
                  >
                    {m.enabled ? '已上线' : '已下线'}
                  </button>
                </div>
              ))}
            </div>
          </div>

          {/* Studio / Express sub-tabs for model selection */}
          <div className="space-y-4">
            <h2 className="text-lg font-semibold">各阶段模型选择</h2>
            <div className="flex gap-1 rounded-lg border border-border bg-muted p-1 w-fit">
              {(['studio', 'express'] as const).map(tab => (
                <button
                  key={tab}
                  onClick={() => setModeTab(tab)}
                  className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                    modeTab === tab
                      ? 'bg-background text-foreground shadow-sm'
                      : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {tab === 'studio' ? '工作台版' : '快捷版'}
                </button>
              ))}
            </div>

{/* Express Pass1 now enabled — same pipeline as Studio */}

            <div className="space-y-3">
              {promptKeysForMode.map(key => {
                const modelOptions = availableModels[key] || []
                const currentModel = models[modeTab]?.[key] || defaultModels[modeTab]?.[key] || ''
                return (
                  <div key={key} className="flex items-center justify-between rounded-md border border-border bg-card px-4 py-3">
                    <span className="text-sm font-medium w-48">{PROMPT_LABELS[key].title}</span>
                    <div className="flex items-center gap-3">
                      <select
                        value={currentModel}
                        onChange={e => setModelForPrompt(modeTab, key, e.target.value)}
                        className="rounded-md border border-input bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                      >
                        {modelOptions.map(m => (
                          <option key={m.value} value={m.value}>{m.label}</option>
                        ))}
                      </select>
                      <span className="text-xs text-muted-foreground w-28">
                        {modelOptions.find(m => m.value === currentModel)?.cost_hint}
                      </span>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>

          {/* Save button for model selection */}
          <div className="flex items-center gap-3">
            <input
              type="text"
              value={saveLabel}
              onChange={e => setSaveLabel(e.target.value)}
              placeholder="版本标签（可选）"
              className="flex-1 max-w-xs rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
            <button
              onClick={handleSave}
              disabled={saving}
              className="rounded-md bg-primary px-6 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {saving ? '保存中...' : '保存模型配置'}
            </button>
          </div>
        </div>
      )}

      {/* ================================================================ */}
      {/* TAB 2: 提示词管理                                                 */}
      {/* ================================================================ */}
      {mainTab === 'prompts' && (
        <div className="space-y-6">
          <div className="space-y-6">
            {PROMPT_KEYS.map(key => (
              <div key={key} className="rounded-lg border border-border bg-card p-5 space-y-3">
                <div className="flex items-start justify-between">
                  <div>
                    <h2 className="text-lg font-semibold">{PROMPT_LABELS[key].title}</h2>
                    <p className="text-sm text-muted-foreground">{PROMPT_LABELS[key].desc}</p>
                  </div>
                  {prompts[key] ? (
                    <span className="text-xs px-2 py-0.5 rounded bg-primary/20 text-primary shrink-0">自定义</span>
                  ) : (
                    <span className="text-xs px-2 py-0.5 rounded bg-muted text-muted-foreground shrink-0">系统默认</span>
                  )}
                </div>
                <p className="text-xs text-muted-foreground/60">
                  可用变量: <code className="text-xs bg-muted px-1 rounded">{PROMPT_LABELS[key].vars}</code>
                </p>
                <textarea
                  value={prompts[key] || defaults[key]}
                  onChange={e => setPrompts(prev => ({ ...prev, [key]: e.target.value }))}
                  className="w-full h-48 rounded-md border border-input bg-background px-3 py-2 text-sm font-mono resize-y focus:outline-none focus:ring-2 focus:ring-ring"
                />
                {prompts[key] && (
                  <button
                    onClick={() => setPrompts(prev => ({ ...prev, [key]: '' }))}
                    className="text-xs text-muted-foreground hover:text-foreground underline"
                  >
                    恢复为系统默认
                  </button>
                )}
              </div>
            ))}
          </div>

          {/* Save */}
          <div className="flex items-center gap-3">
            <input
              type="text"
              value={saveLabel}
              onChange={e => setSaveLabel(e.target.value)}
              placeholder="版本标签（可选）"
              className="flex-1 max-w-xs rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
            <button
              onClick={handleSave}
              disabled={saving}
              className="rounded-md bg-primary px-6 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {saving ? '保存中...' : '保存提示词'}
            </button>
          </div>

          {/* History */}
          {history.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-lg font-semibold">历史版本</h2>
              <div className="space-y-2">
                {history.map((version, idx) => (
                  <div key={idx} className="flex items-center justify-between rounded-md border border-border bg-card px-4 py-3">
                    <div className="flex-1 min-w-0">
                      <span className="font-medium text-sm">{version.label}</span>
                      <span className="text-xs text-muted-foreground ml-3">
                        {new Date(version.saved_at).toLocaleString('zh-CN')}
                      </span>
                      <span className="text-xs text-muted-foreground ml-2">
                        {Object.entries(version.prompts).filter(([, v]) => v).map(([k]) => PROMPT_LABELS[k as PromptKey]?.title?.split(' - ').pop() || k).join(' + ') || '(全部默认)'}
                      </span>
                      {version.models && (
                        <span className="text-xs text-muted-foreground/60 ml-2">+ 模型配置</span>
                      )}
                    </div>
                    <div className="flex items-center gap-2 ml-4">
                      <button onClick={() => handleRestore(idx)} className="text-xs text-primary hover:underline">还原</button>
                      <button onClick={() => handleDeleteHistory(idx)} className="text-xs text-destructive hover:underline">删除</button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* ================================================================ */}
      {/* TAB 3: API Key 管理                                               */}
      {/* ================================================================ */}
      {mainTab === 'keys' && (
        <div className="space-y-6">
          <div className="rounded-lg border border-border bg-card p-5 space-y-4">
            <h2 className="text-lg font-semibold">API Key 管理</h2>
            <p className="text-sm text-muted-foreground">
              按 Provider 维度管理。留空则使用环境变量中的全局 Key。
            </p>

            <div className="space-y-3">
              {/* Gemini (not editable) */}
              <div className="flex items-center gap-3 py-2">
                <span className="text-sm font-medium w-24">Gemini</span>
                <span className="text-sm text-muted-foreground">由 client_factory 管理</span>
                {geminiConfigured ? (
                  <span className="text-xs text-[color:var(--bamboo)] dark:text-[color:var(--bamboo)]">✅ 已配置</span>
                ) : (
                  <span className="text-xs text-[color:var(--cinnabar)]">❌ 未配置</span>
                )}
              </div>

              {/* Editable providers */}
              {Object.entries(PROVIDER_LABELS).map(([provider, label]) => {
                const envVar = provider === 'deepseek' ? 'DEEPSEEK_API_KEY' : provider === 'openai' ? 'OPENAI_API_KEY' : 'MIMO_API_KEY'
                const isEditing = editingKeyProviders.has(provider)
                const maskedValue = providerKeys[provider] || ''
                const envConfigured = apiKeyStatus[envVar] ?? false

                return (
                  <div key={provider} className="flex items-center gap-3 py-2">
                    <span className="text-sm font-medium w-24">{label}</span>
                    {isEditing ? (
                      <input
                        type="text"
                        value={editingKeys[provider] || ''}
                        onChange={e => setEditingKeys(prev => ({ ...prev, [provider]: e.target.value }))}
                        placeholder="输入新的 API Key"
                        className="flex-1 max-w-sm rounded-md border border-input bg-background px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-ring"
                      />
                    ) : (
                      <span className="text-sm text-muted-foreground font-mono">
                        {maskedValue || '未设置'}
                      </span>
                    )}
                    <div className="flex items-center gap-2">
                      {isEditing ? (
                        <button
                          onClick={() => {
                            const next = new Set(editingKeyProviders)
                            next.delete(provider)
                            setEditingKeyProviders(next)
                            setEditingKeys(prev => { const n = { ...prev }; delete n[provider]; return n })
                          }}
                          className="text-xs text-muted-foreground hover:text-foreground"
                        >
                          取消
                        </button>
                      ) : (
                        <>
                          <button
                            onClick={() => {
                              setEditingKeyProviders(prev => new Set(prev).add(provider))
                              setEditingKeys(prev => ({ ...prev, [provider]: '' }))
                            }}
                            className="text-xs text-primary hover:underline"
                          >
                            修改
                          </button>
                          {maskedValue && (
                            <button
                              onClick={() => {
                                setEditingKeyProviders(prev => new Set(prev).add(provider))
                                setEditingKeys(prev => ({ ...prev, [provider]: '' }))
                              }}
                              className="text-xs text-destructive hover:underline"
                            >
                              清除
                            </button>
                          )}
                        </>
                      )}
                      {envConfigured ? (
                        <span className="text-xs text-[color:var(--bamboo)] dark:text-[color:var(--bamboo)]">✅ 环境变量</span>
                      ) : (
                        <span className="text-xs text-[color:var(--cinnabar)]">❌ 无环境变量</span>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>

          {/* Save */}
          <button
            onClick={handleSave}
            disabled={saving || editingKeyProviders.size === 0}
            className="rounded-md bg-primary px-6 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {saving ? '保存中...' : '保存 Key 配置'}
          </button>
        </div>
      )}
    </div>
  )
}
