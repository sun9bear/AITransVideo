"use client"

/**
 * Admin support management page (plan 2026-05-08 §7.2 P1 scope).
 *
 * P1 surface, deliberately minimal:
 *   - Overview (counts + budget state).
 *   - Model picker (defaults to deepseek / DeepSeek V4 Flash).
 *   - Monthly budget cap + token unit price config.
 *   - Sensitive keyword editor (single textarea, one keyword per line).
 *   - Ops email field (default sxz999@proton.me).
 *   - Budget exhausted message.
 *
 * Anything else (template editor, channel config, retention, indices)
 * is P2+ per §7.2 / §16.5.
 */

import { useEffect, useState } from "react"

import { HandoffTicketsPanel } from "@/components/support/admin/HandoffTicketsPanel"
import { PresenceConfigCard } from "@/components/support/admin/PresenceConfigCard"
import { WeChatQrCard } from "@/components/support/admin/WeChatQrCard"

interface SupportSettings {
  support_enabled: boolean
  support_anonymous_enabled: boolean
  support_ai_enabled: boolean
  support_ai_model: string
  support_ai_max_output_tokens: number
  support_ai_monthly_budget_usd: number
  support_ai_input_usd_per_1m_tokens: number
  support_ai_output_usd_per_1m_tokens: number
  support_budget_exhausted_message: string
  support_sensitive_keywords: string[]
  support_ops_email: string
  // Human handoff routing (L1, plan 2026-05-08 follow-up)
  support_admin_heartbeat_interval_seconds: number
  support_admin_online_threshold_seconds: number
  support_handoff_offline_fallback_minutes: number
  support_offline_message: string
}

interface SupportModelOption {
  value: string
  label: string
  cost_hint?: string
  enabled?: boolean
  provider?: string
  // Codex round 2 (2026-05-08): true iff this model's provider has a
  // real, reviewed support implementation. False means the model is
  // selectable but will silently fall back to fake until wiring lands.
  support_implemented?: boolean
}

interface SupportSettingsResponse {
  settings: SupportSettings
  budget_state: "normal" | "budget_exhausted"
  spent_this_month_usd: number
  available_models: SupportModelOption[]
}

interface SupportOverview {
  conversations_total: number
  conversations_today: number
  handoff_requests_total: number
  handoff_requests_pending: number
  ai_messages_this_month: number
  template_hits_this_month: number
  spent_this_month_usd: number
  budget_state: "normal" | "budget_exhausted"
}

const DEFAULTS: SupportSettings = {
  // Codex P2-1 (2026-05-08): start dark; admin must explicitly opt in.
  support_enabled: false,
  support_anonymous_enabled: false,
  support_ai_enabled: false,
  support_ai_model: "deepseek",
  support_ai_max_output_tokens: 400,
  support_ai_monthly_budget_usd: 50,
  support_ai_input_usd_per_1m_tokens: 0.14,
  support_ai_output_usd_per_1m_tokens: 0.28,
  support_budget_exhausted_message:
    "AI 客服当前繁忙，你可以先查看常见问题，或转人工客服处理。",
  support_sensitive_keywords: [
    "人工",
    "退款",
    "投诉",
    "差评",
    "工信部",
    "315",
    "赔偿",
    "举报",
  ],
  support_ops_email: "sxz999@proton.me",
  support_admin_heartbeat_interval_seconds: 30,
  support_admin_online_threshold_seconds: 60,
  support_handoff_offline_fallback_minutes: 5,
  support_offline_message: "运营暂未在线，可扫码添加客服微信，我们尽快回复。",
}

