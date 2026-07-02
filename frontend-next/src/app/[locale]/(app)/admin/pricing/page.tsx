"use client"

import { useEffect, useState, useCallback } from "react"
import { toast } from "sonner"
import {
  getAdminPricing,
  savePricingDraft,
  publishPricing,
  listPricingHistory,
  ForbiddenError,
  type PricingPayload,
  type PricingVersion,
  type PlanConfig,
  type TrialConfig,
  type CreditsConfig,
  type TopupConfig,
  type TopupPackage,
  type CostModelConfig,
} from "@/lib/admin/pricing"

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

/** Format fen to yuan display */
function fenToYuan(fen: number): string {
  return (fen / 100).toFixed(2)
}

/** Format ISO date to readable string */
function fmtDate(iso: string | null): string {
  if (!iso) return "-"
  const d = new Date(iso)
  return d.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })
}

const STATUS_LABELS: Record<string, string> = {
  active: "生效中",
  draft: "草稿",
  archived: "已归档",
}

// ---------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------

export default function AdminPricingPage() {
  const [active, setActive] = useState<PricingVersion | null>(null)
  const [draft, setDraft] = useState<PricingPayload | null>(null)
  const [history, setHistory] = useState<PricingVersion[]>([])
  const [loading, setLoading] = useState(true)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [changeNote, setChangeNote] = useState("")
  const [publishing, setPublishing] = useState(false)
  const [saving, setSaving] = useState(false)

  // which section is in edit mode
  const [editingSection, setEditingSection] = useState<string | null>(null)

  // load data
  const loadData = useCallback(async () => {
    try {
      const [pricing, hist] = await Promise.all([
        getAdminPricing(),
        listPricingHistory(),
      ])
      setActive(pricing.active)
      setDraft(pricing.draft?.payload ?? structuredClone(pricing.active.payload))
      setHistory(hist)
    } catch (err) {
      if (err instanceof ForbiddenError) {
        setForbidden(true)
        return
      }
      const msg = err instanceof Error ? err.message : "加载失败"
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadData()
  }, [loadData])

  // convenience: current payload being edited
  const payload = draft

  const updateDraft = <K extends keyof PricingPayload>(
    key: K,
    value: PricingPayload[K],
  ) => {
    setDraft((prev) => (prev ? { ...prev, [key]: value } : prev))
  }

  // save draft
  const handleSaveDraft = async () => {
    if (!payload) return
    setSaving(true)
    try {
      await savePricingDraft(payload)
      toast.success("草稿已保存")
      void loadData()
    } catch (err) {
      const msg = err instanceof Error ? err.message : "保存失败"
      toast.error(msg)
    } finally {
      setSaving(false)
    }
  }

  // publish
  const handlePublish = async () => {
    if (!payload) return
    if (!changeNote.trim()) {
      toast.error("请填写变更说明")
      return
    }
    setPublishing(true)
    try {
      await publishPricing(payload, changeNote.trim())
      toast.success("定价已发布")
      setChangeNote("")
      setEditingSection(null)
      void loadData()
    } catch (err) {
      const msg = err instanceof Error ? err.message : "发布失败"
      toast.error(msg)
    } finally {
      setPublishing(false)
    }
  }

  // ------ render states ------

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      </div>
    )
  }

  if (forbidden) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3">
        <div className="rounded-2xl border border-[color:var(--cinnabar)]/20 bg-[color:var(--cinnabar)]/5 p-6 text-center">
          <p className="text-lg font-semibold text-[color:var(--cinnabar)]">
            仅管理员可访问
          </p>
          <p className="text-sm text-muted-foreground mt-2">
            您没有权限查看此页面。
          </p>
        </div>
      </div>
    )
  }

  if (!payload || !active) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3">
        <div className="rounded-2xl border border-border bg-card p-6 text-center">
          <p className="text-lg font-semibold text-foreground">
            暂无定价数据
          </p>
          <p className="text-sm text-muted-foreground mt-2">
            请先通过后端 API 创建初始定价版本。
          </p>
        </div>
      </div>
    )
  }

  const frozen = payload.catalog_frozen

  return (
    <div className="space-y-6 max-w-4xl">
      {/* page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold font-heading text-foreground">
            定价管理
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            管理套餐、点数、Top-up 与成本参数 (v{active.payload.version})
          </p>
        </div>
        {frozen && (
          <span className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium border border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/12 text-[color:var(--ochre)]">
            <LockIcon />
            catalog_frozen
          </span>
        )}
      </div>

      {error && (
        <div className="rounded-xl border border-[color:var(--cinnabar)]/20 bg-[color:var(--cinnabar)]/5 p-4 text-sm text-[color:var(--cinnabar)]">
          {error}
        </div>
      )}

      {/* Section 1: Plans & Trial */}
      <SectionCard
        title="套餐与 Trial"
        description="用户套餐配置与试用期参数"
        frozen={frozen}
        editing={editingSection === "plans"}
        onToggleEdit={() =>
          setEditingSection((s) => (s === "plans" ? null : "plans"))
        }
      >
        {editingSection === "plans" ? (
          <PlansEditor
            plans={payload.plans}
            trial={payload.trial}
            freeGrantCredits={payload.credits.free_grant_credits}
            frozen={frozen}
            onChange={(plans, trial) => {
              updateDraft("plans", plans)
              updateDraft("trial", trial)
            }}
          />
        ) : (
          <PlansDisplay
            plans={payload.plans}
            trial={payload.trial}
            freeGrantCredits={payload.credits.free_grant_credits}
          />
        )}
      </SectionCard>

      {/* Section 2: Credits */}
      <SectionCard
        title="点数策略"
        description="扣点费率、扣减优先级、音色克隆成本"
        frozen={frozen}
        editing={editingSection === "credits"}
        onToggleEdit={() =>
          setEditingSection((s) => (s === "credits" ? null : "credits"))
        }
      >
        {editingSection === "credits" ? (
          <CreditsEditor
            credits={payload.credits}
            frozen={frozen}
            onChange={(c) => updateDraft("credits", c)}
          />
        ) : (
          <CreditsDisplay credits={payload.credits} />
        )}
      </SectionCard>

      {/* Section 3: Topup */}
      <SectionCard
        title="Top-up 点数包"
        description="充值包配置"
        frozen={false}
        editing={editingSection === "topup"}
        onToggleEdit={() =>
          setEditingSection((s) => (s === "topup" ? null : "topup"))
        }
      >
        {editingSection === "topup" ? (
          <TopupEditor
            topup={payload.topup}
            onChange={(t) => updateDraft("topup", t)}
          />
        ) : (
          <TopupDisplay topup={payload.topup} />
        )}
      </SectionCard>

      {/* Section 4: Cost Model */}
      <SectionCard
        title="成本校准"
        description="以下参数用于成本测算和 pilot 观测，不直接影响用户扣点"
        frozen={false}
        editing={editingSection === "cost"}
        onToggleEdit={() =>
          setEditingSection((s) => (s === "cost" ? null : "cost"))
        }
      >
        {editingSection === "cost" ? (
          <CostModelEditor
            costModel={payload.cost_model}
            onChange={(c) => updateDraft("cost_model", c)}
          />
        ) : (
          <CostModelDisplay costModel={payload.cost_model} />
        )}
      </SectionCard>

      {/* Section 5: Publish & History */}
      <section className="rounded-2xl border border-border bg-card p-6 space-y-6">
        <div>
          <h2 className="text-lg font-semibold text-foreground">
            发布与版本历史
          </h2>
          <p className="text-sm text-muted-foreground mt-1">
            保存草稿或发布新版本
          </p>
        </div>

        {/* change note + buttons */}
        <div className="space-y-3">
          <label className="block text-sm font-medium text-foreground">
            变更说明
          </label>
          <input
            type="text"
            value={changeNote}
            onChange={(e) => setChangeNote(e.target.value)}
            placeholder="描述本次变更内容..."
            className="w-full rounded-lg border border-border bg-muted/30 px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary/40"
          />
          <div className="flex gap-3">
            <button
              type="button"
              disabled={publishing}
              onClick={() => void handlePublish()}
              className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 disabled:opacity-50"
            >
              {publishing ? "发布中..." : "发布"}
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => void handleSaveDraft()}
              className="rounded-lg border border-border bg-muted/30 px-4 py-2 text-sm text-muted-foreground transition hover:bg-muted/50 disabled:opacity-50"
            >
              {saving ? "保存中..." : "保存草稿"}
            </button>
          </div>
        </div>

        {/* history table */}
        {history.length > 0 && (
          <div>
            <h3 className="text-sm font-semibold text-foreground mb-3">
              版本历史
            </h3>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-muted-foreground">
                    <th className="pb-2 pr-4 font-medium">版本</th>
                    <th className="pb-2 pr-4 font-medium">状态</th>
                    <th className="pb-2 pr-4 font-medium">变更说明</th>
                    <th className="pb-2 pr-4 font-medium">创建时间</th>
                    <th className="pb-2 font-medium">生效时间</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((v) => (
                    <tr
                      key={v.version}
                      className="border-b border-border/50 last:border-0"
                    >
                      <td className="py-2 pr-4 font-mono text-foreground">
                        v{v.version}
                      </td>
                      <td className="py-2 pr-4">
                        <span
                          className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${
                            v.status === "active"
                              ? "bg-[color:var(--bamboo)]/12 text-[color:var(--bamboo)]"
                              : v.status === "draft"
                                ? "bg-[color:var(--ochre)]/12 text-[color:var(--ochre)]"
                                : "bg-muted text-muted-foreground"
                          }`}
                        >
                          {STATUS_LABELS[v.status] ?? v.status}
                        </span>
                      </td>
                      <td className="py-2 pr-4 text-muted-foreground max-w-[200px] truncate">
                        {v.change_note || "-"}
                      </td>
                      <td className="py-2 pr-4 text-muted-foreground whitespace-nowrap">
                        {fmtDate(v.created_at)}
                      </td>
                      <td className="py-2 text-muted-foreground whitespace-nowrap">
                        {fmtDate(v.activated_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>
    </div>
  )
}

// ---------------------------------------------------------------------------
// shared components
// ---------------------------------------------------------------------------

function LockIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect width="18" height="11" x="3" y="11" rx="2" ry="2" />
      <path d="M7 11V7a5 5 0 0 1 10 0v4" />
    </svg>
  )
}

function SectionCard({
  title,
  description,
  frozen,
  editing,
  onToggleEdit,
  children,
}: {
  title: string
  description: string
  frozen: boolean
  editing: boolean
  onToggleEdit: () => void
  children: React.ReactNode
}) {
  return (
    <section className="rounded-2xl border border-border bg-card p-6">
      <div className="flex items-start justify-between mb-4">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-semibold text-foreground">{title}</h2>
            {frozen && (
              <span className="text-[color:var(--ochre)]" title="catalog_frozen: 修改需变更说明">
                <LockIcon />
              </span>
            )}
          </div>
          <p className="text-sm text-muted-foreground mt-1">{description}</p>
        </div>
        <button
          type="button"
          onClick={onToggleEdit}
          className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition ${
            editing
              ? "border-primary/50 bg-primary/10 text-primary"
              : "border-border bg-muted/30 text-muted-foreground hover:bg-muted/50"
          }`}
        >
          {editing ? "收起" : "编辑"}
        </button>
      </div>
      {children}
    </section>
  )
}

function KvRow({
  label,
  value,
  frozen,
}: {
  label: string
  value: string | number
  frozen?: boolean
}) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-sm text-muted-foreground">
        {label}
        {frozen && (
          <span className="ml-1 text-[color:var(--ochre)]" title="frozen">
            <LockIcon />
          </span>
        )}
      </span>
      <span className="text-sm font-mono text-foreground">{value}</span>
    </div>
  )
}

