"use client"

/**
 * "音色修改" Tab — speaker-level voice picker for the editing session
 * (plan §7.3 MVP scope).
 *
 * Design decisions:
 *
 * - **Speaker-level, not per-segment.** User mental model is "this speaker
 *   sounds off, replace them", so the picker groups segments by
 *   `speaker_id`. Changing a speaker writes voice_map entries for EVERY
 *   segment of that speaker (`set` N times). Clearing reverts them all.
 *
 * - **Candidates from auto_matched_by_provider** on the
 *   `voice_selection_review` stage payload (loaded from
 *   `/jobs/{id}/review-state`). That payload carries the voices the
 *   pipeline's voice selector scored for each speaker — primary match
 *   plus a small `backup_voices` list. Showing only those keeps the
 *   dropdown manageable (~3-8 voices per speaker, same provider as the
 *   original pick). Browsing the full 900-voice catalog is deferred
 *   until we know people need it.
 *
 * - **Any-segment-override-present = speaker has override.** Both the
 *   "已自定义" badge and the "恢复原音色" action key off "voice_map has
 *   ANY entry for ANY segment of this speaker", rather than requiring
 *   a 1-to-1 match across all segments. Speakers with mixed overrides
 *   are rare (segments are ganged together in the UI) and collapsing
 *   is simpler than tri-state.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import { toast } from "sonner"
import { Loader2, RotateCcw, Volume2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import { apiClient } from "@/lib/api/client"
import {
  clearVoiceOverride,
  setVoiceOverride,
  type EditingSegment,
  type VoiceMapEntry,
} from "@/lib/api/editing"
import { getErrorMessage } from "@/lib/api/errors"

// Minimal subset of the review-state payload this Tab needs. We ignore
// the rest because voice_selection_review is ~20 fields and we only
// care about candidates per speaker.
interface ReviewStateSpeaker {
  speakerId: string
  displayName: string
  /** Auto-matched voice keyed by provider. Empty dict in single-provider mode. */
  autoMatchedByProvider: Record<string, AutoMatch | null>
  /** Single-provider fallback (older payloads). */
  autoMatchedVoice: { voiceId: string; label: string; provider: string } | null
}

interface AutoMatch {
  voiceId: string
  label: string
  provider: string
  backups: Array<{ voiceId: string; label: string; provider: string }>
}

interface VoiceModifyTabProps {
  jobId: string
  segments: EditingSegment[]
  voiceMap: Record<string, VoiceMapEntry>
  onVoiceMapChange: (next: Record<string, VoiceMapEntry>) => void
}