async function getJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(url, {
    credentials: "include",
    headers: { Accept: "application/json", ...(init.headers ?? {}) },
    ...init,
  })
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`)
  }
  return (await res.json()) as T
}

export default function AdminSupportPage() {
  const [settings, setSettings] = useState<SupportSettings>(DEFAULTS)
  const [available, setAvailable] = useState<
    SupportSettingsResponse["available_models"]
  >([])
  const [budgetState, setBudgetState] = useState<"normal" | "budget_exhausted">(
    "normal",
  )
  const [spent, setSpent] = useState(0)
  const [overview, setOverview] = useState<SupportOverview | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const reload = async () => {
    setLoading(true)
    setError(null)
    try {
      const [settingsRes, overviewRes] = await Promise.all([
        getJson<SupportSettingsResponse>("/api/admin/support/settings"),
        getJson<SupportOverview>("/api/admin/support/overview"),
      ])
      setSettings(settingsRes.settings)
      setAvailable(settingsRes.available_models ?? [])
      setBudgetState(settingsRes.budget_state)
      setSpent(settingsRes.spent_this_month_usd)
      setOverview(overviewRes)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败")
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const body = {
        ...settings,
        support_sensitive_keywords: settings.support_sensitive_keywords
          .map((k) => k.trim())
          .filter(Boolean),
      }
      const res = await getJson<SupportSettingsResponse>(
        "/api/admin/support/settings",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      )
      setSettings(res.settings)
      setBudgetState(res.budget_state)
      setSpent(res.spent_this_month_usd)
      setSavedAt(new Date().toLocaleTimeString("zh-CN"))
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败")
    } finally {
      setSaving(false)
    }
  }

  const update = <K extends keyof SupportSettings>(
    key: K,
    value: SupportSettings[K],
  ) => setSettings((prev) => ({ ...prev, [key]: value }))

  if (loading) {
    return <p className="text-sm text-muted-foreground">加载中…</p>
  }

  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <header>
        <h1 className="ink-display text-2xl text-foreground sm:text-3xl">
          客服管理
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          P1 范围：客服总开关、AI 模型与预算、敏感词、运营邮箱。模板和外部
          渠道配置在 P2+。
        </p>
      </header>

      <section className="grid gap-3 rounded-xl border border-border bg-card p-4 sm:grid-cols-4">
        <Stat label="会话总数" value={overview?.conversations_total ?? 0} />
        <Stat label="今日会话" value={overview?.conversations_today ?? 0} />
        <Stat
          label="待处理人工"
          value={overview?.handoff_requests_pending ?? 0}
        />
        <Stat
          label="本月 LLM 调用"
          value={overview?.ai_messages_this_month ?? 0}
        />
        <Stat
          label="本月模板命中"
          value={overview?.template_hits_this_month ?? 0}
        />
        <Stat
          label="本月预算消耗"
          value={`$${spent.toFixed(4)} / $${settings.support_ai_monthly_budget_usd.toFixed(2)}`}
        />
        <Stat
          label="预算状态"
          value={budgetState === "normal" ? "正常" : "已触顶"}
          highlight={budgetState !== "normal"}
        />
      </section>

      {error ? (
        <p className="rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      ) : null}

      <section className="space-y-4 rounded-xl border border-border bg-card p-4">
        <h2 className="text-base font-semibold text-foreground">
          总开关
        </h2>
        <Toggle
          label="启用客服系统（前端浮窗）"
          checked={settings.support_enabled}
          onChange={(v) => update("support_enabled", v)}
        />
        <Toggle
          label="允许未登录访客咨询（用于售前问答）"
          checked={settings.support_anonymous_enabled}
          onChange={(v) => update("support_anonymous_enabled", v)}
        />
        <Toggle
          label="启用真实 AI Provider（默认 fake，关闭即使用模板/FAQ）"
          checked={settings.support_ai_enabled}
          onChange={(v) => update("support_ai_enabled", v)}
        />
        <p className="text-xs text-muted-foreground">
          注意：开启真实 AI 后，所有 LLM 调用受月度预算上限保护；触顶后会自动
          降级为模板回复。当前 P1 阶段只接入 fake provider；选择其他模型会
          安全降级到 fake，直到对应 provider 的 HTTP wiring 完成并加入实现集合。
        </p>
      </section>

      <section className="space-y-4 rounded-xl border border-border bg-card p-4">
        <h2 className="text-base font-semibold text-foreground">
          大模型管理
        </h2>
        <Field label="客服模型">
          <select
            value={settings.support_ai_model}
            onChange={(e) => update("support_ai_model", e.target.value)}
            className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
          >
            {(available.length > 0
              ? available
              : [{ value: "deepseek", label: "DeepSeek V4 Flash（快速）" }]
            ).map((m) => {
              const tag = m.support_implemented
                ? ""
                : "（P1 未接入客服 → 安全降级到 fake）"
              return (
                <option key={m.value} value={m.value}>
                  {m.label}
                  {m.cost_hint ? ` · ${m.cost_hint}` : ""}
                  {tag ? ` · ${tag}` : ""}
                </option>
              )
            })}
          </select>
          <p className="mt-1 text-xs text-muted-foreground">
            默认 deepseek（解析为 deepseek-v4-flash）。仅文本模型可选；管理面禁用
            的模型不会出现在此列表。**P1 阶段 support 实现集合为空**：选择任何
            模型在「启用真实 AI」打开后都会安全降级到 fake provider，直到
            对应 provider 的 HTTP wiring 完成并被加入 `_IMPLEMENTED_REAL_PROVIDERS`。
          </p>
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="单条最大输出 token">
            <input
              type="number"
              min={1}
              max={4000}
              value={settings.support_ai_max_output_tokens}
              onChange={(e) =>
                update(
                  "support_ai_max_output_tokens",
                  Math.max(1, parseInt(e.target.value, 10) || 0),
                )
              }
              className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
            />
          </Field>
          <Field label="月度预算上限（USD）">
            <input
              type="number"
              min={0}
              step={1}
              value={settings.support_ai_monthly_budget_usd}
              onChange={(e) =>
                update(
                  "support_ai_monthly_budget_usd",
                  Math.max(0, parseFloat(e.target.value) || 0),
                )
              }
              className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
            />
          </Field>
          <Field label="Input 单价（USD / 1M tokens）">
            <input
              type="number"
              min={0}
              step={0.01}
              value={settings.support_ai_input_usd_per_1m_tokens}
              onChange={(e) =>
                update(
                  "support_ai_input_usd_per_1m_tokens",
                  Math.max(0, parseFloat(e.target.value) || 0),
                )
              }
              className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
            />
          </Field>
          <Field label="Output 单价（USD / 1M tokens）">
            <input
              type="number"
              min={0}
              step={0.01}
              value={settings.support_ai_output_usd_per_1m_tokens}
              onChange={(e) =>
                update(
                  "support_ai_output_usd_per_1m_tokens",
                  Math.max(0, parseFloat(e.target.value) || 0),
                )
              }
              className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
            />
          </Field>
        </div>

        <Field label="预算触顶时返回的固定文案">
          <textarea
            rows={3}
            value={settings.support_budget_exhausted_message}
            onChange={(e) =>
              update("support_budget_exhausted_message", e.target.value)
            }
            className="w-full resize-none rounded border border-border bg-background px-2 py-1 text-sm"
          />
        </Field>
      </section>

      <section className="space-y-4 rounded-xl border border-border bg-card p-4">
        <h2 className="text-base font-semibold text-foreground">
          敏感词与人工分流
        </h2>
        <Field label="命中即转人工的关键词（每行一个）">
          <textarea
            rows={6}
            value={settings.support_sensitive_keywords.join("\n")}
            onChange={(e) =>
              update(
                "support_sensitive_keywords",
                e.target.value.split(/\r?\n/).map((s) => s.trim()),
              )
            }
            className="w-full resize-y rounded border border-border bg-background px-2 py-1 text-sm font-mono"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            子串匹配（不区分大小写）。命中后不会调用 LLM，直接进入人工分流。
          </p>
        </Field>
        <Field label="运营接收人工工单的邮箱">
          <input
            type="email"
            value={settings.support_ops_email}
            onChange={(e) => update("support_ops_email", e.target.value)}
            className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            仅在「运营全部不在线 AND 未上传微信 QR」时使用（写入
            runtime_logs/support_handoff_email.log；SMTP 待接）。
          </p>
        </Field>
      </section>

      <PresenceConfigCard
        values={{
          support_admin_heartbeat_interval_seconds:
            settings.support_admin_heartbeat_interval_seconds,
          support_admin_online_threshold_seconds:
            settings.support_admin_online_threshold_seconds,
          support_handoff_offline_fallback_minutes:
            settings.support_handoff_offline_fallback_minutes,
          support_offline_message: settings.support_offline_message,
        }}
        onChange={(patch) => setSettings((s) => ({ ...s, ...patch }))}
      />

      <WeChatQrCard />

      <HandoffTicketsPanel />

      <div className="flex items-center justify-end gap-3">
        {savedAt ? (
          <span className="text-xs text-muted-foreground">
            已保存：{savedAt}
          </span>
        ) : null}
        <button
          type="button"
          disabled={saving}
          onClick={() => void save()}
          className="rounded bg-[color:var(--cinnabar,#C73E3A)] px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {saving ? "保存中…" : "保存"}
        </button>
      </div>
    </div>
  )
}

function Stat({
  label,
  value,
  highlight,
}: {
  label: string
  value: string | number
  highlight?: boolean
}) {
  return (
    <div className="space-y-1">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p
        className={
          "text-base font-semibold " +
          (highlight ? "text-[color:var(--cinnabar,#C73E3A)]" : "text-foreground")
        }
      >
        {value}
      </p>
    </div>
  )
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <label className="flex items-center gap-2 text-sm text-foreground">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4"
      />
      {label}
    </label>
  )
}

function Field({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-foreground">{label}</span>
      {children}
    </label>
  )
}