function NumberInput({
  label,
  value,
  onChange,
  suffix,
  step,
  min,
  frozen,
}: {
  label: string
  value: number
  onChange: (v: number) => void
  suffix?: string
  step?: number
  min?: number
  frozen?: boolean
}) {
  return (
    <div className="flex items-center gap-3 py-1">
      <span className="text-sm text-muted-foreground flex-1">
        {label}
        {frozen && (
          <span className="ml-1 text-[color:var(--ochre)]">
            <LockIcon />
          </span>
        )}
      </span>
      <div className="rounded-lg border border-border bg-muted/30 transition hover:border-primary/30 focus-within:border-primary/40">
        <input
          type="number"
          value={value}
          step={step ?? 1}
          min={min ?? 0}
          onChange={(e) => onChange(Number(e.target.value))}
          className="w-24 rounded-lg bg-transparent px-3 py-1.5 text-sm text-foreground text-right focus:outline-none"
        />
      </div>
      {suffix && (
        <span className="text-xs text-muted-foreground">{suffix}</span>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Section 1: Plans & Trial — display + editor
// ---------------------------------------------------------------------------

function PlansDisplay({
  plans,
  trial,
  freeGrantCredits,
}: {
  plans: Record<string, PlanConfig>
  trial: TrialConfig
  freeGrantCredits: number
}) {
  return (
    <div className="space-y-4">
      {Object.entries(plans).map(([key, plan]) => (
        <div
          key={key}
          className="rounded-xl border border-border bg-muted/20 p-4"
        >
          <div className="flex items-center gap-2 mb-2">
            <span className="text-sm font-semibold text-foreground">
              {plan.display_name}
            </span>
            <span className="text-xs font-mono text-muted-foreground">
              ({key})
            </span>
            {plan.self_serve && (
              <span className="rounded-full px-2 py-0.5 text-[10px] bg-[color:var(--bamboo)]/12 text-[color:var(--bamboo)]">
                自助开通
              </span>
            )}
          </div>
          <div className="grid grid-cols-2 gap-x-6 text-sm">
            {plan.free_quota_total !== null && (
              <KvRow
                label="免费任务额度"
                value={`${plan.free_quota_total} 次`}
              />
            )}
            <KvRow
              label="最大时长"
              value={`${plan.max_duration_minutes} 分钟`}
            />
            <KvRow label="并发数" value={plan.max_concurrent_jobs} />
            <KvRow
              label="服务模式"
              value={plan.allowed_service_modes.join(", ")}
            />
            {plan.price_cny_fen && (
              <>
                <KvRow
                  label="月付"
                  value={`${fenToYuan(plan.price_cny_fen.monthly)} 元`}
                  frozen
                />
                <KvRow
                  label="季付"
                  value={`${fenToYuan(plan.price_cny_fen.quarterly)} 元`}
                  frozen
                />
                <KvRow
                  label="年付"
                  value={`${fenToYuan(plan.price_cny_fen.annual)} 元`}
                  frozen
                />
              </>
            )}
            {plan.monthly_grant_credits !== null && (
              <KvRow
                label="月赠点数"
                value={plan.monthly_grant_credits}
                frozen
              />
            )}
            {key === "free" && (
              <KvRow
                label="赠送点数"
                value={`${freeGrantCredits} 点`}
              />
            )}
          </div>
          {key === "free" && (
            <p className="mt-3 text-xs text-[color:var(--ochre)]">
              当前 Free 仍同时受旧任务额度与新点数赠送约束；V3 credits 真值切换后会进一步收口。
            </p>
          )}
        </div>
      ))}

      {/* Trial */}
      <div className="rounded-xl border border-border bg-muted/20 p-4">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-sm font-semibold text-foreground">
            试用期
          </span>
          {trial.frozen && (
            <span className="text-[color:var(--ochre)]">
              <LockIcon />
            </span>
          )}
        </div>
        <div className="grid grid-cols-2 gap-x-6 text-sm">
          <KvRow label="天数" value={`${trial.days} 天`} frozen />
          <KvRow
            label="素材分钟"
            value={`${trial.source_minutes} 分钟`}
            frozen
          />
          <KvRow
            label="含 Studio"
            value={trial.includes_studio ? "是" : "否"}
          />
          <KvRow
            label="需手机号"
            value={trial.phone_required ? "是" : "否"}
          />
          <KvRow
            label="自动扣费"
            value={trial.auto_charge ? "是" : "否"}
          />
          <KvRow label="到期套餐" value={trial.fallback_plan} />
          <KvRow label="赠送点数" value={trial.grant_credits} frozen />
        </div>
      </div>
    </div>
  )
}

function PlansEditor({
  plans,
  trial,
  freeGrantCredits,
  frozen,
  onChange,
}: {
  plans: Record<string, PlanConfig>
  trial: TrialConfig
  freeGrantCredits: number
  frozen: boolean
  onChange: (plans: Record<string, PlanConfig>, trial: TrialConfig) => void
}) {
  const updatePlan = (key: string, patch: Partial<PlanConfig>) => {
    const updated = { ...plans, [key]: { ...plans[key], ...patch } }
    onChange(updated, trial)
  }

  const updateTrial = (patch: Partial<TrialConfig>) => {
    onChange(plans, { ...trial, ...patch })
  }

  return (
    <div className="space-y-4">
      {Object.entries(plans).map(([key, plan]) => (
        <div
          key={key}
          className="rounded-xl border border-border bg-muted/20 p-4 space-y-2"
        >
          <p className="text-sm font-semibold text-foreground">
            {plan.display_name}{" "}
            <span className="font-mono text-muted-foreground text-xs">
              ({key})
            </span>
          </p>
          {plan.free_quota_total !== null && (
            <>
              <NumberInput
                label="免费任务额度(次)"
                value={plan.free_quota_total}
                onChange={(v) =>
                  updatePlan(key, { free_quota_total: v || null })
                }
              />
              <div className="rounded-lg px-3 py-2 text-xs border border-[color:var(--ochre)]/25 bg-[color:var(--ochre)]/8 text-[color:var(--ochre)]">
                <div>赠送点数：{freeGrantCredits} 点</div>
                <div className="mt-1">
                  Free 赠点请到“点数策略”中的“免费赠点”调整，避免重复真值。
                </div>
              </div>
            </>
          )}
          <NumberInput
            label="最大时长(分钟)"
            value={plan.max_duration_minutes}
            onChange={(v) =>
              updatePlan(key, { max_duration_minutes: v })
            }
          />
          <NumberInput
            label="并发数"
            value={plan.max_concurrent_jobs}
            onChange={(v) =>
              updatePlan(key, { max_concurrent_jobs: v })
            }
          />
          {plan.price_cny_fen && (
            <>
              <NumberInput
                label="月付(分)"
                value={plan.price_cny_fen.monthly}
                frozen={frozen}
                onChange={(v) =>
                  updatePlan(key, {
                    price_cny_fen: { ...plan.price_cny_fen!, monthly: v },
                  })
                }
              />
              <NumberInput
                label="季付(分)"
                value={plan.price_cny_fen.quarterly}
                frozen={frozen}
                onChange={(v) =>
                  updatePlan(key, {
                    price_cny_fen: {
                      ...plan.price_cny_fen!,
                      quarterly: v,
                    },
                  })
                }
              />
              <NumberInput
                label="年付(分)"
                value={plan.price_cny_fen.annual}
                frozen={frozen}
                onChange={(v) =>
                  updatePlan(key, {
                    price_cny_fen: { ...plan.price_cny_fen!, annual: v },
                  })
                }
              />
            </>
          )}
          {plan.price_usd_cents && (
            <>
              <NumberInput
                label="PayPal 月付(美分)"
                value={plan.price_usd_cents.monthly}
                frozen={frozen}
                onChange={(v) =>
                  updatePlan(key, {
                    price_usd_cents: { ...plan.price_usd_cents!, monthly: v },
                  })
                }
              />
              <NumberInput
                label="PayPal 季付(美分)"
                value={plan.price_usd_cents.quarterly}
                frozen={frozen}
                onChange={(v) =>
                  updatePlan(key, {
                    price_usd_cents: {
                      ...plan.price_usd_cents!,
                      quarterly: v,
                    },
                  })
                }
              />
              <NumberInput
                label="PayPal 年付(美分)"
                value={plan.price_usd_cents.annual}
                frozen={frozen}
                onChange={(v) =>
                  updatePlan(key, {
                    price_usd_cents: { ...plan.price_usd_cents!, annual: v },
                  })
                }
              />
            </>
          )}
          {plan.monthly_grant_credits !== null && (
            <NumberInput
              label="月赠点数"
              value={plan.monthly_grant_credits}
              frozen={frozen}
              onChange={(v) =>
                updatePlan(key, { monthly_grant_credits: v })
              }
            />
          )}
        </div>
      ))}

      {/* Trial editor */}
      <div className="rounded-xl border border-border bg-muted/20 p-4 space-y-2">
        <p className="text-sm font-semibold text-foreground">试用期</p>
        <NumberInput
          label="天数"
          value={trial.days}
          frozen={frozen}
          onChange={(v) => updateTrial({ days: v })}
        />
        <NumberInput
          label="素材分钟"
          value={trial.source_minutes}
          frozen={frozen}
          onChange={(v) => updateTrial({ source_minutes: v })}
        />
        <NumberInput
          label="赠送点数"
          value={trial.grant_credits}
          frozen={frozen}
          onChange={(v) => updateTrial({ grant_credits: v })}
        />
        <div className="flex items-center gap-3 py-1">
          <span className="text-sm text-muted-foreground flex-1">
            到期套餐
          </span>
          <input
            type="text"
            value={trial.fallback_plan}
            onChange={(e) =>
              updateTrial({ fallback_plan: e.target.value })
            }
            className="w-32 rounded-lg border border-border bg-muted/30 px-3 py-1.5 text-sm text-foreground text-right focus:outline-none focus:border-primary/40"
          />
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Section 2: Credits — display + editor
// ---------------------------------------------------------------------------

function CreditsDisplay({ credits }: { credits: CreditsConfig }) {
  return (
    <div className="space-y-3">
      <KvRow label="Free 用户赠点" value={credits.free_grant_credits} />
      <KvRow
        label="音色克隆成本"
        value={`${credits.voice_clone_cost_credits} 点`}
      />

      <div className="pt-2">
        <p className="text-xs font-semibold text-muted-foreground mb-1.5">
          扣点费率 (debit_rates)
        </p>
        <div className="rounded-lg border border-border bg-muted/20 p-3 space-y-1">
          {Object.entries(credits.debit_rates).map(([k, v]) => (
            <KvRow key={k} label={k} value={v} frozen />
          ))}
        </div>
      </div>

      <div className="pt-2">
        <p className="text-xs font-semibold text-muted-foreground mb-1.5">
          扣减优先级 (bucket_priority)
        </p>
        <div className="rounded-lg border border-border bg-muted/20 p-3 space-y-1">
          {Object.entries(credits.bucket_priority).map(([k, v]) => (
            <KvRow key={k} label={k} value={v.join(" > ")} />
          ))}
        </div>
      </div>
    </div>
  )
}

function CreditsEditor({
  credits,
  frozen,
  onChange,
}: {
  credits: CreditsConfig
  frozen: boolean
  onChange: (c: CreditsConfig) => void
}) {
  const updateRate = (key: string, value: number) => {
    onChange({
      ...credits,
      debit_rates: { ...credits.debit_rates, [key]: value },
    })
  }

  return (
    <div className="space-y-3">
      <NumberInput
        label="Free 用户赠点"
        value={credits.free_grant_credits}
        onChange={(v) => onChange({ ...credits, free_grant_credits: v })}
      />
      <NumberInput
        label="音色克隆成本(点)"
        value={credits.voice_clone_cost_credits}
        onChange={(v) =>
          onChange({ ...credits, voice_clone_cost_credits: v })
        }
      />

      <div className="pt-2">
        <p className="text-xs font-semibold text-muted-foreground mb-1.5">
          扣点费率 (debit_rates)
        </p>
        <div className="rounded-lg border border-border bg-muted/20 p-3 space-y-1">
          {Object.entries(credits.debit_rates).map(([k, v]) => (
            <NumberInput
              key={k}
              label={k}
              value={v}
              step={0.1}
              frozen={frozen}
              onChange={(val) => updateRate(k, val)}
            />
          ))}
        </div>
      </div>

      <div className="pt-2">
        <p className="text-xs font-semibold text-muted-foreground mb-1.5">
          扣减优先级 (bucket_priority) — 只读
        </p>
        <div className="rounded-lg border border-border bg-muted/20 p-3 space-y-1">
          {Object.entries(credits.bucket_priority).map(([k, v]) => (
            <KvRow key={k} label={k} value={v.join(" > ")} />
          ))}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Section 3: Topup — display + editor
// ---------------------------------------------------------------------------

function TopupDisplay({ topup }: { topup: TopupConfig }) {
  return (
    <div className="space-y-3">
      <KvRow label="充值功能" value={topup.enabled ? "开启" : "关闭"} />
      <div className="space-y-2">
        {topup.packages.map((pkg) => (
          <div
            key={pkg.code}
            className={`rounded-lg border p-3 flex items-center justify-between ${
              pkg.active
                ? "border-border bg-muted/20"
                : "border-border/50 bg-muted/10 opacity-60"
            }`}
          >
            <div>
              <span className="text-sm font-medium text-foreground">
                {pkg.code}
              </span>
              <span className="ml-2 text-xs text-muted-foreground">
                {pkg.credits} 点
              </span>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-sm font-mono text-foreground">
                {fenToYuan(pkg.price_cny_fen)} 元
              </span>
              {pkg.price_usd_cents != null && pkg.price_usd_cents > 0 && (
                <span className="text-xs font-mono text-muted-foreground">
                  ${(pkg.price_usd_cents / 100).toFixed(2)}
                </span>
              )}
              {!pkg.active && (
                <span className="text-xs text-muted-foreground">
                  (已下架)
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function TopupEditor({
  topup,
  onChange,
}: {
  topup: TopupConfig
  onChange: (t: TopupConfig) => void
}) {
  const updatePkg = (idx: number, patch: Partial<TopupPackage>) => {
    const updated = topup.packages.map((p, i) =>
      i === idx ? { ...p, ...patch } : p,
    )
    onChange({ ...topup, packages: updated })
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 py-1">
        <span className="text-sm text-muted-foreground flex-1">
          充值功能
        </span>
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={topup.enabled}
            onChange={(e) =>
              onChange({ ...topup, enabled: e.target.checked })
            }
            className="h-4 w-4 rounded border-border"
          />
          <span className="text-sm text-foreground">
            {topup.enabled ? "开启" : "关闭"}
          </span>
        </label>
      </div>

      {topup.packages.map((pkg, idx) => (
        <div
          key={pkg.code}
          className="rounded-lg border border-border bg-muted/20 p-3 space-y-2"
        >
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-foreground">
              {pkg.code}
            </span>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={pkg.active}
                onChange={(e) =>
                  updatePkg(idx, { active: e.target.checked })
                }
                className="h-3.5 w-3.5 rounded border-border"
              />
              <span className="text-xs text-muted-foreground">
                {pkg.active ? "上架" : "下架"}
              </span>
            </label>
          </div>
          <NumberInput
            label="点数"
            value={pkg.credits}
            onChange={(v) => updatePkg(idx, { credits: v })}
          />
          <NumberInput
            label="价格(分)"
            value={pkg.price_cny_fen}
            onChange={(v) => updatePkg(idx, { price_cny_fen: v })}
          />
          <NumberInput
            label="PayPal 美元价(美分, 0=不开)"
            value={pkg.price_usd_cents ?? 0}
            onChange={(v) =>
              updatePkg(idx, { price_usd_cents: v > 0 ? v : null })
            }
          />
          <NumberInput
            label="排序"
            value={pkg.sort_order}
            onChange={(v) => updatePkg(idx, { sort_order: v })}
          />
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Section 4: Cost Model — display + editor
// ---------------------------------------------------------------------------

const COST_LABELS: Record<string, string> = {
  point_cost_rmb: "每点成本 (元)",
  point_price_rmb: "每点售价 (元)",
  target_gross_margin: "目标毛利率",
  k_cn_chars_per_src_min: "千中文字/素材分钟",
  fx_usd_cny: "美元兑人民币汇率",
  translate_cost_rmb_per_src_min: "翻译成本 (元/素材分钟)",
  s2_review_cost_rmb_per_src_min: "审校成本 (元/素材分钟)",
  rewrite_cost_rmb_per_src_min: "重写成本 (元/素材分钟)",
  server_cost_rmb_per_src_min: "服务器成本 (元/素材分钟)",
}

function CostModelDisplay({ costModel }: { costModel: CostModelConfig }) {
  return (
    <div className="space-y-1">
      {Object.entries(costModel).map(([k, v]) => (
        <KvRow
          key={k}
          label={COST_LABELS[k] ?? k}
          value={typeof v === "number" ? v : String(v)}
        />
      ))}
    </div>
  )
}

function CostModelEditor({
  costModel,
  onChange,
}: {
  costModel: CostModelConfig
  onChange: (c: CostModelConfig) => void
}) {
  return (
    <div className="space-y-1">
      {(Object.keys(costModel) as (keyof CostModelConfig)[]).map((k) => (
        <NumberInput
          key={k}
          label={COST_LABELS[k] ?? k}
          value={costModel[k]}
          step={k === "target_gross_margin" ? 0.01 : 0.001}
          onChange={(v) => onChange({ ...costModel, [k]: v })}
        />
      ))}
    </div>
  )
}
