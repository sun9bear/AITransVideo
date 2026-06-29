"use client"

import { useCallback, useEffect, useState } from "react"
import { useTranslations } from "next-intl"
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

/** Translator scoped to the `appVoices` namespace (relative keys). */
type VoicesTranslator = ReturnType<typeof useTranslations<"appVoices">>

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function VoiceLibraryPage() {
  const t = useTranslations("appVoices")
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
      showToast(t("toast.calibrateDone"))
    } catch (err) {
      setErrors((e) => ({ ...e, [voiceId]: err instanceof Error ? err.message : t("error.calibrateFailed") }))
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
      setErrors((e) => ({ ...e, [voiceId]: err instanceof Error ? err.message : t("error.probeFailed") }))
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
      showToast(t("toast.copyFailed"))
    }
  }, [t])

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
      showToast(t("toast.labelUpdated"))
    } catch (err) {
      setErrors((e) => ({ ...e, [voiceId]: err instanceof Error ? err.message : t("error.labelUpdateFailed") }))
    }
  }, [editValue, t])

  // --- Delete voice ---
  const handleDelete = useCallback(async (voiceId: string, label: string) => {
    const confirmed = await confirm({
      title: t("delete.confirmTitle"),
      description: t("delete.confirmDescription", { label }),
      destructive: true,
    })
    if (!confirmed) return
    try {
      const resp = await fetch(`/gateway/user-voices/${encodeURIComponent(voiceId)}`, {
        method: 'DELETE',
        credentials: 'include',
      })
      if (!resp.ok) throw new Error(t("error.deleteFailedWithStatus", { status: resp.status }))
      setSummary((s) => s ? {
        ...s,
        voiceCount: s.voiceCount - 1,
        clonedVoiceCount: Math.max(0, s.clonedVoiceCount - 1),
        voices: s.voices.filter((v) => v.voiceId !== voiceId),
      } : s)
      showToast(t("toast.voiceDeleted"))
    } catch (err) {
      setErrors((e) => ({ ...e, [voiceId]: err instanceof Error ? err.message : t("error.deleteFailed") }))
    }
  }, [confirm, t])

  // --- Add voice modal callback ---
  const handleAddSuccess = useCallback(() => {
    setShowAdd(false)
    showToast(t("toast.voiceAdded"))
    // Re-fetch full library to include new voice
    getVoiceLibrary().then(setSummary).catch(() => {})
  }, [t])

  // --- Helpers ---
  const clearError = (voiceId: string) => {
    setErrors((e) => { if (!(voiceId in e)) return e; const n = { ...e }; delete n[voiceId]; return n })
  }
  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 3000)
  }

  if (isLoading) return <EmptyState title={t("loading.title")} description={t("loading.description")} />
  if (!summary || summary.voices.length === 0) {
    return (
      <div className="space-y-6">
        <EmptyState
          title={t("empty.title")}
          description={t("empty.description")}
          actionLabel={t("empty.addAction")}
          actionTo={undefined}
        />
        <div className="flex justify-center">
          <button
            type="button"
            onClick={() => setShowAdd(true)}
            className="rounded-full bg-primary px-5 py-2 text-sm font-semibold text-primary-foreground"
          >
            {t("header.addButton")}
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
          <h1 className="text-2xl font-bold font-heading text-foreground">{t("header.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("header.subtitle")}</p>
        </div>
        <div className="flex gap-3 items-center">
          <span className="rounded-full bg-muted/40 px-3 py-1 text-xs text-muted-foreground">{t("header.voiceCount", { count: summary.voiceCount })}</span>
          <span className="rounded-full bg-primary/15 px-3 py-1 text-xs text-primary/80">{t("header.clonedCount", { count: summary.clonedVoiceCount })}</span>
          <button
            type="button"
            onClick={() => setShowAdd(true)}
            className="rounded-full bg-primary px-4 py-1.5 text-xs font-semibold text-primary-foreground hover:bg-primary/90 transition"
          >
            {t("header.addButton")}
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
          const sourceTitle = getVoiceSourceTitle(voice, t)
          const sourceHref = getVoiceSourceHref(voice)
          const sourceBadges = getVoiceSourceBadges(voice, t)

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
                          title={t("card.editNameTitle")}
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
                    <SpeedBadge cps={voice.charsPerSecond} calibratedAt={voice.speedCalibratedAt} t={t} />
                  </div>

                  {sourceTitle || sourceBadges.length > 0 ? (
                    <div className="space-y-1 pt-1 text-xs text-muted-foreground">
                      {sourceTitle ? (
                        <p className="truncate">
                          <span className="text-muted-foreground/70">{t("card.sourceLabel")}</span>
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
                    {isCopied ? t("card.copied") : t("card.copyId")}
                  </button>

                  {/* Probe (试听) */}
                  <button
                    type="button"
                    onClick={() => handleProbe(voice.voiceId, voice.label || voice.voiceId)}
                    disabled={isProbing}
                    className="rounded-full border border-border bg-muted/40 px-3 py-1 text-xs font-semibold text-muted-foreground transition hover:border-primary/40 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {isProbing ? t("card.synthesizing") : t("card.probe")}
                  </button>

                  {/* Calibrate (测试语速) */}
                  <button
                    type="button"
                    onClick={() => handleCalibrate(voice.voiceId)}
                    disabled={isRunning}
                    className="rounded-full border border-border bg-muted/40 px-3 py-1 text-xs font-semibold text-muted-foreground transition hover:border-primary/40 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {isRunning ? t("card.calibrating") : voice.charsPerSecond != null ? t("card.recalibrate") : t("card.calibrate")}
                  </button>

                  {/* Delete */}
                  <button
                    type="button"
                    onClick={() => handleDelete(voice.voiceId, voice.label || voice.voiceId)}
                    className="rounded-full border border-border bg-muted/40 px-3 py-1 text-xs font-semibold text-destructive/70 transition hover:border-destructive/40 hover:text-destructive"
                  >
                    {t("card.delete")}
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

function SpeedBadge({ cps, calibratedAt, t }: { cps: number | null; calibratedAt: string | null; t: VoicesTranslator }) {
  if (cps == null) return <span className="text-muted-foreground/60">{t("speedBadge.uncalibrated")}</span>
  const tier = cps < 3.5 ? { label: t("speedBadge.tierSlow"), cls: "text-amber-500/80" }
    : cps >= 4.5 ? { label: t("speedBadge.tierFast"), cls: "text-cyan-500/80" }
    : { label: t("speedBadge.tierMedium"), cls: "" }
  const ago = relativeAgo(calibratedAt, t)
  const cpsLabel = cps.toFixed(1)
  return (
    <span className={tier.cls}>
      {ago
        ? t("speedBadge.summaryWithAgo", { cps: cpsLabel, tier: tier.label, ago })
        : t("speedBadge.summary", { cps: cpsLabel, tier: tier.label })}
    </span>
  )
}

function relativeAgo(iso: string | null, t: VoicesTranslator): string | null {
  if (!iso) return null
  const ts = Date.parse(iso)
  if (!Number.isFinite(ts)) return null
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000))
  if (s < 60) return t("ago.justNow")
  if (s < 3600) return t("ago.minutesAgo", { minutes: Math.floor(s / 60) })
  if (s < 86400) return t("ago.hoursAgo", { hours: Math.floor(s / 3600) })
  return t("ago.daysAgo", { days: Math.floor(s / 86400) })
}

function getVoiceSourceTitle(voice: VoiceLibraryEntry, t: VoicesTranslator): string | null {
  const title = voice.sourceVideoTitle?.trim()
  if (title && !isGeneratedSourceTitle(title)) return title
  if (voice.sourceType === "youtube_url") return t("source.youtube")
  if (voice.sourceType === "local_video") return t("source.uploadedVideo")
  return null
}

function getVoiceSourceHref(voice: VoiceLibraryEntry): string | null {
  const ref = voice.sourceRef?.trim()
  if (!ref) return null
  if (voice.sourceType !== "youtube_url") return null
  if (!/^https?:\/\//i.test(ref)) return null
  return ref
}

function getVoiceSourceBadges(voice: VoiceLibraryEntry, t: VoicesTranslator): string[] {
  const badges: string[] = []
  const origin = formatCreatedFrom(voice.createdFrom, t)
  if (origin) badges.push(origin)

  const speakerName = voice.sourceSpeakerName?.trim()
  const label = voice.label?.trim() ?? ""
  if (speakerName && !label.includes(speakerName)) {
    badges.push(t("source.speakerBadge", { name: speakerName }))
  }

  const channel = getSourceChannel(voice.sourceContentTags)
  if (channel) badges.push(t("source.channelBadge", { channel }))
  if (voice.sourceContentEra) badges.push(t("source.eraBadge", { era: voice.sourceContentEra }))

  const sample = formatSampleSeconds(voice.cloneSampleSeconds, t)
  if (sample) badges.push(sample)

  if (badges.length === 0 && voice.sourceContentHash) {
    badges.push(t("source.reusableBadge"))
  }
  return badges.slice(0, 5)
}

function isGeneratedSourceTitle(title: string): boolean {
  return /^(油管视频|上传视频) \d{4}-\d{2}-\d{2} \d{3}(?:_[a-z0-9]{4})?$/.test(title)
}

function formatCreatedFrom(value: string | null, t: VoicesTranslator): string | null {
  if (value === "smart_auto") return t("source.createdFromSmartAuto")
  if (value === "studio_manual") return t("source.createdFromStudioManual")
  if (value === "manual_add") return t("source.createdFromManualAdd")
  return null
}

function formatSampleSeconds(seconds: number | null, t: VoicesTranslator): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return null
  if (seconds >= 60) {
    const minutes = Math.round(seconds / 60)
    return t("source.sampleMinutes", { minutes })
  }
  return t("source.sampleSeconds", { seconds: Math.round(seconds) })
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
  const t = useTranslations("appVoices")
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
      setError(err instanceof Error ? err.message : t("error.probeFailed"))
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
      setError(err instanceof Error ? err.message : t("error.addFailed"))
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
          <h2 className="text-lg font-bold text-foreground">{t("addModal.title")}</h2>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground text-xl">&times;</button>
        </div>

        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">{t("addModal.voiceIdLabel")}</label>
            <input
              value={voiceId}
              onChange={(e) => { setVoiceId(e.target.value); setProbeOk(null) }}
              placeholder={t("addModal.voiceIdPlaceholder")}
              className="w-full rounded-lg border border-border bg-muted/20 px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary/50"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">{t("addModal.voiceNameLabel")}</label>
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder={t("addModal.voiceNamePlaceholder")}
              className="w-full rounded-lg border border-border bg-muted/20 px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary/50"
            />
          </div>
        </div>

        {/* Probe result */}
        {probeOk === true && (
          <div className="flex items-center gap-2 text-xs text-green-500">
            <span>{t("addModal.probeOk")}</span>
          </div>
        )}
        {probeOk === false && (
          <div className="text-xs text-destructive">{error || t("addModal.probeUnavailable")}</div>
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
            {t("addModal.cancel")}
          </button>
          <button
            type="button"
            onClick={handleProbe}
            disabled={!voiceId.trim() || probeRunning}
            className="rounded-lg border border-primary/40 bg-primary/10 px-4 py-2 text-sm font-semibold text-primary hover:bg-primary/20 transition disabled:opacity-50"
          >
            {probeRunning ? t("addModal.probeRunning") : t("addModal.probeTest")}
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={!voiceId.trim() || !label.trim() || saving}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground hover:bg-primary/90 transition disabled:opacity-50"
          >
            {saving ? t("addModal.saving") : t("addModal.save")}
          </button>
        </div>
      </div>
    </div>
  )
}
