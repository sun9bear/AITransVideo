"use client"

import { useEffect, useState } from 'react'
import { toast } from 'sonner'

interface AdminSettings {
  tts_provider: string
  review_model: string
  translation_model: string
  skip_translation_config_for_users: boolean
  skip_all_reviews_for_free_users: boolean
  free_user_max_duration_minutes: number
  enable_pre_tts_rewrite: boolean
  express_tts_provider: string
  studio_tts_provider: string
  cosyvoice_runtime_endpoint_mode: string
  cosyvoice_offline_endpoint_mode: string
  translation_char_range_min_factor: number
  translation_char_range_max_factor: number
}

const DEFAULT_SETTINGS: AdminSettings = {
  tts_provider: 'minimax',
  review_model: 'gemini_pro',
  translation_model: 'deepseek',
  skip_translation_config_for_users: true,
  skip_all_reviews_for_free_users: true,
  enable_pre_tts_rewrite: true,
  free_user_max_duration_minutes: 10,
  express_tts_provider: 'cosyvoice',
  studio_tts_provider: 'minimax',
  cosyvoice_runtime_endpoint_mode: 'international',
  cosyvoice_offline_endpoint_mode: 'mainland',
  translation_char_range_min_factor: 0.85,
  translation_char_range_max_factor: 1.15,
}

const TTS_OPTIONS = [
  { value: 'minimax', label: 'MiniMax Speech 2.8', description: '成熟稳定，¥0.20/千字，RPM 20' },
  { value: 'mimo', label: 'MiMo-V2-TTS（小米）', description: '限时免费，RPM 100，自然语言风格控制' },
  { value: 'volcengine', label: '豆包语音合成 2.0（V3）', description: '火山引擎 V3 流式接口，300+音色，¥3/万字符，自动情感表达' },
]

const EXPRESS_TTS_OPTIONS = [
  { value: 'cosyvoice', label: 'CosyVoice', description: '阿里通义，快速合成，适合快捷版' },
  { value: 'mimo', label: 'MiMo-V2-TTS（小米）', description: '限时免费，RPM 100，自然语言风格控制' },
  { value: 'volcengine', label: '豆包语音合成 1.0', description: '火山引擎 V3 流式接口，100+音色，¥5/万字符' },
]

const STUDIO_TTS_OPTIONS = [
  { value: 'minimax', label: 'MiniMax Speech 2.8', description: '成熟稳定，¥0.20/千字，支持音色克隆' },
  { value: 'cosyvoice', label: 'CosyVoice（阿里百炼）', description: '阿里百炼 v3-flash，~60中文音色，¥2/万字符' },
  { value: 'volcengine', label: '豆包语音合成 2.0（V3）', description: '火山引擎 V3 流式接口，300+音色，¥3/万字符，自动情感表达' },
]

const COSYVOICE_ENDPOINT_OPTIONS = [
  { value: 'international', label: '国际端点', description: '新加坡节点，延迟低（1-2s），支持 10 个核心音色' },
  { value: 'mainland', label: '国内端点', description: '中国大陆节点，延迟较高（3-7s），支持全部 59 个音色' },
]

// REVIEW_OPTIONS and TRANSLATION_OPTIONS removed — model selection moved to /admin/prompts