export function VoiceModifyTab({
  jobId,
  segments,
  voiceMap,
  onVoiceMapChange,
}: VoiceModifyTabProps) {
  const [speakers, setSpeakers] = useState<ReviewStateSpeaker[]>([])
  const [loadState, setLoadState] = useState<"idle" | "loading" | "ready" | "error">("idle")
  const [loadError, setLoadError] = useState<string | null>(null)
  const [pendingSpeakerIds, setPendingSpeakerIds] = useState<Set<string>>(new Set())

  // Group editing segments by speaker_id. Speakers missing from the
  // review-state payload still get a row (with just "无可选音色").
  const segmentsBySpeaker = useMemo(() => {
    const map = new Map<string, EditingSegment[]>()
    for (const seg of segments) {
      const sid = seg.speaker_id || "(未标注)"
      const list = map.get(sid) ?? []
      list.push(seg)
      map.set(sid, list)
    }
    return map
  }, [segments])

  // Load review-state once on mount. We cache it in component state so
  // rapid speaker changes don't re-fetch.
  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoadState("loading")
      setLoadError(null)
      try {
        const state = await apiClient.get<{
          results?: {
            review_flow?: {
              stages?: Record<string, { payload?: Record<string, unknown> }>
            }
          }
        }>(`/jobs/${jobId}/review-state`)
        if (cancelled) return
        const stages = state.results?.review_flow?.stages ?? {}
        const payload = stages.voice_selection_review?.payload ?? {}
        const raw = Array.isArray(payload.speakers) ? payload.speakers : []
        const parsed: ReviewStateSpeaker[] = raw.map((s: Record<string, unknown>) => {
          const byProv = (s.auto_matched_by_provider ?? {}) as Record<string, Record<string, unknown> | null>
          const amByProv: Record<string, AutoMatch | null> = {}
          for (const [prov, match] of Object.entries(byProv)) {
            if (match && typeof match === "object") {
              const rawBackups = Array.isArray(match.backup_voices) ? match.backup_voices : []
              const backups = rawBackups
                .map((b: unknown) => {
                  if (b && typeof b === "object") {
                    const obj = b as Record<string, unknown>
                    return {
                      voiceId: String(obj.voice_id ?? ""),
                      label: String(obj.label ?? obj.voice_id ?? ""),
                      provider: String(obj.provider ?? prov),
                    }
                  }
                  return { voiceId: "", label: "", provider: "" }
                })
                .filter((b) => b.voiceId)
              amByProv[prov] = {
                voiceId: String(match.voice_id ?? ""),
                label: String(match.label ?? match.voice_id ?? ""),
                provider: prov,
                backups,
              }
            } else {
              amByProv[prov] = null
            }
          }
          const legacyAuto =
            s.auto_matched_voice && typeof s.auto_matched_voice === "object"
              ? (s.auto_matched_voice as Record<string, unknown>)
              : null
          return {
            speakerId: String(s.speaker_id ?? ""),
            displayName: String(s.display_name ?? s.speaker_id ?? ""),
            autoMatchedByProvider: amByProv,
            autoMatchedVoice: legacyAuto
              ? {
                  voiceId: String(legacyAuto.voice_id ?? ""),
                  label: String(legacyAuto.label ?? legacyAuto.voice_id ?? ""),
                  provider: String(legacyAuto.provider ?? ""),
                }
              : null,
          }
        }).filter((s: ReviewStateSpeaker) => s.speakerId)
        setSpeakers(parsed)
        setLoadState("ready")
      } catch (err) {
        if (cancelled) return
        setLoadError(getErrorMessage(err))
        setLoadState("error")
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [jobId])

  const setSpeakerPending = useCallback((speakerId: string, pending: boolean) => {
    setPendingSpeakerIds((prev) => {
      const next = new Set(prev)
      if (pending) next.add(speakerId)
      else next.delete(speakerId)
      return next
    })
  }, [])

  // Apply a single voice choice to every segment of a speaker. On any
  // per-segment failure we surface the error but continue — other
  // segments may still succeed, matching the batch re-TTS D38 contract.
  const applyVoiceToSpeaker = useCallback(
    async (
      speakerId: string,
      provider: string,
      voiceId: string,
    ): Promise<void> => {
      const ownSegments = segmentsBySpeaker.get(speakerId) ?? []
      if (ownSegments.length === 0) return
      setSpeakerPending(speakerId, true)
      const next: Record<string, VoiceMapEntry> = { ...voiceMap }
      const failures: string[] = []
      for (const seg of ownSegments) {
        try {
          await setVoiceOverride(jobId, seg.segment_id, provider, voiceId)
          next[seg.segment_id] = { provider, voice_id: voiceId }
        } catch (err) {
          failures.push(seg.segment_id)
          console.warn("setVoiceOverride failed", seg.segment_id, err)
        }
      }
      onVoiceMapChange(next)
      setSpeakerPending(speakerId, false)
      if (failures.length > 0) {
        toast.error(`${failures.length} 段音色保存失败，其余成功`)
      } else {
        toast.success(`说话人 ${speakerId} 的 ${ownSegments.length} 段音色已更新`)
      }
    },
    [jobId, segmentsBySpeaker, voiceMap, onVoiceMapChange, setSpeakerPending],
  )

  const clearSpeakerOverride = useCallback(
    async (speakerId: string): Promise<void> => {
      const ownSegments = segmentsBySpeaker.get(speakerId) ?? []
      const toClear = ownSegments.filter((s) => voiceMap[s.segment_id])
      if (toClear.length === 0) return
      setSpeakerPending(speakerId, true)
      const next: Record<string, VoiceMapEntry> = { ...voiceMap }
      const failures: string[] = []
      for (const seg of toClear) {
        try {
          await clearVoiceOverride(jobId, seg.segment_id)
          delete next[seg.segment_id]
        } catch (err) {
          failures.push(seg.segment_id)
          console.warn("clearVoiceOverride failed", seg.segment_id, err)
        }
      }
      onVoiceMapChange(next)
      setSpeakerPending(speakerId, false)
      if (failures.length > 0) {
        toast.error(`${failures.length} 段恢复失败，其余成功`)
      } else {
        toast.success(`说话人 ${speakerId} 已恢复原音色`)
      }
    },
    [jobId, segmentsBySpeaker, voiceMap, onVoiceMapChange, setSpeakerPending],
  )

  const speakerIds = useMemo(() => Array.from(segmentsBySpeaker.keys()), [segmentsBySpeaker])
  const speakerMetaByKey = useMemo(() => {
    const out = new Map<string, ReviewStateSpeaker>()
    for (const s of speakers) out.set(s.speakerId, s)
    return out
  }, [speakers])

  if (loadState === "loading") {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />加载音色候选...
      </div>
    )
  }

  if (loadState === "error") {
    return (
      <div className="rounded-md border border-border bg-background p-4 text-sm">
        <p className="text-destructive mb-2">音色候选加载失败：{loadError}</p>
        <p className="text-muted-foreground text-xs">
          仍可通过&ldquo;翻译修改&rdquo;Tab 逐段修改；更换音色功能需要此数据。
        </p>
      </div>
    )
  }

  if (speakerIds.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">该任务没有段落。</p>
    )
  }

  return (
    <section className="space-y-3">
      {speakerIds.map((speakerId) => {
        const ownSegments = segmentsBySpeaker.get(speakerId) ?? []
        const meta = speakerMetaByKey.get(speakerId) ?? null
        const overriddenCount = ownSegments.filter((s) => voiceMap[s.segment_id]).length
        const hasOverride = overriddenCount > 0
        const pending = pendingSpeakerIds.has(speakerId)

        // First override entry represents what the speaker is currently on;
        // baseline (when no override) comes from the segment dict's voice_id.
        const overrideEntry = hasOverride
          ? voiceMap[ownSegments.find((s) => voiceMap[s.segment_id])!.segment_id]
          : null
        const baselineVoice = ownSegments[0]?.voice_id ?? ""

        // Candidate picker: auto_matched + backups across all providers.
        const candidates: Array<{ provider: string; voiceId: string; label: string }> = []
        const seen = new Set<string>()
        if (meta) {
          const providers = Object.keys(meta.autoMatchedByProvider)
          for (const prov of providers) {
            const m = meta.autoMatchedByProvider[prov]
            if (!m) continue
            const pushCandidate = (p: string, vid: string, lbl: string) => {
              const key = `${p}/${vid}`
              if (!vid || seen.has(key)) return
              seen.add(key)
              candidates.push({ provider: p, voiceId: vid, label: lbl })
            }
            pushCandidate(m.provider, m.voiceId, m.label)
            for (const b of m.backups) pushCandidate(b.provider, b.voiceId, b.label)
          }
          if (candidates.length === 0 && meta.autoMatchedVoice) {
            const { provider, voiceId, label } = meta.autoMatchedVoice
            if (voiceId) candidates.push({ provider, voiceId, label })
          }
        }

        return (
          <article key={speakerId} className="surface-card p-4 space-y-3">
            <header className="flex flex-wrap items-center gap-3">
              <span className="font-medium">
                <Volume2 className="inline h-4 w-4 mr-1" />
                {meta?.displayName || speakerId}
              </span>
              <span className="text-xs text-muted-foreground">
                {ownSegments.length} 段
              </span>
              {hasOverride && (
                <span className="text-xs px-2 py-0.5 rounded-full bg-violet-500/15 text-violet-400">
                  已自定义 {overriddenCount}/{ownSegments.length} 段
                </span>
              )}
            </header>

            <div className="text-xs text-muted-foreground">
              {hasOverride && overrideEntry ? (
                <>
                  当前：<span className="font-mono">{overrideEntry.provider} / {overrideEntry.voice_id}</span>
                </>
              ) : (
                <>
                  当前（原始）：<span className="font-mono">{baselineVoice || "未指定"}</span>
                </>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <label className="text-xs text-muted-foreground">
                候选音色
                {candidates.length === 0 && (
                  <span className="ml-1 text-destructive">（无可选，此说话人未经过音色选择阶段）</span>
                )}
              </label>
              <select
                className="rounded-md border border-border bg-background px-2 py-1 text-sm disabled:opacity-50"
                disabled={candidates.length === 0 || pending}
                value=""
                onChange={(e) => {
                  const v = e.currentTarget.value
                  if (!v) return
                  const [provider, voiceId] = v.split("||")
                  if (!provider || !voiceId) return
                  e.currentTarget.value = ""  // reset after selection
                  void applyVoiceToSpeaker(speakerId, provider, voiceId)
                }}
              >
                <option value="">选择音色...</option>
                {candidates.map((c) => (
                  <option key={`${c.provider}/${c.voiceId}`} value={`${c.provider}||${c.voiceId}`}>
                    [{c.provider}] {c.label}
                  </option>
                ))}
              </select>
              {hasOverride && (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={pending}
                  onClick={() => void clearSpeakerOverride(speakerId)}
                >
                  {pending ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" />
                  ) : (
                    <RotateCcw className="h-3.5 w-3.5 mr-1" />
                  )}
                  恢复原音色
                </Button>
              )}
              {pending && !hasOverride && (
                <span className="text-xs text-muted-foreground inline-flex items-center gap-1">
                  <Loader2 className="h-3 w-3 animate-spin" />保存中...
                </span>
              )}
            </div>
          </article>
        )
      })}
      <p className="text-xs text-muted-foreground">
        修改音色后，需要在&ldquo;翻译修改&rdquo;Tab 点击&ldquo;一键重新合成&rdquo;或逐段重合成，新音色才会在合成时生效。
      </p>
    </section>
  )
}
