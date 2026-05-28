"use client"

import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import {
  type AdminCosyvoiceRuntimeStatus,
  type AdminCosyvoiceSettings,
  getAdminCosyvoiceControl,
  updateAdminCosyvoiceControl,
} from '@/lib/api/adminCosyvoice'

const MODEL_OPTIONS = [
  { value: 'cosyvoice-v3.5-flash', label: 'Flash' },
  { value: 'cosyvoice-v3.5-plus', label: 'Plus' },
]

const ENDPOINT_OPTIONS = [
  { value: 'international', label: '国际端点' },
  { value: 'mainland', label: '国内端点' },
]

export default function AdminCosyvoicePage() {
  const [settings, setSettings] = useState<AdminCosyvoiceSettings | null>(null)
  const [runtime, setRuntime] = useState<AdminCosyvoiceRuntimeStatus | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setIsLoading(true)
    setError(null)
    try {
      const data = await getAdminCosyvoiceControl()
      setSettings(data.settings)
      setRuntime(data.runtime)
    } catch (err) {
      const msg = err instanceof Error ? err.message : '加载 CosyVoice 配置失败'
      setError(msg)
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const save = async () => {
    if (!settings) return
    setIsSaving(true)
    setError(null)
    try {
      const data = await updateAdminCosyvoiceControl(settings)
      setSettings(data.settings)
      setRuntime(data.runtime)
      toast.success('CosyVoice 配置已保存')
    } catch (err) {
      const msg = err instanceof Error ? err.message : '保存 CosyVoice 配置失败'
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

  if (!settings) {
    return (
      <div className="max-w-3xl rounded-lg border border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/5 p-4 text-sm text-[color:var(--cinnabar)]">
        {error ?? '无法加载 CosyVoice 管理配置'}
      </div>
    )
  }

  return (
    <div className="max-w-5xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold font-heading text-foreground">CosyVoice 国内端点</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          管理手动克隆、Express 自动克隆和运行时 readiness。部署级 URL、HMAC、OSS 密钥只读展示，不在后台页面编辑。
        </p>
      </div>

      {error ? (
        <div className="rounded-lg border border-[color:var(--cinnabar)]/30 bg-[color:var(--cinnabar)]/5 p-4 text-sm text-[color:var(--cinnabar)]">
          {error}
        </div>
      ) : null}

      <RuntimeSection runtime={runtime} />

      <Section
        title="端点策略"
        description="这些是业务策略开关，保存后热生效；不是 worker URL 或 secret。"
      >
        <div className="grid gap-4 md:grid-cols-2">
          <SelectField
            label="运行时 TTS 端点"
            value={settings.cosyvoice_runtime_endpoint_mode}
            options={ENDPOINT_OPTIONS}
            onChange={(value) => setSettings((s) => s && ({ ...s, cosyvoice_runtime_endpoint_mode: value }))}
          />
          <SelectField
            label="离线分析端点"
            value={settings.cosyvoice_offline_endpoint_mode}
            options={ENDPOINT_OPTIONS}
            onChange={(value) => setSettings((s) => s && ({ ...s, cosyvoice_offline_endpoint_mode: value }))}
          />
        </div>
      </Section>

      <Section
        title="手动克隆"
        description="控制用户在音色选择页显式点击“克隆音色”的路径。"
      >
        <div className="space-y-4">
          <ToggleField
            label="启用手动克隆 worker"
            description="关闭后手动 CosyVoice 克隆入口会被 runtime gate 拦住。"
            checked={settings.cosyvoice_clone_worker_enabled}
            onChange={(checked) => setSettings((s) => s && ({ ...s, cosyvoice_clone_worker_enabled: checked }))}
          />
          <ToggleField
            label="全用户开放手动克隆"
            description="打开后所有已登录用户都能看到显式克隆入口；仍需用户主动确认，且仍受 runtime 和 quota 约束。"
            checked={settings.cosyvoice_clone_general_availability_enabled}
            danger
            onChange={(checked) => setSettings((s) => s && ({ ...s, cosyvoice_clone_general_availability_enabled: checked }))}
          />
          <div className="grid gap-4 md:grid-cols-2">
            <SelectField
              label="默认目标模型"
              value={settings.cosyvoice_clone_default_target_model}
              options={MODEL_OPTIONS}
              onChange={(value) => setSettings((s) => s && ({ ...s, cosyvoice_clone_default_target_model: value }))}
            />
            <NumberField
              label="每用户 active 克隆音色上限"
              value={settings.cosyvoice_clone_max_voices_per_user}
              min={1}
              max={100}
              onChange={(value) => setSettings((s) => s && ({ ...s, cosyvoice_clone_max_voices_per_user: value }))}
            />
          </div>
          <AllowlistField
            label="手动克隆 allowlist"
            value={settings.cosyvoice_clone_user_allowlist}
            onChange={(value) => setSettings((s) => s && ({ ...s, cosyvoice_clone_user_allowlist: value }))}
          />
        </div>
      </Section>

      <Section
        title="Express 自动克隆"
        description="控制快捷版任务里的自动临时音色克隆。即使打开，也需要用户在提交页显式勾选授权。"
      >
        <div className="space-y-4">
          <ToggleField
            label="启用 Express 自动克隆"
            description="付费 API canary。建议先只给 admin/test user 加 allowlist。"
            checked={settings.express_cosyvoice_auto_clone_enabled}
            danger
            onChange={(checked) => setSettings((s) => s && ({ ...s, express_cosyvoice_auto_clone_enabled: checked }))}
          />
          <AllowlistField
            label="Express 自动克隆 allowlist"
            value={settings.express_cosyvoice_auto_clone_user_allowlist}
            onChange={(value) => setSettings((s) => s && ({ ...s, express_cosyvoice_auto_clone_user_allowlist: value }))}
          />
          <div className="grid gap-4 md:grid-cols-3">
            <NumberField
              label="主说话人占比阈值"
              value={settings.express_cosyvoice_auto_clone_main_speaker_min_ratio}
              min={0.1}
              max={1}
              step={0.01}
              onChange={(value) => setSettings((s) => s && ({ ...s, express_cosyvoice_auto_clone_main_speaker_min_ratio: value }))}
            />
            <NumberField
              label="主说话人最少行数"
              value={settings.express_cosyvoice_auto_clone_main_speaker_min_lines}
              min={1}
              max={100}
              onChange={(value) => setSettings((s) => s && ({ ...s, express_cosyvoice_auto_clone_main_speaker_min_lines: value }))}
            />
            <NumberField
              label="样本最大秒数"
              value={settings.express_cosyvoice_auto_clone_sample_max_seconds}
              min={10}
              max={60}
              step={0.5}
              onChange={(value) => setSettings((s) => s && ({ ...s, express_cosyvoice_auto_clone_sample_max_seconds: value }))}
            />
          </div>
          <div className="grid gap-4 md:grid-cols-4">
            <SelectField
              label="目标模型"
              value={settings.express_cosyvoice_auto_clone_target_model}
              options={MODEL_OPTIONS}
              onChange={(value) => setSettings((s) => s && ({ ...s, express_cosyvoice_auto_clone_target_model: value }))}
            />
            <NumberField
              label="每日克隆上限"
              value={settings.express_cosyvoice_auto_clone_per_user_daily_cap}
              min={0}
              max={1000}
              onChange={(value) => setSettings((s) => s && ({ ...s, express_cosyvoice_auto_clone_per_user_daily_cap: value }))}
            />
            <NumberField
              label="active 临时音色上限"
              value={settings.express_cosyvoice_auto_clone_per_user_active_temp_cap}
              min={0}
              max={100}
              onChange={(value) => setSettings((s) => s && ({ ...s, express_cosyvoice_auto_clone_per_user_active_temp_cap: value }))}
            />
            <NumberField
              label="reservation TTL 分钟"
              value={settings.express_cosyvoice_auto_clone_reservation_ttl_minutes}
              min={5}
              max={120}
              onChange={(value) => setSettings((s) => s && ({ ...s, express_cosyvoice_auto_clone_reservation_ttl_minutes: value }))}
            />
          </div>
        </div>
      </Section>

      <div className="flex flex-wrap gap-3 border-t border-border pt-4">
        <button
          className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:brightness-110 disabled:opacity-50"
          disabled={isSaving}
          onClick={() => { void save() }}
          type="button"
        >
          {isSaving ? '保存中…' : '保存 CosyVoice 配置'}
        </button>
        <button
          className="rounded-lg border border-border bg-muted/30 px-4 py-2 text-sm text-muted-foreground transition hover:bg-muted/50"
          onClick={() => { void load() }}
          type="button"
        >
          重新加载
        </button>
      </div>
    </div>
  )
}

function RuntimeSection({ runtime }: { runtime: AdminCosyvoiceRuntimeStatus | null }) {
  if (!runtime) return null
  return (
    <Section
      title="运行时状态"
      description="这些来自 env 或健康探针，只读显示。密钥只显示是否已配置，不显示值。"
    >
      <div className="grid gap-3 md:grid-cols-2">
        <StatusRow
          label="手动克隆 runtime"
          ok={runtime.manual_clone_runtime_ready}
          detail={runtime.manual_clone_runtime_unavailable_code ?? 'ready'}
        />
        <StatusRow
          label="Mainland worker"
          ok={runtime.mainland_worker.config_ready}
          detail={`enabled=${runtime.mainland_worker.effective_enabled}`}
        />
        <StatusRow
          label="Worker URL / HMAC"
          ok={
            runtime.mainland_worker.url_configured &&
            runtime.mainland_worker.hmac_key_id_configured &&
            runtime.mainland_worker.hmac_secret_configured
          }
          detail={`url=${statusWord(runtime.mainland_worker.url_configured)}, key=${statusWord(runtime.mainland_worker.hmac_key_id_configured)}, secret=${statusWord(runtime.mainland_worker.hmac_secret_configured)}`}
        />
        <StatusRow
          label="Sample uploader"
          ok={runtime.sample_uploader.production_ready && runtime.sample_uploader.missing_config_fields.length === 0}
          detail={
            runtime.sample_uploader.missing_config_fields.length > 0
              ? `${runtime.sample_uploader.backend}: missing ${runtime.sample_uploader.missing_config_fields.join(', ')}`
              : runtime.sample_uploader.backend
          }
        />
        <StatusRow
          label="临时音色 cleanup"
          ok={runtime.cleanup.dry_run_effective}
          detail={runtime.cleanup.dry_run_effective ? 'dry-run' : 'execute mode'}
        />
      </div>
    </Section>
  )
}

function Section({
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
      <p className="mb-4 mt-1 text-sm text-muted-foreground">{description}</p>
      {children}
    </section>
  )
}

function ToggleField({
  label,
  description,
  checked,
  onChange,
  danger = false,
}: {
  label: string
  description: string
  checked: boolean
  onChange: (checked: boolean) => void
  danger?: boolean
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-border bg-muted/30 p-4 transition hover:bg-muted/50">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-1 h-4 w-4 rounded border-border"
      />
      <span>
        <span className="block text-sm font-medium text-foreground">
          {label}
          {danger ? (
            <span className="ml-2 rounded bg-[color:var(--cinnabar)]/15 px-1.5 py-0.5 text-[10px] text-[color:var(--cinnabar)]">
              付费路径
            </span>
          ) : null}
        </span>
        <span className="mt-1 block text-xs text-muted-foreground">{description}</span>
      </span>
    </label>
  )
}

function SelectField({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: { value: string; label: string }[]
  onChange: (value: string) => void
}) {
  return (
    <label className="block">
      <span className="mb-2 block text-sm font-medium text-foreground">{label}</span>
      <select
        className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}

function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  step = 1,
}: {
  label: string
  value: number
  onChange: (value: number) => void
  min: number
  max: number
  step?: number
}) {
  return (
    <label className="block">
      <span className="mb-2 block text-sm font-medium text-foreground">{label}</span>
      <input
        className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground"
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => {
          const next = Number(event.target.value)
          if (Number.isFinite(next)) onChange(next)
        }}
      />
    </label>
  )
}

function AllowlistField({
  label,
  value,
  onChange,
}: {
  label: string
  value: string[]
  onChange: (value: string[]) => void
}) {
  return (
    <label className="block">
      <span className="mb-2 block text-sm font-medium text-foreground">{label}</span>
      <textarea
        className="min-h-24 w-full rounded-lg border border-border bg-background px-3 py-2 font-mono text-xs text-foreground"
        value={value.join('\n')}
        onChange={(event) => onChange(parseAllowlist(event.target.value))}
        placeholder="每行一个 user_id，也可以用逗号分隔"
      />
    </label>
  )
}

function StatusRow({ label, ok, detail }: { label: string; ok: boolean; detail: string }) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg border border-border bg-muted/20 p-3">
      <div>
        <p className="text-sm font-medium text-foreground">{label}</p>
        <p className="mt-1 break-all text-xs text-muted-foreground">{detail}</p>
      </div>
      <span
        className={`shrink-0 rounded-full px-2 py-1 text-xs font-semibold ${
          ok
            ? 'bg-emerald-500/15 text-emerald-700'
            : 'bg-[color:var(--cinnabar)]/15 text-[color:var(--cinnabar)]'
        }`}
      >
        {ok ? 'OK' : '检查'}
      </span>
    </div>
  )
}

function parseAllowlist(raw: string) {
  return raw
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function statusWord(ok: boolean) {
  return ok ? 'set' : 'missing'
}
