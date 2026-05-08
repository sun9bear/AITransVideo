"use client"

/**
 * Admin announcements composer + sent list (plan 2026-05-08 §16.7
 * follow-up §"管理员可以发送系统通知的页面").
 *
 * Three sections stacked vertically:
 *   1. Compose / edit form (title, body, topic, severity, action_url,
 *      audience picker with 2-stage UI, live preview count, save / send /
 *      "试发给自己" actions).
 *   2. Drafts list — clickable to load back into the composer.
 *   3. Sent list — read/archive stats, recall, clone-and-resend.
 *
 * 14 audience kinds are fetched from
 * /api/admin/support/announcements/audience-kinds so adding a new kind
 * server-side surfaces in the UI without a frontend redeploy.
 */

import { useCallback, useEffect, useMemo, useState } from "react"

import {
  adminCloneAnnouncement,
  adminCreateAnnouncement,
  adminDeleteAnnouncement,
  adminListAnnouncements,
  adminListAudienceKinds,
  adminPreviewAudience,
  adminRecallAnnouncement,
  adminSendAnnouncement,
  adminUpdateAnnouncement,
  type AnnouncementInput,
  type AnnouncementSeverity,
  type AnnouncementTopic,
  type AnnouncementView,
  type AudienceKindSpec,
} from "@/lib/api/support"

const DEFAULT_INPUT: AnnouncementInput = {
  title: "",
  body: "",
  topic: "maintenance",
  severity: "info",
  action_url: null,
  audience_kind: "all",
  audience_params: {},
  popup: false,
}

const TOPIC_OPTIONS: { value: AnnouncementTopic; label: string }[] = [
  { value: "maintenance", label: "系统 / 维护" },
  { value: "billing", label: "账单 / 套餐" },
  { value: "account", label: "账户" },
  { value: "artifact", label: "任务 / 交付物" },
  { value: "support", label: "客服" },
]

const SEVERITY_OPTIONS: { value: AnnouncementSeverity; label: string }[] = [
  { value: "info", label: "info（默认）" },
  { value: "success", label: "success（绿）" },
  { value: "warning", label: "warning（黄）" },
  { value: "error", label: "error（红）" },
]

const GROUP_LABELS: Record<string, string> = {
  broad: "广播",
  subscription: "按订阅",
  lifecycle: "按生命周期",
  behavior: "按行为",
}

