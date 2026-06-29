"use client"

import { useCallback, useEffect, useState } from "react"
import { EmptyState } from "@/components/empty-state"
import { useConfirmDialog } from "@/components/ui/confirm-dialog"
import {
  addUserVoice,
  calibrateVoiceSpeed,
  getVoiceLibrary,
  probeVoice,
  updateVoiceLabel,
  type VoiceLibraryEntry,
  type VoiceLibrarySummary,
} from "@/lib/api/voiceLibrary"
import { useIntlLocale } from "@/lib/intl-locale"

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function VoiceLibraryPage() {
  const [summary, setSummary] = useState<VoiceLibrarySummary | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [running, setRunning] = useState<Record<string, true>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [editingLabel, setEditingLabel] = useState<string | null>(null)
  const [editValue, setEditValue] = useState("")
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [probing, setProbing] = useState<Record<string, true>>({})
  const [audioUrls, setAudioUrls] = useState<Record<string, string>>({})
  const [showAdd, setShowAdd] = useState(false)
  const [toast, setToast] = useState<string | null>(null)
  const { confirm, confirmDialog } = useConfirmDialog()
  const formatLocale = useIntlLocale()

  useEffect(() => {
    getVoiceLibrary()
      .then(setSummary)
      .catch(() => {})
      .finally(() => setIsLoading(false))
  }, [])

  // --- Calibrate (测试语速) ---
  const handleCalibrate = useCallback(async (voiceId: string) => {
    setRunning((s) => ({ ...s, [voiceId]: true }))
    clearError(voiceId)
    try {
      const result = await calibrateVoiceSpeed(voiceId)
      setSummary((s) => patchVoice(s, voiceId, {
        charsPerSecond: result.calibration?.cps ?? null,
        speedCalibratedAt: String(result.voice?.speed_calibrated_at ?? '') || new Date().toISOString(),
      }))
      showToast("语速标定完成")
    } catch (err) {
      setErrors((e) => ({ ...e, [voiceId]: err instanceof Error ? err.message : "标定失败" }))
    } finally {
      setRunning((s) => { const n = { ...s }; delete n[voiceId]; return n })
    }
  }, [])

  // --- Probe (试听) ---
  const handleProbe = useCallback(async (voiceId: string, label: string) => {
    setProbing((s) => ({ ...s, [voiceId]: true }))
    clearError(voiceId)
    try {
      const result = await probeVoice(voiceId, label)
      if (result.audio_base64) {
        const url = `data:audio/wav;base64,${result.audio_base64}`
        setAudioUrls((u) => ({ ...u, [voiceId]: url }))
        // Auto-play
        const audio = new Audio(url)
        audio.play().catch(() => {})
      }
    } catch (err) {
      setErrors((e) => ({ ...e, [voiceId]: err instanceof Error ? err.message : "试听失败" }))
    } finally {
      setProbing((s) => { const n = { ...s }; delete n[voiceId]; return n })
    }
  }, [])

  // --- Copy voice_id ---
  const handleCopy = useCallback(async (voiceId: string) => {
    try {
      await navigator.clipboard.writeText(voiceId)
      setCopiedId(voiceId)
      setTimeout(() => setCopiedId(null), 2000)
    } catch {
      showToast("复制失败")
    }
  }, [])

  // --- Edit label ---
  const startEdit = useCallback((voiceId: string, currentLabel: string) => {
    setEditingLabel(voiceId)
    setEditValue(currentLabel)
  }, [])

  const saveLabel = useCallback(async (voiceId: string) => {
    const trimmed = editValue.trim()
    if (!trimmed) return
    setEditingLabel(null)
    try {
      await updateVoiceLabel(voiceId, trimmed)
      setSummary((s) => patchVoice(s, voiceId, { label: trimmed }))
      showToast("名称已更新")
    } catch (err) {
      setErrors((e) => ({ ...e, [voiceId]: err instanceof Error ? err.message : "更新失败" }))
    }
  }, [editValue])

  // --- Delete voice ---
  const handleDelete = useCallback(async (voiceId: string, label: string) => {
    const confirmed = await confirm({
      title: "删除音色",
      description: `确定删除音色「${label}」吗？删除后不可恢复。`,
      destructive: true,
    })
    if (!confirmed) return
    try {
      const resp = await fetch(`/gateway/user-voices/${encodeURIComponent(voiceId)}`, {
        method: 'DELETE',
        credentials: 'include',
      })
      if (!resp.ok) throw new Error(`删除失败 (${resp.status})`)
      setSummary((s) => s ? {
        ...s,
        voiceCount: s.voiceCount - 1,
        clonedVoiceCount: Math.max(0, s.clonedVoiceCount - 1),
        voices: s.voices.filter((v) => v.voiceId !== voiceId),
      } : s)
      showToast("音色已删除")
    } catch (err) {
      setErrors((e) => ({ ...e, [voiceId]: err instanceof Error ? err.message : "删除失败" }))
    }
  }, [confirm])

  // --- Add voice modal callback ---
  const handleAddSuccess = useCallback(() => {
    setShowAdd(false)
    showToast("音色已添加")
    // Re-fetch full library to include new voice
    getVoiceLibrary().then(setSummary).catch(() => {})
  }, [])

  // --- Helpers ---
  const clearError = (voiceId: string) => {
    setErrors((e) => { if (!(voiceId in e)) return e; const n = { ...e }; delete n[voiceId]; return n })
  }
  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 3000)
  }

  if (isLoading) return <EmptyState title="加载中" description="正在加载音色库…" />
  if (!summary || summary.voices.length === 0) {
    return (
      <div className="space-y-6">
        <EmptyState
          title="音色库为空"
          description="在翻译任务中克隆音色后会自动保存到这里，或手动添加。"
          actionLabel="添加音色"
          actionTo={undefined}
        />
        <div className="flex justify-center">
          <button
            type="button"
            onClick={() => setShowAdd(true)}
            className="rounded-full bg-primary px-5 py-2 text-sm font-semibold text-primary-foreground"
          >
            + 添加音色
          </button>
        </div>
        {showAdd && <AddVoiceModal onClose={() => setShowAdd(false)} onSuccess={handleAddSuccess} />}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold font-heading text-foreground">我的音色</h1>
          <p className="text-sm text-muted-foreground mt-1">管理已克隆和内置的音色。</p>
        </div>
        <div className="flex gap-3 items-center">
          <span className="rounded-full bg-muted/40 px-3 py-1 text-xs text-muted-foreground">{summary.voiceCount} 个音色</span>
          <span className="rounded-full bg-primary/15 px-3 py-1 text-xs text-primary/80">{summary.clonedVoiceCount} 个克隆</span>
          <button
            type="button"
            onClick={() => setShowAdd(true)}
            className="rounded-full bg-primary px-4 py-1.5 text-xs font-semibold text-primary-foreground hover:bg-primary/90 transition"
          >
            + 添加音色
          </button>
        </div>
      </div>

      {/* Voice list */}
      <div className="space-y-3">
        {summary.voices.map((voice) => {
          const isRunning = !!running[voice.voiceId]
          const isProbing = !!probing[voice.voiceId]
          const errorMsg = errors[voice.voiceId]
          const audioUrl = audioUrls[voice.voiceId]
          const isCopied = copiedId === voice.voiceId
          const isEditing = editingLabel === voice.voiceId
          const sourceTitle = getVoiceSourceTitle(voice)
          const sourceHref = getVoiceSourceHref(voice)
          const sourceBadges = getVoiceSourceBadges(voice)

          return (
            <article
              key={voice.voiceId}
              className="rounded-2xl border border-border bg-card p-5 transition hover:border-border/80"
            >
              <div className="flex items-start justify-between gap-4">
                {/* Left: name + meta */}
                <div className="space-y-1.5 min-w-0 flex-1">
                  {/* Editable name */}
                  <div className="flex items-center gap-2">
                    {isEditing ? (
                      <input
                        autoFocus
                        value={editValue}
                        onChange={(e) => setEditValue(e.target.value)}
                        onBlur={() => saveLabel(voice.voiceId)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") saveLabel(voice.voiceId)
                          if (e.key === "Escape") setEditingLabel(null)
                        }}
                        className="bg-muted/30 border border-border rounded px-2 py-0.5 text-foreground font-semibold text-sm focus:outline-none focus:ring-1 focus:ring-primary/50 max-w-[300px]"
                      />
                    ) : (
                      <>
                        <h3 className="font-semibold text-foreground truncate">
                          {voice.label || voice.voiceId}
                        </h3>
                        <button
                          type="button"
                          onClick={() => startEdit(voice.voiceId, voice.label || voice.voiceId)}
                          className="text-muted-foreground/40 hover:text-foreground transition shrink-0"
                          title="修改名称"
                        >
                          ✏️
                        </button>
                      </>
                    )}
                  </div>

                  {/* Meta: date + cps (no speaker, no raw voice_id) */}
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    {voice.createdAt ? (
                      <span>{new Date(voice.createdAt).toLocaleDateString(formatLocale)}</span>
                    ) : null}
                    <SpeedBadge cps={voice.charsPerSecond} calibratedAt={voice.speedCalibratedAt} />
                  </div>

                  {sourceTitle || sourceBadges.length > 0 ? (
                    <div className="space-y-1 pt-1 text-xs text-muted-foreground">
                      {sourceTitle ? (
                        <p className="truncate">
                          <span className="text-muted-foreground/70">来源：</span>
                          {sourceHref ? (
                            <a
                              href={sourceHref}
                              target="_blank"
                              rel="noreferrer"
                              className="hover:text-foreground hover:underline"
                              title={sourceTitle}
                            >
                              {sourceTitle}
                            </a>
                          ) : (
                            <span title={sourceTitle}>{sourceTitle}</span>
                          )}
                        </p>
                      ) : null}
                      {sourceBadges.length > 0 ? (
                        <div className="flex flex-wrap gap-1.5">
                          {sourceBadges.map((badge) => (
                            <span
                              key={badge}
                              className="rounded-full bg-muted/40 px-2 py-0.5 text-[11px] text-muted-foreground"
                            >
                              {badge}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </div>

                {/* Right: action buttons */}
                <div className="flex shrink-0 items-center gap-1.5 flex-wrap justify-end">
                  {/* Copy ID */}
                  <button
                    type="button"
                    onClick={() => handleCopy(voice.voiceId)}
                    className="rounded-full border border-border bg-muted/40 px-3 py-1 text-xs font-semibold text-muted-foreground transition hover:border-primary/40 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
                    title={voice.voiceId}
                  >
                    {isCopied ? "✓ 已复制" : "复制 ID"}
                  </button>

                  {/* Probe (试听) */}
                  <button
                    type="button"
                    onClick={() => handleProbe(voice.voiceId, voice.label || voice.voiceId)}
                    disabled={isProbing}
                    className="rounded-full border border-border bg-muted/40 px-3 py-1 text-xs font-semibold text-muted-foreground transition hover:border-primary/40 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {isProbing ? "合成中…" : "试听"}
                  </button>

                  {/* Calibrate (测试语速) */}
                  <button
                    type="button"
                    onClick={() => handleCalibrate(voice.voiceId)}
                    disabled={isRunning}
                    className="rounded-full border border-border bg-muted/40 px-3 py-1 text-xs font-semibold text-muted-foreground transition hover:border-primary/40 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {isRunning ? "标定中…" : voice.charsPerSecond != null ? "重新测试" : "测试语速"}
                  </button>

                  {/* Delete */}
                  <button
                    type="button"
                    onClick={() => handleDelete(voice.voiceId, voice.label || voice.voiceId)}
                    className="rounded-full border border-border bg-muted/40 px-3 py-1 text-xs font-semibold text-destructive/70 transition hover:border-destructive/40 hover:text-destructive"
                  >
                    删除
                  </button>
                </div>
              </div>

              {/* Audio player (after probe) */}
              {audioUrl ? (
                <div className="mt-3">
                  <audio controls src={audioUrl} className="w-full h-8" />
                </div>
              ) : null}

              {/* Error */}
              {errorMsg ? (
                <p className="mt-2 text-xs text-destructive">{errorMsg}</p>
              ) : null}
            </article>
          )
        })}
      </div>

      {/* Add modal */}
      {showAdd && <AddVoiceModal onClose={() => setShowAdd(false)} onSuccess={handleAddSuccess} />}

      {/* Toast */}
      {toast ? (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 rounded-full bg-foreground/90 text-background px-6 py-2 text-sm font-medium shadow-lg z-50 animate-in fade-in slide-in-from-bottom-4">
          {toast}
        </div>
      ) : null}

      {confirmDialog}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function patchVoice(
  summary: VoiceLibrarySummary | null,
  voiceId: string,
  patch: Partial<VoiceLibraryEntry>,
): VoiceLibrarySummary | null {
  if (!summary) return summary
  return {
    ...summary,
    voices: summary.voices.map((v): VoiceLibraryEntry =>
      v.voiceId === voiceId ? { ...v, ...patch } : v,
    ),
  }
}

function SpeedBadge({ cps, calibratedAt }: { cps: number | null; calibratedAt: string | null }) {
  if (cps == null) return <span className="text-muted-foreground/60">· 未标定</span>
  const tier = cps < 3.5 ? { label: "慢", cls: "text-amber-500/80" }
    : cps >= 4.5 ? { label: "快", cls: "text-cyan-500/80" }
    : { label: "中速", cls: "" }
  const ago = relativeAgo(calibratedAt)
  return (
    <span className={tier.cls}>
      · {cps.toFixed(1)} 字/秒（{tier.label}）{ago ? ` · ${ago}` : ""}
    </span>
  )
}

function relativeAgo(iso: string | null): string | null {
  if (!iso) return null
  const ts = Date.parse(iso)
  if (!Number.isFinite(ts)) return null
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000))
  if (s < 60) return "刚刚"
  if (s < 3600) return `${Math.floor(s / 60)}分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)}小时前`
  return `${Math.floor(s / 86400)}天前`
}

function getVoiceSourceTitle(voice: VoiceLibraryEntry): string | null {
  const title = voice.sourceVideoTitle?.trim()
  if (title && !isGeneratedSourceTitle(title)) return title
  if (voice.sourceType === "youtube_url") return "YouTube 视频"
  if (voice.sourceType === "local_video") return "上传视频"
  return null
}

function getVoiceSourceHref(voice: VoiceLibraryEntry): string | null {
  const ref = voice.sourceRef?.trim()
  if (!ref) return null
  if (voice.sourceType !== "youtube_url") return null
  if (!/^https?:\/\//i.test(ref)) return null
  return ref
}

function getVoiceSourceBadges(voice: VoiceLibraryEntry): string[] {
  const badges: string[] = []
  const origin = formatCreatedFrom(voice.createdFrom)
  if (origin) badges.push(origin)

  const speakerName = voice.sourceSpeakerName?.trim()
  const label = voice.label?.trim() ?? ""
  if (speakerName && !label.includes(speakerName)) {
    badges.push(`说话人：${speakerName}`)
  }

  const channel = getSourceChannel(voice.sourceContentTags)
  if (channel) badges.push(`频道：${channel}`)
  if (voice.sourceContentEra) badges.push(`${voice.sourceContentEra}年`)

  const sample = formatSampleSeconds(voice.cloneSampleSeconds)
  if (sample) badges.push(sample)

  if (badges.length === 0 && voice.sourceContentHash) {
    badges.push("可按来源复用")
  }
  return badges.slice(0, 5)
}

function isGeneratedSourceTitle(title: string): boolean {
  return /^(油管视频|上传视频) \d{4}-\d{2}-\d{2} \d{3}(?:_[a-z0-9]{4})?$/.test(title)
}

function formatCreatedFrom(value: string | null): string | null {
  if (value === "smart_auto") return "智能版自动克隆"
  if (value === "studio_manual") return "工作台克隆"
  if (value === "manual_add") return "手动添加"
  return null
}

function formatSampleSeconds(seconds: number | null): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return null
  if (seconds >= 60) {
    const minutes = Math.round(seconds / 60)
    return `样本 ${minutes} 分钟`
  }
  return `样本 ${Math.round(seconds)} 秒`
}

function getSourceChannel(tags: unknown): string | null {
  if (!tags || typeof tags !== "object" || Array.isArray(tags)) return null
  const maybeChannel = (tags as { channel?: unknown }).channel
  return typeof maybeChannel === "string" && maybeChannel.trim()
    ? maybeChannel.trim()
    : null
}

// ---------------------------------------------------------------------------
// AddVoiceModal
// ---------------------------------------------------------------------------

function AddVoiceModal({ onClose, onSuccess }: { onClose: () => void; onSuccess: () => void }) {
  const [voiceId, setVoiceId] = useState("")
  const [label, setLabel] = useState("")
  const [saving, setSaving] = useState(false)
  const [probeRunning, setProbeRunning] = useState(false)
  const [probeOk, setProbeOk] = useState<boolean | null>(null)
  const [probeAudioUrl, setProbeAudioUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const handleProbe = async () => {
    if (!voiceId.trim()) return
    setProbeRunning(true)
    setProbeOk(null)
    setProbeAudioUrl(null)
    setError(null)
    try {
      const result = await probeVoice(voiceId.trim(), label.trim() || voiceId.trim())
      if (result.audio_base64) {
        const url = `data:audio/wav;base64,${result.audio_base64}`
        setProbeAudioUrl(url)
        setProbeOk(true)
        const audio = new Audio(url)
        audio.play().catch(() => {})
      }
    } catch (err) {
      setProbeOk(false)
      setError(err instanceof Error ? err.message : "试听失败")
    } finally {
      setProbeRunning(false)
    }
  }

  const handleSave = async () => {
    if (!voiceId.trim() || !label.trim()) return
    setSaving(true)
    setError(null)
    try {
      await addUserVoice({
        voice_id: voiceId.trim(),
        label: label.trim(),
        tts_provider: "minimax_tts",
        platform: "minimax_domestic",
      })
      onSuccess()
    } catch (err) {
      setError(err instanceof Error ? err.message : "添加失败")
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div
        className="bg-card border border-border rounded-2xl p-6 w-full max-w-md space-y-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-bold text-foreground">添加音色</h2>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground text-xl">&times;</button>
        </div>

        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">Voice ID</label>
            <input
              value={voiceId}
              onChange={(e) => { setVoiceId(e.target.value); setProbeOk(null) }}
              placeholder="例如 vt_speaker_a_1776252490214"
              className="w-full rounded-lg border border-border bg-muted/20 px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary/50"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">音色名称</label>
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="例如 查理·芒格"
              className="w-full rounded-lg border border-border bg-muted/20 px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary/50"
            />
          </div>
        </div>

        {/* Probe result */}
        {probeOk === true && (
          <div className="flex items-center gap-2 text-xs text-green-500">
            <span>✓ 音色可用</span>
          </div>
        )}
        {probeOk === false && (
          <div className="text-xs text-destructive">{error || "音色不可用"}</div>
        )}
        {probeAudioUrl && (
          <audio controls src={probeAudioUrl} className="w-full h-8" />
        )}
        {error && probeOk !== false && (
          <div className="text-xs text-destructive">{error}</div>
        )}

        {/* Actions */}
        <div className="flex gap-3 justify-end pt-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground hover:text-foreground transition"
          >
            取消
          </button>
          <button
            type="button"
            onClick={handleProbe}
            disabled={!voiceId.trim() || probeRunning}
            className="rounded-lg border border-primary/40 bg-primary/10 px-4 py-2 text-sm font-semibold text-primary hover:bg-primary/20 transition disabled:opacity-50"
          >
            {probeRunning ? "合成中…" : "试听测试"}
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={!voiceId.trim() || !label.trim() || saving}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:bg-primary/90 transition disabled:opacity-50"
          >
            {saving ? "保存中…" : "添加"}
          </button>
        </div>
      </div>
    </div>
  )
}