export default function AdminSettingsPage() {
  const [settings, setSettings] = useState<AdminSettings>(DEFAULT_SETTINGS)
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)

  useEffect(() => {
    fetch('/api/admin/settings', { credentials: 'include' })
      .then(async (resp) => {
        if (resp.status === 403) {
          setForbidden(true)
          return
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
        const data = await resp.json()
        // Gateway returns { settings: {...} } envelope
        const payload = data?.settings ?? data ?? {}
        setSettings({ ...DEFAULT_SETTINGS, ...payload })
      })
      .catch((err) => setError(`加载设置失败: ${err.message}`))
      .finally(() => setIsLoading(false))
  }, [])

  const handleSave = async () => {
    setIsSaving(true)
    setError(null)
    try {
      const resp = await fetch('/api/admin/settings', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: '保存失败' }))
        throw new Error(err.detail || `HTTP ${resp.status}`)
      }
      toast.success('设置已保存')
    } catch (err) {
      const msg = err instanceof Error ? err.message : '保存失败'
      setError(msg)
      toast.error(msg)
    } finally {
      setIsSaving(false)
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    )
  }

  if (forbidden) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3">
        <div className="rounded-2xl border border-red-500/20 bg-red-500/5 p-6 text-center">
          <p className="text-lg font-semibold text-red-400">仅管理员可访问</p>
          <p className="text-sm text-muted-foreground mt-2">您没有权限查看此页面。</p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-2xl font-bold font-heading text-foreground">系统设置</h1>
        <p className="text-sm text-muted-foreground mt-1">
          管理翻译流程的全局配置
        </p>
      </div>

      {error ? (
        <div className="rounded-xl border border-red-500/20 bg-red-500/5 p-4 text-sm text-red-400">
          {error}
        </div>
      ) : null}

      {/* TTS Provider */}
      <SettingSection
        title="语音合成（TTS）引擎"
        description="选择配音阶段使用的 TTS 服务"
      >
        <RadioGroup
          options={TTS_OPTIONS}
          value={settings.tts_provider}
          onChange={(v) => setSettings((s) => ({ ...s, tts_provider: v }))}
          name="tts_provider"
        />
      </SettingSection>

      {/* Express TTS Provider */}
      <SettingSection
        title="快捷版 TTS 引擎"
        description="快捷版（express）任务使用的语音合成服务"
      >
        <RadioGroup
          options={EXPRESS_TTS_OPTIONS}
          value={settings.express_tts_provider}
          onChange={(v) => setSettings((s) => ({ ...s, express_tts_provider: v }))}
          name="express_tts_provider"
        />
      </SettingSection>

      {/* Studio TTS Provider */}
      <SettingSection
        title="工作台版 TTS 引擎"
        description="工作台版（Studio）任务使用的语音合成服务"
      >
        <RadioGroup
          options={STUDIO_TTS_OPTIONS}
          value={settings.studio_tts_provider}
          onChange={(v) => setSettings((s) => ({ ...s, studio_tts_provider: v }))}
          name="studio_tts_provider"
        />
      </SettingSection>

      {/* Model management — moved to prompts page */}
      <SettingSection
        title="大模型配置"
        description="审校模型和翻译模型现已移至「提示词管理」页面，支持工作台版/快捷版独立配置"
      >
        <div className="rounded-xl border border-border bg-muted/30 p-4">
          <a
            href="/admin/prompts"
            className="text-sm text-primary hover:underline"
          >
            前往「提示词 & 模型管理」→
          </a>
        </div>
      </SettingSection>

      {/* Skip translation config */}
      <SettingSection
        title="流程简化"
        description="普通用户的视频翻译流程设置"
      >
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.skip_translation_config_for_users}
            onChange={(e) => setSettings((s) => ({ ...s, skip_translation_config_for_users: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">普通用户跳过翻译配置阶段</p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后，普通用户不会看到翻译配置审核步骤，直接使用上面设置的默认翻译模型。
            </p>
          </div>
        </label>
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.skip_all_reviews_for_free_users}
            onChange={(e) => setSettings((s) => ({ ...s, skip_all_reviews_for_free_users: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">免费用户跳过所有审核</p>
            <p className="text-xs text-muted-foreground mt-1">
              开启后，免费用户的任务将自动跳过所有人工审核步骤（说话人、翻译、语音），全流程自动完成。
            </p>
          </div>
        </label>
        <label className="flex items-center gap-3 rounded-xl border border-border bg-muted/30 p-4 cursor-pointer hover:bg-muted/50 transition">
          <input
            type="checkbox"
            checked={settings.enable_pre_tts_rewrite}
            onChange={(e) => setSettings((s) => ({ ...s, enable_pre_tts_rewrite: e.target.checked }))}
            className="h-4 w-4 rounded border-border"
          />
          <div>
            <p className="text-sm font-medium text-foreground">Pre-TTS 预重写</p>
            <p className="text-xs text-muted-foreground mt-1">
              TTS 生成前预估时长并重写不匹配的译文。关闭后由 TTS 后的对齐阶段统一处理，适合语速差异大的场景。
            </p>
          </div>
        </label>
      </SettingSection>

      {/* Free user limits */}
      <SettingSection title="免费用户限制" description="控制免费用户的使用配额。">
        <div className="rounded-xl border border-border bg-muted/30 p-4 space-y-3">
          <div className="flex items-center gap-3">
            <span className="text-sm font-medium text-foreground whitespace-nowrap">最大视频时长</span>
            <div className="group rounded-lg border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
              <input
                type="number"
                min={1}
                max={180}
                step={1}
                value={settings.free_user_max_duration_minutes}
                onChange={(e) => setSettings((s) => ({ ...s, free_user_max_duration_minutes: Number(e.target.value) || 10 }))}
                className="w-20 rounded-lg bg-transparent px-3 py-2 text-sm text-foreground text-center focus:outline-none"
              />
            </div>
            <span className="text-sm text-muted-foreground">分钟</span>
          </div>
          <p className="text-xs text-muted-foreground">
            超过此时长的视频将被拒绝处理。付费用户不受此限制。
          </p>
        </div>
      </SettingSection>

      {/* Translation Char Range */}
      <SettingSection
        title="翻译字数范围"
        description="控制翻译阶段 min_chars / max_chars 的计算范围。probe 校准后 target_chars 已精准，此范围影响 LLM 翻译的字数容差和重试判断。"
      >
        <div className="rounded-xl border border-border bg-muted/30 p-4 space-y-4">
          <div className="flex items-center gap-3">
            <span className="text-sm font-medium text-foreground whitespace-nowrap">下限系数</span>
            <div className="group rounded-lg border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
              <input
                type="number"
                min={0.5}
                max={1.0}
                step={0.05}
                value={settings.translation_char_range_min_factor}
                onChange={(e) => setSettings((s) => ({ ...s, translation_char_range_min_factor: Number(e.target.value) || 0.85 }))}
                className="w-20 rounded-lg bg-transparent px-3 py-2 text-sm text-foreground text-center focus:outline-none"
              />
            </div>
            <span className="text-xs text-muted-foreground">min_chars = target_chars × {settings.translation_char_range_min_factor}</span>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm font-medium text-foreground whitespace-nowrap">上限系数</span>
            <div className="group rounded-lg border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
              <input
                type="number"
                min={1.0}
                max={2.0}
                step={0.05}
                value={settings.translation_char_range_max_factor}
                onChange={(e) => setSettings((s) => ({ ...s, translation_char_range_max_factor: Number(e.target.value) || 1.15 }))}
                className="w-20 rounded-lg bg-transparent px-3 py-2 text-sm text-foreground text-center focus:outline-none"
              />
            </div>
            <span className="text-xs text-muted-foreground">max_chars = target_chars × {settings.translation_char_range_max_factor}</span>
          </div>
          <p className="text-xs text-muted-foreground">
            范围越窄（如 0.90-1.10），翻译字数越精准但重试次数可能增加；范围越宽（如 0.80-1.20），翻译更宽松但配音时长匹配度降低。
          </p>
        </div>
      </SettingSection>

      {/* CosyVoice Endpoint Settings */}
      <SettingSection
        title="CosyVoice 端点配置"
        description="控制 CosyVoice 语音合成的服务端点。运行时端点影响实际配音生成，离线端点用于音色分析和建库。"
      >
        <div className="space-y-4">
          <div>
            <p className="text-sm font-medium text-foreground mb-2">运行时端点（Runtime）</p>
            <p className="text-xs text-muted-foreground mb-3">
              用于 express / CosyVoice 实际生产调用。默认国际端点，延迟低但音色覆盖有限。
            </p>
            <RadioGroup
              options={COSYVOICE_ENDPOINT_OPTIONS}
              value={settings.cosyvoice_runtime_endpoint_mode}
              onChange={(v) => setSettings((s) => ({ ...s, cosyvoice_runtime_endpoint_mode: v }))}
              name="cosyvoice_runtime_endpoint"
            />
          </div>
          <div className="border-t border-border pt-4">
            <p className="text-sm font-medium text-foreground mb-2">离线端点（Offline）</p>
            <p className="text-xs text-muted-foreground mb-3">
              用于 calibration / profiling / 离线建库。默认国内端点，音色覆盖完整。
            </p>
            <RadioGroup
              options={COSYVOICE_ENDPOINT_OPTIONS}
              value={settings.cosyvoice_offline_endpoint_mode}
              onChange={(v) => setSettings((s) => ({ ...s, cosyvoice_offline_endpoint_mode: v }))}
              name="cosyvoice_offline_endpoint"
            />
          </div>
        </div>
      </SettingSection>

      {/* Save button */}
      <div className="flex gap-3 pt-4 border-t border-border">
        <button
          className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:opacity-50"
          disabled={isSaving}
          onClick={() => { void handleSave() }}
          type="button"
        >
          {isSaving ? '保存中…' : '保存设置'}
        </button>
        <button
          className="rounded-lg border border-border bg-muted/30 px-4 py-2 text-sm text-muted-foreground transition hover:bg-muted/50"
          onClick={() => setSettings(DEFAULT_SETTINGS)}
          type="button"
        >
          恢复默认
        </button>
      </div>
    </div>
  )
}

function SettingSection({
  title,
  description,
  children,
}: {
  title: string
  description: string
  children: React.ReactNode
}) {
  return (
    <section className="rounded-2xl border border-border bg-card p-6">
      <h2 className="text-lg font-semibold text-foreground">{title}</h2>
      <p className="text-sm text-muted-foreground mt-1 mb-4">{description}</p>
      {children}
    </section>
  )
}

function RadioGroup({
  options,
  value,
  onChange,
  name,
}: {
  options: { value: string; label: string; description: string }[]
  value: string
  onChange: (v: string) => void
  name?: string
}) {
  const groupName = name ?? options.map((o) => o.value).join('-')
  return (
    <div className="space-y-2">
      {options.map((opt) => (
        <label
          key={opt.value}
          className={`flex items-start gap-3 rounded-xl border p-4 cursor-pointer transition ${
            value === opt.value
              ? 'border-primary/50 bg-primary/5'
              : 'border-border bg-muted/30 hover:bg-muted/50'
          }`}
        >
          <input
            type="radio"
            name={groupName}
            checked={value === opt.value}
            onChange={() => onChange(opt.value)}
            className="mt-0.5 h-4 w-4 border-border"
          />
          <div>
            <p className={`text-sm font-medium ${value === opt.value ? 'text-primary' : 'text-foreground'}`}>
              {opt.label}
            </p>
            <p className="text-xs text-muted-foreground mt-0.5">{opt.description}</p>
          </div>
        </label>
      ))}
    </div>
  )
}