export default function AdminAnnouncementsPage() {
  const [kinds, setKinds] = useState<AudienceKindSpec[]>([])
  const [input, setInput] = useState<AnnouncementInput>(DEFAULT_INPUT)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [previewCount, setPreviewCount] = useState<number | null>(null)
  const [previewing, setPreviewing] = useState(false)

  const [drafts, setDrafts] = useState<AnnouncementView[]>([])
  const [sent, setSent] = useState<AnnouncementView[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [kindsRes, draftsRes, sentRes] = await Promise.all([
        adminListAudienceKinds(),
        adminListAnnouncements("draft"),
        adminListAnnouncements("sent"),
      ])
      setKinds(kindsRes.kinds)
      setDrafts(draftsRes.items)
      setSent(sentRes.items)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  // Group kinds by their UI group for the picker.
  const grouped = useMemo(() => {
    const out: Record<string, AudienceKindSpec[]> = {
      broad: [],
      subscription: [],
      lifecycle: [],
      behavior: [],
    }
    for (const k of kinds) {
      out[k.group]?.push(k)
    }
    return out
  }, [kinds])

  const currentKindSpec = useMemo(
    () => kinds.find((k) => k.kind === input.audience_kind),
    [kinds, input.audience_kind],
  )

  // Auto-preview audience count whenever the kind / params change.
  useEffect(() => {
    if (kinds.length === 0) return
    let cancelled = false
    setPreviewing(true)
    setPreviewCount(null)
    const handle = setTimeout(async () => {
      try {
        const r = await adminPreviewAudience(input)
        if (!cancelled) setPreviewCount(r.count)
      } catch {
        if (!cancelled) setPreviewCount(null)
      } finally {
        if (!cancelled) setPreviewing(false)
      }
    }, 300)
    return () => {
      cancelled = true
      clearTimeout(handle)
    }
  }, [input.audience_kind, JSON.stringify(input.audience_params), kinds.length])

  const updateInput = <K extends keyof AnnouncementInput>(
    key: K,
    value: AnnouncementInput[K],
  ) => setInput((prev) => ({ ...prev, [key]: value }))

  const updateAudienceParam = (key: string, value: number) =>
    setInput((prev) => ({
      ...prev,
      audience_params: { ...(prev.audience_params ?? {}), [key]: value },
    }))

  const resetForm = () => {
    setInput(DEFAULT_INPUT)
    setEditingId(null)
    setSavedAt(null)
  }

  const loadIntoEditor = (a: AnnouncementView) => {
    setInput({
      title: a.title,
      body: a.body,
      topic: a.topic,
      severity: a.severity,
      action_url: a.action_url,
      audience_kind: a.audience_kind,
      audience_params: a.audience_params ?? {},
      popup: a.popup,
    })
    setEditingId(a.id)
    setSavedAt(null)
    if (typeof window !== "undefined") {
      window.scrollTo({ top: 0, behavior: "smooth" })
    }
  }

  const saveDraft = async () => {
    setBusy(true)
    setError(null)
    try {
      const view = editingId
        ? await adminUpdateAnnouncement(editingId, input)
        : await adminCreateAnnouncement(input)
      setEditingId(view.id)
      setSavedAt(new Date().toLocaleTimeString("zh-CN"))
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败")
    } finally {
      setBusy(false)
    }
  }

  const sendNow = async () => {
    setBusy(true)
    setError(null)
    try {
      // Save first (creates draft if needed).
      const view = editingId
        ? await adminUpdateAnnouncement(editingId, input)
        : await adminCreateAnnouncement(input)
      const result = await adminSendAnnouncement(view.id)
      const msg =
        `已发送：受众 ${result.audience_size} 人，新增通知 ${result.newly_notified} 条` +
        (result.skipped_already_notified > 0
          ? `（跳过 ${result.skipped_already_notified} 条已通知）`
          : "")
      alert(msg)
      resetForm()
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : "发送失败")
    } finally {
      setBusy(false)
    }
  }

  const sendToSelfTest = async () => {
    setBusy(true)
    setError(null)
    try {
      // Force audience to admin_only for test send. Caller can revert
      // after seeing the result.
      const testInput: AnnouncementInput = {
        ...input,
        audience_kind: "admin_only",
        audience_params: {},
      }
      const view = await adminCreateAnnouncement(testInput)
      await adminSendAnnouncement(view.id)
      alert("已发送给自己（admin_only），打开通知中心查看效果")
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : "试发失败")
    } finally {
      setBusy(false)
    }
  }

  const deleteDraft = async (id: string) => {
    if (!confirm("确认删除草稿？")) return
    setBusy(true)
    try {
      await adminDeleteAnnouncement(id)
      if (editingId === id) resetForm()
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败")
    } finally {
      setBusy(false)
    }
  }

  const cloneSent = async (id: string) => {
    setBusy(true)
    try {
      const clone = await adminCloneAnnouncement(id)
      loadIntoEditor(clone)
      await reload()
      alert(`已复制到草稿，可编辑后重新发送`)
    } catch (err) {
      setError(err instanceof Error ? err.message : "复制失败")
    } finally {
      setBusy(false)
    }
  }

  const recallSent = async (id: string, recipientCount: number | null) => {
    if (
      !confirm(
        `确认撤回这条公告？将硬删除已发的 ${recipientCount ?? "?"} 条通知。`,
      )
    ) {
      return
    }
    setBusy(true)
    try {
      const r = await adminRecallAnnouncement(id)
      alert(`已撤回，删除 ${r.deleted_count} 条通知`)
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : "撤回失败")
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return <p className="text-sm text-muted-foreground">加载中…</p>
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8">
      <header>
        <h1 className="ink-display text-2xl text-foreground sm:text-3xl">
          系统公告
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          编写公告 → 选受众 → 预览数量 → 发送。已发送的公告可撤回（硬删除已派发的通知）或复制为新草稿编辑后重发。
        </p>
      </header>

      {error ? (
        <p className="rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      ) : null}

      {/* --- Composer --- */}
      <section className="space-y-4 rounded-xl border border-border bg-card p-4">
        <header className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-foreground">
            {editingId ? "编辑草稿" : "新公告"}
          </h2>
          {editingId ? (
            <button
              type="button"
              onClick={resetForm}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              清空表单 / 写新公告
            </button>
          ) : null}
        </header>

        <Field label="标题（≤ 255 字）">
          <input
            type="text"
            maxLength={255}
            value={input.title}
            onChange={(e) => updateInput("title", e.target.value)}
            className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
          />
        </Field>

        <Field label="正文（≤ 4000 字）">
          <textarea
            rows={6}
            maxLength={4000}
            value={input.body}
            onChange={(e) => updateInput("body", e.target.value)}
            className="w-full resize-y rounded border border-border bg-background px-2 py-1 text-sm"
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="话题">
            <select
              value={input.topic}
              onChange={(e) =>
                updateInput("topic", e.target.value as AnnouncementTopic)
              }
              className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
            >
              {TOPIC_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="严重级别">
            <select
              value={input.severity}
              onChange={(e) =>
                updateInput("severity", e.target.value as AnnouncementSeverity)
              }
              className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
            >
              {SEVERITY_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="操作链接（可选）">
            <input
              type="text"
              maxLength={512}
              placeholder="/pricing"
              value={input.action_url ?? ""}
              onChange={(e) =>
                updateInput("action_url", e.target.value || null)
              }
              className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
            />
          </Field>
        </div>

        {/* Audience picker */}
        <fieldset className="space-y-3 rounded border border-border/50 bg-background/50 p-3">
          <legend className="px-2 text-xs font-medium text-foreground">
            受众
          </legend>

          {(["broad", "subscription", "lifecycle", "behavior"] as const).map(
            (group) => {
              const list = grouped[group]
              if (!list || list.length === 0) return null
              return (
                <div key={group} className="space-y-1">
                  <p className="text-xs uppercase tracking-wide text-muted-foreground">
                    {GROUP_LABELS[group]}
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {list.map((k) => (
                      <button
                        key={k.kind}
                        type="button"
                        onClick={() => {
                          // Reset params to defaults when switching kinds.
                          const defaults: Record<string, unknown> = {}
                          for (const p of k.params) {
                            defaults[p.key] = p.default
                          }
                          setInput((prev) => ({
                            ...prev,
                            audience_kind: k.kind,
                            audience_params: defaults,
                          }))
                        }}
                        className={
                          "rounded-full border px-3 py-1 text-xs " +
                          (input.audience_kind === k.kind
                            ? "border-foreground bg-foreground text-background"
                            : "border-border bg-card text-foreground hover:bg-muted")
                        }
                      >
                        {k.label}
                      </button>
                    ))}
                  </div>
                </div>
              )
            },
          )}

          {currentKindSpec && currentKindSpec.params.length > 0 ? (
            <div className="grid gap-3 border-t border-border/50 pt-3 sm:grid-cols-2">
              {currentKindSpec.params.map((p) => (
                <Field
                  key={p.key}
                  label={
                    p.key === "days"
                      ? "天数 N"
                      : p.key === "min_jobs"
                        ? "最少任务数 M"
                        : p.key
                  }
                  hint={`默认 ${p.default}${p.min !== undefined ? `，范围 ${p.min}-${p.max}` : ""}`}
                >
                  <input
                    type="number"
                    min={p.min}
                    max={p.max}
                    value={String(
                      (input.audience_params?.[p.key] as number | undefined) ??
                        p.default,
                    )}
                    onChange={(e) =>
                      updateAudienceParam(
                        p.key,
                        parseInt(e.target.value, 10) || (p.default as number),
                      )
                    }
                    className="w-full rounded border border-border bg-background px-2 py-1 text-sm"
                  />
                </Field>
              ))}
            </div>
          ) : null}

          <div className="rounded border border-dashed border-border bg-muted/20 px-3 py-2 text-xs">
            预览：
            {input.audience_kind === "for_new_registrations" ? (
              <span className="ml-1 text-foreground">
                <span className="font-semibold">持续生效。</span>
                每位之后注册的新用户都会收到（包括弹窗）。撤回该公告会停止派发并删除已派发的通知。
              </span>
            ) : previewing ? (
              <span className="ml-1 text-muted-foreground">计算中…</span>
            ) : previewCount !== null ? (
              <span className="ml-1 font-semibold text-foreground">
                将发送给 {previewCount} 名用户
              </span>
            ) : (
              <span className="ml-1 text-muted-foreground">—</span>
            )}
          </div>
        </fieldset>

        {/* Popup option — show as modal on next page load */}
        <label className="flex items-start gap-2 rounded border border-amber-300/40 bg-amber-50/30 p-3 text-sm">
          <input
            type="checkbox"
            checked={!!input.popup}
            onChange={(e) => updateInput("popup", e.target.checked)}
            className="mt-0.5 h-4 w-4"
          />
          <span className="flex-1">
            <span className="font-medium text-foreground">弹窗显示</span>
            <span className="ml-2 text-xs text-muted-foreground">
              开启后，受众用户**首次**进入网站时会看到一个弹窗（关闭后不再弹）。
              通知本身仍然进入「通知中心」铃铛，可以重看。
              适合系统维护、重要变更等强提醒场景；普通通知建议留默认（关闭）。
            </span>
          </span>
        </label>

        {/* Action buttons */}
        <div className="flex flex-wrap items-center justify-end gap-2">
          {savedAt ? (
            <span className="text-xs text-muted-foreground">
              已保存：{savedAt}
            </span>
          ) : null}
          <button
            type="button"
            disabled={busy || !input.title.trim() || !input.body.trim()}
            onClick={() => void sendToSelfTest()}
            className="rounded border border-border bg-card px-3 py-1.5 text-xs hover:bg-muted disabled:opacity-50"
          >
            试发给自己
          </button>
          <button
            type="button"
            disabled={busy || !input.title.trim() || !input.body.trim()}
            onClick={() => void saveDraft()}
            className="rounded border border-border bg-card px-3 py-1.5 text-xs hover:bg-muted disabled:opacity-50"
          >
            保存草稿
          </button>
          <button
            type="button"
            disabled={
              busy ||
              !input.title.trim() ||
              !input.body.trim() ||
              // for_new_registrations is a LIVE audience — count=0 at
              // send time is expected, so don't block sending. For all
              // other kinds, count=0 means "no recipients", block.
              (previewCount === 0 &&
                input.audience_kind !== "for_new_registrations")
            }
            onClick={() => {
              const msg =
                input.audience_kind === "for_new_registrations"
                  ? "确认发布这条公告？将持续生效，每位之后注册的新用户都会收到。撤回会停止派发并删除已派发的通知。"
                  : `确认发送给 ${previewCount ?? "?"} 名用户？发送后只能撤回（硬删除），无法编辑。`
              if (confirm(msg)) {
                void sendNow()
              }
            }}
            className="rounded bg-[color:var(--cinnabar,#C73E3A)] px-4 py-1.5 text-xs font-medium text-white disabled:opacity-50"
          >
            {busy
              ? "发送中…"
              : input.audience_kind === "for_new_registrations"
                ? "发布"
                : "发送"}
          </button>
        </div>
      </section>

      {/* --- Drafts --- */}
      <section className="space-y-3 rounded-xl border border-border bg-card p-4">
        <h2 className="text-base font-semibold text-foreground">
          草稿（{drafts.length}）
        </h2>
        {drafts.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无草稿</p>
        ) : (
          <ul className="space-y-2">
            {drafts.map((d) => (
              <li
                key={d.id}
                className="flex items-start justify-between gap-3 rounded-lg border border-border bg-background p-3 text-sm"
              >
                <div className="min-w-0 flex-1">
                  <p className="font-medium text-foreground truncate">{d.title}</p>
                  <p className="mt-1 truncate text-xs text-muted-foreground">
                    {d.body.slice(0, 80)}
                    {d.body.length > 80 ? "…" : ""}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground/80">
                    {d.audience_kind} · {new Date(d.updated_at).toLocaleString("zh-CN")}
                  </p>
                </div>
                <div className="flex shrink-0 flex-col gap-1 text-xs">
                  <button
                    type="button"
                    onClick={() => loadIntoEditor(d)}
                    className="rounded border border-border bg-card px-2 py-1 text-foreground hover:bg-muted"
                  >
                    编辑
                  </button>
                  <button
                    type="button"
                    onClick={() => void deleteDraft(d.id)}
                    className="rounded border border-destructive/30 bg-destructive/5 px-2 py-1 text-destructive hover:bg-destructive/10"
                  >
                    删除
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* --- Sent --- */}
      <section className="space-y-3 rounded-xl border border-border bg-card p-4">
        <h2 className="text-base font-semibold text-foreground">
          已发送（{sent.length}）
        </h2>
        {sent.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无已发送公告</p>
        ) : (
          <ul className="space-y-2">
            {sent.map((s) => (
              <li
                key={s.id}
                className="rounded-lg border border-border bg-background p-3 text-sm"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="flex items-center gap-2 font-medium text-foreground truncate">
                      <span className="truncate">{s.title}</span>
                      {s.popup ? (
                        <span className="rounded-full bg-amber-100 px-2 text-[10px] font-semibold uppercase tracking-wide text-amber-700">
                          弹窗
                        </span>
                      ) : null}
                    </p>
                    <p className="mt-1 truncate text-xs text-muted-foreground">
                      {s.body.slice(0, 80)}
                      {s.body.length > 80 ? "…" : ""}
                    </p>
                    <p className="mt-2 text-xs text-muted-foreground/80">
                      {s.audience_kind} ·{" "}
                      {s.sent_at
                        ? new Date(s.sent_at).toLocaleString("zh-CN")
                        : "—"}
                      {" · "}
                      {s.audience_kind === "for_new_registrations" ? (
                        <>
                          持续生效
                          {s.stats ? (
                            <>
                              {" · "}
                              已派发 {s.stats.total} · 已读 {s.stats.read}
                            </>
                          ) : null}
                        </>
                      ) : (
                        <>
                          发给 {s.recipient_count ?? "?"} 人
                          {s.stats ? (
                            <>
                              {" · "}
                              已读 {s.stats.read} / 归档 {s.stats.archived}
                            </>
                          ) : null}
                        </>
                      )}
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-col gap-1 text-xs">
                    <button
                      type="button"
                      onClick={() => void cloneSent(s.id)}
                      className="rounded border border-border bg-card px-2 py-1 text-foreground hover:bg-muted"
                    >
                      复制 / 重发
                    </button>
                    <button
                      type="button"
                      onClick={() => void recallSent(s.id, s.recipient_count)}
                      className="rounded border border-destructive/30 bg-destructive/5 px-2 py-1 text-destructive hover:bg-destructive/10"
                    >
                      撤回
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-foreground">{label}</span>
      {children}
      {hint ? (
        <span className="mt-1 block text-xs text-muted-foreground">{hint}</span>
      ) : null}
    </label>
  )
}
