"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useTranslations } from "next-intl"
import { Link } from "@/i18n/navigation"
import { useParams } from "next/navigation"
import { useRouter } from "@/i18n/navigation"
import { toast } from "sonner"
import {
  ArrowLeft,
  Loader2,
  PlaySquare,
  RefreshCw,
  Sparkles,
  X,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { useConfirmDialog } from "@/components/ui/confirm-dialog"
import { sleep, useIsMountedRef } from "@/lib/react/useIsMountedRef"
import {
  acceptSegmentDraft,
  cancelEditing,
  applyBulkReplaceTerms,
  commitEditing,
  discardSegmentDraft,
  enterEditing,
  getEditingSegments,
  getVoiceMap,
  listEditingSpeakers,
  patchSegmentText,
  previewBulkReplaceTerms,
  previewEditingSegmentSource,
  buildPreviewSourceStreamUrl,
  regenerateSegmentTts,
  regenerateAllDirtyTts,
  regenerateSelectedDirtyTts,
  retryEditingSpeakerProfile,
  revertUnsyncedTextSegments,
  getRegenerateAllStatus,
  cancelRegenerateAll,
  splitEditingSegmentMany,
  type CommitStrategy,
  type BulkReplacePreviewResponse,
  type EditingSegment,
  type EditingSegmentsResponse,
  type EditingSpeaker,
  type SegmentStatus,
  type UnsyncedTextSegment,
  type VoiceMapEntry,
} from "@/lib/api/editing"
import { getJob } from "@/lib/api/jobs"
import { ApiError, apiClient } from "@/lib/api/client"
import { buildStreamUrl } from "@/lib/api/downloads"
import { getErrorMessage } from "@/lib/api/errors"
import { getJobDisplayTitle } from "@/features/jobs/presentation"
import {
  usePlayerSegmentSync,
  type PlayerSyncSegment,
} from "@/lib/react/usePlayerSegmentSync"
import { usePollingTask } from "@/lib/react/usePollingTask"
import { SegmentRow } from "@/components/workspace/edit/SegmentRow"
import { CurrentSegmentOpsPanel } from "@/components/workspace/edit/CurrentSegmentOpsPanel"
import { SplitSegmentDialog } from "./SplitSegmentDialog"
import {
  SegmentVirtualList,
  type SegmentVirtualListRef,
} from "@/components/workspace/segments/SegmentVirtualList"
import type { JobSummary } from "@/types/jobs"
import { EditPageSpeakerCreateDialog } from "@/components/workspace/EditPageSpeakerCreateDialog"
import { VoiceModifyTab } from "./VoiceModifyTab"

// Feature flag: gating frontend entry so the page is never rendered when
// the backend won't honour the endpoints. Must match gateway
// AVT_ENABLE_POST_EDIT. Without this flag, the fetch calls still hit 404
// and the page would show an unhelpful error.
const POST_EDIT_ENABLED = process.env.NEXT_PUBLIC_ENABLE_POST_EDIT === "1"

function extractAudioSyncConflict(error: unknown): UnsyncedTextSegment[] | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const payload = error.payload
  if (!payload || typeof payload !== "object") return null
  if (!("code" in payload) || payload.code !== "editing_audio_sync_required") {
    return null
  }
  const segments = "unsynced_segments" in payload ? payload.unsynced_segments : null
  if (!Array.isArray(segments)) return []
  return segments
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    .map((item) => ({
      segment_id: String(item.segment_id ?? ""),
      status: typeof item.status === "string" ? item.status : undefined,
      display_name: typeof item.display_name === "string" ? item.display_name : undefined,
      speaker_id: typeof item.speaker_id === "string" ? item.speaker_id : undefined,
      current_cn_text: typeof item.current_cn_text === "string" ? item.current_cn_text : undefined,
      audio_cn_text: typeof item.audio_cn_text === "string" ? item.audio_cn_text : undefined,
      current_source_text: typeof item.current_source_text === "string" ? item.current_source_text : undefined,
      audio_source_text: typeof item.audio_source_text === "string" ? item.audio_source_text : undefined,
    }))
    .filter((item) => item.segment_id)
}

function isPreTtsLengthWarningStatus(status: SegmentStatus): boolean {
  return status === "text_dirty" || status === "voice_dirty" || status === "tts_failed"
}

function hasTtsLengthGuidanceWarning(
  segment: EditingSegment,
  status: SegmentStatus,
): boolean {
  if (!isPreTtsLengthWarningStatus(status)) return false
  const severity = segment.tts_length_guidance?.severity
  return severity === "mild" || severity === "warning" || severity === "severe"
}


export default function VideoEditPage() {
  const t = useTranslations("app")
  const params = useParams()
  const router = useRouter()
  const jobId = ((params.jobId as string) ?? "").trim()
  const { confirm, confirmDialog } = useConfirmDialog()
  const isMountedRef = useIsMountedRef()

  const [job, setJob] = useState<JobSummary | null>(null)
  const [resource, setResource] = useState<EditingSegmentsResponse | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)
  const [savingSegmentIds, setSavingSegmentIds] = useState<Set<string>>(new Set())
  const [regeneratingSegmentIds, setRegeneratingSegmentIds] = useState<Set<string>>(new Set())
  const [isBatchRegenerating, setIsBatchRegenerating] = useState(false)
  // D39 cancel: store the in-flight task_id so the "取消批量合成" button
  // can reach the backend cancel endpoint. null when no batch is running.
  const [bulkReplaceOpen, setBulkReplaceOpen] = useState(false)
  const [bulkReplaceFind, setBulkReplaceFind] = useState("")
  const [bulkReplaceValue, setBulkReplaceValue] = useState("")
  const [bulkReplacePreview, setBulkReplacePreview] = useState<BulkReplacePreviewResponse | null>(null)
  const [isBulkReplacePreviewing, setIsBulkReplacePreviewing] = useState(false)
  const [isBulkReplaceApplying, setIsBulkReplaceApplying] = useState(false)
  const [batchTaskId, setBatchTaskId] = useState<string | null>(null)
  // Optimistic state: set true the instant the cancel button is clicked
  // so the UI doesn't keep showing "正在合成" while the signal propagates
  // (~1-2s between click and worker observing cancel_requested).
  const [isCancellingBatch, setIsCancellingBatch] = useState(false)
  const [isCommitting, setIsCommitting] = useState(false)
  const commitInFlightRef = useRef(false)
  const [commitModalOpen, setCommitModalOpen] = useState(false)
  const [commitStrategy, setCommitStrategy] = useState<CommitStrategy>("overwrite")
  const [copyDisplayName, setCopyDisplayName] = useState<string>("")
  const [audioSyncConflict, setAudioSyncConflict] = useState<UnsyncedTextSegment[] | null>(null)
  const [isResolvingAudioSync, setIsResolvingAudioSync] = useState(false)
  const [activeTab, setActiveTab] = useState<"text" | "voice">("text")
  const [voiceMap, setVoiceMap] = useState<Record<string, VoiceMapEntry>>({})
  // Friendly speaker display names from review-state's
  // voice_selection_review stage. UI shows these instead of raw
  // `speaker_a` variable names. Empty map when review-state hasn't
  // been seeded (e.g. task never went through voice selection) — UI
  // falls back to the raw id.
  const [speakerNameMap, setSpeakerNameMap] = useState<Record<string, string>>({})
  // Editing-mode speakers (baseline + user-added). Loaded from
  // /editing/speakers; used to populate the "+ 新增说话人" dropdown
  // option and the profile-status badges in the voice tab.
  const [editingSpeakers, setEditingSpeakers] = useState<EditingSpeaker[]>([])
  const editingSpeakersRef = useRef<EditingSpeaker[]>([])
  const [createSpeakerDialogOpen, setCreateSpeakerDialogOpen] = useState(false)
  const virtualListRef = useRef<SegmentVirtualListRef>(null)
  // Phase 1 (Task 3): segment_id whose split modal should be shown.
  // SplitSegmentDialog component lands in Task 6; until then this just
  // tracks the trigger state but renders no UI.
  const [splitDialogSegmentId, setSplitDialogSegmentId] = useState<string | null>(null)

  // Phase 1 (Task 5 + Codex round-6 fix): track sticky video occluder
  // height on mobile so SegmentVirtualList.scrollToId can compensate.
  // On desktop the video is in the left column (not above the list),
  // so stickyOffset = 0 there. On mobile the video sits above the list
  // with `sticky top-0`, so we pass its rendered height.
  const stickyVideoRef = useRef<HTMLDivElement | null>(null)
  const [isMobileLayout, setIsMobileLayout] = useState(false)
  const [stickyVideoHeight, setStickyVideoHeight] = useState(0)

  useEffect(() => {
    if (typeof window === "undefined") return
    // 1024px matches the `lg` Tailwind breakpoint where the layout
    // switches from stacked (sticky video on top) to side-by-side.
    const mq = window.matchMedia("(max-width: 1023px)")
    const update = () => setIsMobileLayout(mq.matches)
    update()
    mq.addEventListener("change", update)
    return () => mq.removeEventListener("change", update)
  }, [])

  useEffect(() => {
    if (!isMobileLayout) {
      setStickyVideoHeight(0)
      return
    }
    const el = stickyVideoRef.current
    if (!el) return
    const sync = () => setStickyVideoHeight(el.offsetHeight)
    sync()
    const obs = new ResizeObserver(sync)
    obs.observe(el)
    return () => obs.disconnect()
  }, [isMobileLayout])

  // Sticky compensation: on mobile, the header (~52px) + sticky video
  // sit above the list. Plus a small breathing margin.
  const stickyOffsetPx = isMobileLayout ? stickyVideoHeight + 56 : 0

  // Mirror stickyOffsetPx into a ref so imperative scroll callbacks
  // (handleSplitSegmentMany, scrollToSegment) always read the LATEST value
  // without re-creating themselves on every mobile resize. Adding
  // stickyOffsetPx to those callbacks' deps would cascade re-renders
  // through every memoized child (Codex round-7 P2 #1).
  const stickyOffsetRef = useRef(0)
  useEffect(() => {
    stickyOffsetRef.current = stickyOffsetPx
  }, [stickyOffsetPx])

  // ---- Bootstrap ----

  const loadData = useCallback(async (): Promise<void> => {
    if (!jobId) return
    try {
      const [nextJob, nextRes, vmRes, reviewState] = await Promise.all([
        getJob(jobId),
        getEditingSegments(jobId),
        // voice_map failure shouldn't block text editing — default to empty.
        getVoiceMap(jobId).catch(() => ({ voice_map: {} })),
        // Speaker display-name map. Best-effort: if review-state isn't
        // loadable (transient 404 / disk issue) the dropdown falls back
        // to raw speaker_id strings — not blocking.
        apiClient
          .get<{
            results?: {
              review_flow?: {
                stages?: Record<string, { payload?: Record<string, unknown> }>
              }
            }
          }>(`/jobs/${jobId}/review-state`)
          .catch(() => null),
      ])
      setJob(nextJob)
      setResource(nextRes)
      setVoiceMap(vmRes.voice_map ?? {})
      // Extract speaker name mapping from voice_selection_review payload
      const stages = reviewState?.results?.review_flow?.stages ?? {}
      const vsPayload = stages.voice_selection_review?.payload ?? {}
      const rawSpeakers = Array.isArray(vsPayload.speakers) ? vsPayload.speakers : []
      const nameMap: Record<string, string> = {}
      for (const sp of rawSpeakers) {
        if (sp && typeof sp === "object") {
          const obj = sp as Record<string, unknown>
          const sid = String(obj.speaker_id ?? "").trim()
          const name = String(obj.speaker_name ?? "").trim()
          if (sid && name) nameMap[sid] = name
        }
      }
      setSpeakerNameMap(nameMap)
      setPageError(null)
    } catch (error) {
      setPageError(getErrorMessage(error))
    } finally {
      setIsLoading(false)
    }
  }, [jobId])

  // ---- Editing-mode speakers (Task 8) ----
  // Loaded from /editing/speakers. Background profile inference can take
  // 5-15s after a new speaker is created, so we poll while at least one
  // speaker is in "inferring" state. Failures degrade silently — the next
  // user action / refresh will retry.
  const refetchEditingSpeakers = useCallback(async () => {
    if (!jobId) return
    try {
      const list = await listEditingSpeakers(jobId)
      editingSpeakersRef.current = list
      setEditingSpeakers(list)
    } catch {
      // Best-effort: don't block the editor on this. The dropdown +
      // badges fall back to the baseline-derived availableSpeakerIds.
    }
  }, [jobId])

  useEffect(() => {
    void refetchEditingSpeakers()
  }, [refetchEditingSpeakers])

  const hasInferringSpeaker = useMemo(
    () => editingSpeakers.some((sp) => sp.profile_status === "inferring"),
    [editingSpeakers],
  )
  usePollingTask(refetchEditingSpeakers, {
    enabled: hasInferringSpeaker,
    immediate: false,
    intervalMs: 3000,
  })

  const handleSpeakerCreated = useCallback(
    (sp: EditingSpeaker) => {
      // Optimistic insert so the dropdown / voice card show up
      // immediately; refetch follows for authoritative state.
      setEditingSpeakers((prev) => {
        const next = prev.some((existing) => existing.speaker_id === sp.speaker_id)
          ? prev
          : [...prev, sp]
        editingSpeakersRef.current = next
        return next
      })
      void refetchEditingSpeakers()
    },
    [refetchEditingSpeakers],
  )

  const handleRetryProfile = useCallback(
    async (speakerId: string) => {
      try {
        await retryEditingSpeakerProfile(jobId, speakerId)
        toast.success("已重新触发音色画像推断")
        void refetchEditingSpeakers()
      } catch (error) {
        toast.error(`重试失败: ${getErrorMessage(error)}`)
      }
    },
    [jobId, refetchEditingSpeakers],
  )

  const existingSpeakerNames = useMemo(
    () =>
      new Set(
        editingSpeakers
          .map((sp) => sp.display_name.trim())
          .filter((name): name is string => name.length > 0),
      ),
    [editingSpeakers],
  )

  // First mount: if job isn't in editing yet, call enter-edit then load.
  // (Matches plan §7.1: first visit POSTs /enter-edit; subsequent visits
  // just load since state is already editing.)
  const enterAttemptedRef = useRef(false)
  useEffect(() => {
    if (!jobId) return
    if (enterAttemptedRef.current) return
    enterAttemptedRef.current = true

    ;(async () => {
      try {
        const current = await getJob(jobId)
        setJob(current)
        if (current.status !== "editing") {
          // Transition first. 409 here means someone raced us — load anyway.
          // D45: 10s watchdog toast — gateway should come back in <1s normally;
          // if it hasn't by 10s the user at least knows the request is in
          // flight (reassurance) instead of a silent blank screen.
          const slowToastTimer = window.setTimeout(() => {
            toast.loading("正在准备修改环境，可能网络较慢…", { id: "enter-edit-slow" })
          }, 10_000)
          try {
            await enterEditing(jobId)
          } catch (err) {
            const msg = getErrorMessage(err)
            if (!msg.includes("409")) {
              throw err
            }
          } finally {
            window.clearTimeout(slowToastTimer)
            toast.dismiss("enter-edit-slow")
          }
        }
        await loadData()
      } catch (error) {
        setPageError(getErrorMessage(error))
        setIsLoading(false)
      }
    })()
  }, [jobId, loadData])

  // ---- Segment text edit ----

  const handleTextChange = useCallback(
    async (segmentId: string, cn_text: string) => {
      setSavingSegmentIds((prev) => new Set(prev).add(segmentId))
      try {
        const result = await patchSegmentText(jobId, segmentId, { cn_text })
        setResource((prev) =>
          prev
            ? {
                ...prev,
                segments: prev.segments.map((s) =>
                  s.segment_id === segmentId ? { ...s, cn_text } : s,
                ),
                segment_status: result.segment_status,
              }
            : prev,
        )
      } catch (error) {
        toast.error(`保存失败: ${getErrorMessage(error)}`)
      } finally {
        setSavingSegmentIds((prev) => {
          const next = new Set(prev)
          next.delete(segmentId)
          return next
        })
      }
    },
    [jobId],
  )

  // ---- Speaker reassignment ----
  // Backend propagates voice_id + tts_provider from the new speaker's
  // baseline + clears any voice_map override on this segment + flags
  // voice_dirty so batch re-TTS picks it up. Response carries the
  // patched segment (with new voice_id/tts_provider) + refreshed
  // segment_status — mirror both into local state and drop any stale
  // voice_map entry for this segment.
  const handleSpeakerChange = useCallback(
    async (segmentId: string, speaker_id: string) => {
      setSavingSegmentIds((prev) => new Set(prev).add(segmentId))
      try {
        const result = await patchSegmentText(jobId, segmentId, { speaker_id })
        setResource((prev) =>
          prev
            ? {
                ...prev,
                segments: prev.segments.map((s) =>
                  s.segment_id === segmentId
                    ? {
                        ...s,
                        speaker_id,
                        voice_id: result.segment?.voice_id ?? s.voice_id,
                        tts_provider:
                          result.segment?.tts_provider ?? s.tts_provider,
                      }
                    : s,
                ),
                segment_status: result.segment_status,
              }
            : prev,
        )
        // Voice_map override was cleared server-side — mirror locally
        setVoiceMap((prev) => {
          if (!(segmentId in prev)) return prev
          const next = { ...prev }
          delete next[segmentId]
          return next
        })
        const friendly = speakerNameMap[speaker_id] || speaker_id
        toast.success(`已改为说话人 ${friendly}；重合成时将使用其音色`)
        // 2026-05-09: 段落归属到一个 editing-mode 新 speaker 时,后端会
        // 触发一次 voice profile 推断 (status: pending_segments →
        // inferring → ready/failed). 立即 refetch 让 UI 不停留在
        // "待归属段落"——polling 只在 status==='inferring' 时跑,推断
        // 太快直接到 ready 时 polling 永远没开始。
        void refetchEditingSpeakers()
      } catch (error) {
        toast.error(`改说话人失败: ${getErrorMessage(error)}`)
      } finally {
        setSavingSegmentIds((prev) => {
          const next = new Set(prev)
          next.delete(segmentId)
          return next
        })
      }
    },
    [jobId, speakerNameMap, refetchEditingSpeakers],
  )

  // ---- Source text (English) edit ----
  // 2026-04-21 plan §7.4: user may correct upstream ASR mistakes on the
  // original transcript. Backend marks text_dirty; user still needs to
  // update cn_text separately (no auto-retranslate).
  const handleSourceTextChange = useCallback(
    async (segmentId: string, source_text: string) => {
      setSavingSegmentIds((prev) => new Set(prev).add(segmentId))
      try {
        const result = await patchSegmentText(jobId, segmentId, { source_text })
        setResource((prev) =>
          prev
            ? {
                ...prev,
                segments: prev.segments.map((s) =>
                  s.segment_id === segmentId ? { ...s, source_text } : s,
                ),
                segment_status: result.segment_status,
              }
            : prev,
        )
      } catch (error) {
        toast.error(`保存失败: ${getErrorMessage(error)}`)
      } finally {
        setSavingSegmentIds((prev) => {
          const next = new Set(prev)
          next.delete(segmentId)
          return next
        })
      }
    },
    [jobId],
  )

  // ---- Split segment (Phase 2a multi-cut) ----
  // Plan §5.6: atomic N-cut split via POST /split-many. Backend uses
  // write-ahead journal; cuts strictly increasing in both indices;
  // speaker_ids length = cuts.length + 1.
  const handleSplitSegmentMany = useCallback(
    async (
      segmentId: string,
      body: {
        cuts: Array<{ source_index: number; cn_index: number }>
        speaker_ids: string[]
      },
    ) => {
      setSavingSegmentIds((prev) => new Set(prev).add(segmentId))
      try {
        const result = await splitEditingSegmentMany(jobId, segmentId, body)
        const firstNewSegmentId = result.new_segments[0]?.segment_id
        const revealFirstNewSegment = () => {
          if (!firstNewSegmentId) return
          window.requestAnimationFrame(() => {
            virtualListRef.current?.scrollToId(firstNewSegmentId, { align: "start", stickyOffset: stickyOffsetRef.current })
          })
        }
        setResource((prev) => {
          if (!prev) return prev
          const index = prev.segments.findIndex(
            (s) => s.segment_id === segmentId,
          )
          if (index < 0) return prev
          const nextSegments = [...prev.segments]
          nextSegments.splice(index, 1, ...result.new_segments)
          return {
            ...prev,
            segments: nextSegments,
            segment_status: result.segment_status,
            total: result.total_count,
          }
        })
        revealFirstNewSegment()
        try {
          const refreshed = await getEditingSegments(jobId)
          setResource(refreshed)
          revealFirstNewSegment()
        } catch {
          // Keep optimistic; next normal reload will resync.
        }
        toast.success(`拆分完成：${result.new_segments.length} 段，共 ${result.total_count} 段`)
      } catch (error) {
        toast.error(`拆分失败: ${getErrorMessage(error)}`)
      } finally {
        setSavingSegmentIds((prev) => {
          const next = new Set(prev)
          next.delete(segmentId)
          return next
        })
      }
    },
    [jobId],
  )

  // ---- Preview source audio ----
  // 2026-04-21 redesign: POST prepares the server-side WAV cache,
  // returns a URL string pointing at the Range-aware GET stream
  // endpoint. The segment card feeds that URL directly into
  // <audio src={...}>. Browsers handle Range / progressive playback
  // natively, so 30+ second segments (1 MB+ WAV) no longer trip the
  // Uvicorn+httpx big-JSON pathology that killed the old base64 path.
  const handlePreviewSource = useCallback(
    async (segmentId: string): Promise<string | null> => {
      try {
        await previewEditingSegmentSource(jobId, segmentId)
        // Nonce == server-side timestamp-ish; ensures <audio> refetches
        // after a re-POST (split / text edit) even with the same URL.
        return buildPreviewSourceStreamUrl(jobId, segmentId, Date.now())
      } catch (error) {
        toast.error(`原文试听失败: ${getErrorMessage(error)}`)
        return null
      }
    },
    [jobId],
  )

  // Distinct speaker ids currently present in the task — used to populate
  // the speaker-reassignment dropdown. Stable ordering by first appearance.
  const availableSpeakerIds = useMemo<string[]>(() => {
    if (!resource) return []
    const seen: string[] = []
    const set = new Set<string>()
    for (const s of resource.segments) {
      const sid = s.speaker_id
      if (!sid || set.has(sid)) continue
      set.add(sid)
      seen.push(sid)
    }
    return seen
  }, [resource])

  // ---- Regenerate TTS ----

  const handleRegenerate = useCallback(
    async (segmentId: string) => {
      if (regeneratingSegmentIds.has(segmentId)) return
      setRegeneratingSegmentIds((prev) => new Set(prev).add(segmentId))
      try {
        const result = await regenerateSegmentTts(jobId, segmentId)
        setResource((prev) =>
          prev ? { ...prev, segment_status: result.segment_status } : prev,
        )
        toast.success(`段 ${segmentId} 重合成完成`)
      } catch (error) {
        // 501 = TTS provider wiring not yet in place (B-task scope). The
        // backend message is the raw NotImplementedError string which does
        // not contain '501' or 'not implemented', so rely on the HTTP status
        // code surfaced by ApiError instead of substring matching.
        if (error instanceof ApiError && error.status === 501) {
          toast.error("TTS 合成后端尚未接入 — 功能即将上线")
        } else {
          toast.error(`重合成失败: ${getErrorMessage(error)}`)
        }
        // Refresh so UI reflects tts_failed
        void loadData()
      } finally {
        setRegeneratingSegmentIds((prev) => {
          const next = new Set(prev)
          next.delete(segmentId)
          return next
        })
      }
    },
    [jobId, loadData, regeneratingSegmentIds],
  )

  const handleAcceptDraft = useCallback(
    async (segmentId: string) => {
      try {
        const result = await acceptSegmentDraft(jobId, segmentId)
        setResource((prev) =>
          prev ? { ...prev, segment_status: result.segment_status } : prev,
        )
      } catch (error) {
        toast.error(`接受失败: ${getErrorMessage(error)}`)
      }
    },
    [jobId],
  )

  const handleDiscardDraft = useCallback(
    async (segmentId: string) => {
      try {
        const result = await discardSegmentDraft(jobId, segmentId)
        setResource((prev) =>
          prev ? { ...prev, segment_status: result.segment_status } : prev,
        )
      } catch (error) {
        toast.error(`丢弃失败: ${getErrorMessage(error)}`)
      }
    },
    [jobId],
  )

  // ---- Batch regenerate ----

  const handleBatchRegenerate = useCallback(async (): Promise<boolean> => {
    if (isBatchRegenerating) return false
    setIsBatchRegenerating(true)
    setIsCancellingBatch(false)
    // D39 async batch: POST returns a task_id immediately; progress
    // comes from polling GET /regenerate-all-tts/status. The single
    // sonner toast gets updated in-place via its id so the user sees
    // "合成中 3/100 · 段: seg_004" → "合成中 70/100 · ..." → final summary.
    // Fixed id "batch-regen" so handleCancelBatch can also address it.
    const toastId = "batch-regen"
    toast.loading("正在启动批量合成…", { id: toastId })
    const POLL_INTERVAL_MS = 1000
    const MAX_POLLS = 30 * 60  // 30 minutes; generous for 300+ segments
    let completedCleanly = false
    try {
      const { task_id: taskId } = await regenerateAllDirtyTts(jobId)
      setBatchTaskId(taskId)
      let polls = 0
      let lastDisplayedProgress = ""

      while (polls < MAX_POLLS) {
        await sleep(POLL_INTERVAL_MS)
        if (!isMountedRef.current) {
          // Page left mid-batch: the backend task keeps running on its own;
          // just stop tracking and drop the orphaned loading toast.
          toast.dismiss(toastId)
          return false
        }
        if (typeof document !== "undefined" && document.hidden) {
          continue
        }
        polls += 1
        const status = await getRegenerateAllStatus(jobId, taskId)
        if (!isMountedRef.current) {
          toast.dismiss(toastId)
          return false
        }

        // Someone launched another batch — old task id orphaned.
        if (status.mismatch) {
          toast.warning("检测到新的批量合成任务，停止跟踪当前进度", {
            id: toastId,
          })
          break
        }

        if (status.stage === "completed") {
          const result = status.result
          if (result && result.failed_count > 0) {
            toast.warning(
              `批量合成完成：成功 ${result.succeeded_count} 段，失败 ${result.failed_count} 段（${result.failed_segment_ids.join(", ")}）`,
              { id: toastId },
            )
          } else if (result && result.succeeded_count > 0) {
            toast.success(
              `批量合成完成，共 ${result.succeeded_count} 段`,
              { id: toastId },
            )
          } else {
            toast.info("没有需要重新合成的段落", { id: toastId })
          }
          completedCleanly = !result || result.failed_count <= 0
          break
        }

        if (status.stage === "failed") {
          toast.error(
            `批量合成失败: ${status.error ?? "未知错误"}`,
            { id: toastId },
          )
          break
        }

        // D39: user clicked 取消批量合成; worker has acknowledged and
        // transitioned to stage='cancelled' with partial counts.
        if (status.stage === "cancelled") {
          const result = status.result
          const succeeded = result?.succeeded_count ?? status.succeeded_count
          const remaining = status.total - succeeded - (result?.failed_count ?? status.failed_count)
          toast.info(
            `已取消批量合成：完成 ${succeeded} 段，剩余 ${Math.max(0, remaining)} 段未处理`,
            { id: toastId },
          )
          break
        }

        // Still running / starting — update progress toast in-place.
        const done = status.succeeded_count + status.failed_count
        const total = status.total || 0
        const currentSuffix = status.current_segment_id
          ? ` · 段 ${status.current_segment_id}`
          : ""
        const progressText = total > 0
          ? `批量合成中 ${done}/${total}${currentSuffix}`
          : "正在扫描需要合成的段落…"
        if (progressText !== lastDisplayedProgress) {
          toast.loading(progressText, { id: toastId })
          lastDisplayedProgress = progressText
        }
      }

      if (polls >= MAX_POLLS) {
        toast.error(
          "批量合成超过 30 分钟未完成，已停止跟踪。可刷新页面查看状态",
          { id: toastId },
        )
      }
      await loadData()
      return completedCleanly
    } catch (error) {
      toast.error(`批量合成失败: ${getErrorMessage(error)}`, { id: toastId })
      return false
    } finally {
      if (isMountedRef.current) {
        setIsBatchRegenerating(false)
        setBatchTaskId(null)
        setIsCancellingBatch(false)
      }
    }
  }, [isBatchRegenerating, isMountedRef, jobId, loadData])

  // D39 user-initiated cancel of a running batch. Writes the
  // cancel_requested flag on the backend; the running polling loop
  // inside handleBatchRegenerate will observe the stage='cancelled'
  // transition and exit cleanly. No need to interrupt the React
  // polling loop explicitly.
  const handleCancelBatch = useCallback(async () => {
    if (!batchTaskId || isCancellingBatch) return
    setIsCancellingBatch(true)
    try {
      const { cancelled } = await cancelRegenerateAll(jobId, batchTaskId)
      if (cancelled) {
        // Worker will flip stage within ~1s; the polling loop updates
        // the toast in-place.
        toast.loading("已请求取消，等待当前段合成结束…", {
          id: "batch-regen",  // matches handleBatchRegenerate's toastId
        })
      } else {
        toast.warning("取消请求未生效（任务可能已结束）", {
          id: "batch-regen",
        })
        setIsCancellingBatch(false)
      }
    } catch (error) {
      toast.error(`取消失败: ${getErrorMessage(error)}`)
      setIsCancellingBatch(false)
    }
  }, [batchTaskId, isCancellingBatch, jobId])

  const handleBulkReplacePreview = useCallback(async () => {
    const find = bulkReplaceFind
    if (!find) {
      toast.error("请输入要查找的词")
      return
    }
    setIsBulkReplacePreviewing(true)
    try {
      const preview = await previewBulkReplaceTerms(jobId, {
        find,
        replace: bulkReplaceValue,
      })
      setBulkReplacePreview(preview)
      if (preview.segment_count === 0) {
        toast.info("没有找到匹配段落")
      }
    } catch (error) {
      toast.error(`预览失败: ${getErrorMessage(error)}`)
    } finally {
      setIsBulkReplacePreviewing(false)
    }
  }, [bulkReplaceFind, bulkReplaceValue, jobId])

  const handleBulkReplaceApply = useCallback(async () => {
    if (!bulkReplacePreview || isBulkReplaceApplying || isBatchRegenerating) return
    const segmentIds = bulkReplacePreview.matches.map((item) => item.segment_id)
    if (segmentIds.length === 0) return
    setIsBulkReplaceApplying(true)
    setIsBatchRegenerating(true)
    setIsCancellingBatch(false)
    const toastId = "batch-regen"
    toast.loading(`正在替换并合成 ${segmentIds.length} 段...`, { id: toastId })
    try {
      const applied = await applyBulkReplaceTerms(jobId, {
        find: bulkReplacePreview.find,
        replace: bulkReplacePreview.replace,
        expected_segment_ids: segmentIds,
        expected_total_matches: bulkReplacePreview.total_matches,
        expected_matches: bulkReplacePreview.matches.map((match) => ({
          segment_id: match.segment_id,
          before_text: match.before_text,
        })),
      })
      setResource((prev) =>
        prev
          ? {
              ...prev,
              segments: applied.segments,
              segment_status: applied.segment_status,
            }
          : prev,
      )
      if (applied.replaced_segment_ids.length === 0) {
        toast.info("没有需要替换的段落", { id: toastId })
        setBulkReplaceOpen(false)
        return
      }

      const { task_id: taskId } = await regenerateSelectedDirtyTts(
        jobId,
        applied.replaced_segment_ids,
      )
      setBatchTaskId(taskId)

      const POLL_INTERVAL_MS = 1000
      const MAX_POLLS = 30 * 60
      let polls = 0
      let lastDisplayedProgress = ""
      while (polls < MAX_POLLS) {
        await sleep(POLL_INTERVAL_MS)
        if (!isMountedRef.current) {
          toast.dismiss(toastId)
          return
        }
        if (typeof document !== "undefined" && document.hidden) {
          continue
        }
        polls += 1
        const status = await getRegenerateAllStatus(jobId, taskId)
        if (!isMountedRef.current) {
          toast.dismiss(toastId)
          return
        }
        if (status.mismatch) {
          toast.warning("检测到新的合成任务，已停止跟踪当前进度", { id: toastId })
          break
        }
        if (status.stage === "completed") {
          const result = status.result
          if (result && result.failed_count > 0) {
            toast.warning(
              `替换完成，音频合成成功 ${result.succeeded_count} 段，失败 ${result.failed_count} 段`,
              { id: toastId },
            )
          } else {
            toast.success(`已替换并合成 ${applied.replaced_segment_ids.length} 段`, {
              id: toastId,
            })
          }
          break
        }
        if (status.stage === "failed") {
          toast.error(`合成失败: ${status.error ?? "未知错误"}`, { id: toastId })
          break
        }
        if (status.stage === "cancelled") {
          const succeeded = status.result?.succeeded_count ?? status.succeeded_count
          toast.info(`已取消：完成 ${succeeded} 段`, { id: toastId })
          break
        }
        const done = status.succeeded_count + status.failed_count
        const total = status.total || applied.replaced_segment_ids.length
        const currentSuffix = status.current_segment_id
          ? ` · 段 ${status.current_segment_id}`
          : ""
        const progressText = `正在合成替换段落 ${done}/${total}${currentSuffix}`
        if (progressText !== lastDisplayedProgress) {
          toast.loading(progressText, { id: toastId })
          lastDisplayedProgress = progressText
        }
      }
      if (polls >= MAX_POLLS) {
        toast.error("合成超过 30 分钟未完成，已停止跟踪。刷新页面可查看状态。", {
          id: toastId,
        })
      }
      setBulkReplaceOpen(false)
      setBulkReplacePreview(null)
      await loadData()
    } catch (error) {
      toast.error(`批量替换失败: ${getErrorMessage(error)}`, { id: toastId })
    } finally {
      if (isMountedRef.current) {
        setIsBulkReplaceApplying(false)
        setIsBatchRegenerating(false)
        setBatchTaskId(null)
        setIsCancellingBatch(false)
      }
    }
  }, [
    bulkReplacePreview,
    isBulkReplaceApplying,
    isBatchRegenerating,
    isMountedRef,
    jobId,
    loadData,
  ])

  // ---- Abandon / Commit ----

  const handleAbandon = useCallback(async () => {
    const confirmed = await confirm({
      title: "放弃修改",
      description: "确定要放弃本次修改吗？所有编辑将丢失，已消耗的点数不退。",
      destructive: true,
    })
    if (!confirmed) return
    try {
      await cancelEditing(jobId)
      toast.success("已放弃本次修改")
      router.push("/projects")
    } catch (error) {
      toast.error(`放弃失败: ${getErrorMessage(error)}`)
    }
  }, [confirm, jobId, router])

  const handleOpenCommitModal = useCallback(() => {
    setCommitStrategy("overwrite")
    // Optimistic local default; replaced below with the server's
    // collision-aware suggestion as soon as it arrives. We pre-seed so
    // the field isn't empty during the ~100-200 ms fetch.
    setCopyDisplayName(
      job ? `${getJobDisplayTitle(t, job)} · 副本 1` : "",
    )
    setCommitModalOpen(true)
    // Fire-and-forget server lookup: picks the correct N by counting
    // existing copies of this source job (plan §6.4 / D17).
    void (async () => {
      try {
        const response = await fetch(
          `/gateway/jobs/${jobId}/suggested-copy-name`,
          { credentials: "include" },
        )
        if (!response.ok) return
        const body = (await response.json()) as { suggested_name?: string }
        if (body?.suggested_name) {
          setCopyDisplayName(body.suggested_name)
        }
      } catch {
        // Network hiccup — keep the optimistic default. The user can
        // still edit before commit, and the backend collision check
        // resolves truly-ambiguous cases.
      }
    })()
  }, [job, jobId])

  const commitCurrentOptions = useCallback(async () => {
    const result = await commitEditing(jobId, commitStrategy, {
      copy_display_name:
        commitStrategy === "copy_as_new" ? copyDisplayName.trim() : undefined,
    })
    setCommitModalOpen(false)
    setAudioSyncConflict(null)
    if (result.strategy === "copy_as_new") {
      toast.success(`副本 "${result.new_display_name}" 已创建，开始重新生成`)
    } else {
      toast.success(`重新生成已开始，第 ${result.edit_generation} 次修改`)
    }
    router.push("/projects")
  }, [commitStrategy, copyDisplayName, jobId, router])

  const handleCommit = useCallback(async () => {
    if (commitInFlightRef.current || isCommitting) return
    if (commitStrategy === "copy_as_new" && !copyDisplayName.trim()) {
      toast.error("副本名不能为空")
      return
    }
    commitInFlightRef.current = true
    setIsCommitting(true)
    try {
      await commitCurrentOptions()
    } catch (error) {
      const conflict = extractAudioSyncConflict(error)
      if (conflict) {
        setAudioSyncConflict(conflict)
        return
      }
      toast.error(`合成失败: ${getErrorMessage(error)}`)
    } finally {
      commitInFlightRef.current = false
      setIsCommitting(false)
    }
  }, [commitCurrentOptions, commitStrategy, copyDisplayName, isCommitting])

  const handleRegenerateConflictAndCommit = useCallback(async () => {
    if (!audioSyncConflict || isResolvingAudioSync) return
    setIsResolvingAudioSync(true)
    try {
      const ok = await handleBatchRegenerate()
      if (!ok) return
      commitInFlightRef.current = true
      setIsCommitting(true)
      try {
        await commitCurrentOptions()
      } catch (error) {
        const conflict = extractAudioSyncConflict(error)
        if (conflict) {
          setAudioSyncConflict(conflict)
        } else {
          toast.error(`合成失败: ${getErrorMessage(error)}`)
        }
      } finally {
        commitInFlightRef.current = false
        setIsCommitting(false)
      }
    } finally {
      setIsResolvingAudioSync(false)
    }
  }, [audioSyncConflict, commitCurrentOptions, handleBatchRegenerate, isResolvingAudioSync])

  const handleRevertConflictTextAndCommit = useCallback(async () => {
    if (!audioSyncConflict || isResolvingAudioSync) return
    setIsResolvingAudioSync(true)
    try {
      const segmentIds = audioSyncConflict.map((item) => item.segment_id)
      const result = await revertUnsyncedTextSegments(jobId, segmentIds)
      setResource((prev) =>
        prev
          ? {
              ...prev,
              segments: result.segments,
              segment_status: result.segment_status,
            }
          : prev,
      )
      toast.success(`已放弃 ${result.reverted_segment_ids.length} 段未合成的文本修改`)
      commitInFlightRef.current = true
      setIsCommitting(true)
      try {
        await commitCurrentOptions()
      } catch (error) {
        const conflict = extractAudioSyncConflict(error)
        if (conflict) {
          setAudioSyncConflict(conflict)
        } else {
          toast.error(`合成失败: ${getErrorMessage(error)}`)
        }
      } finally {
        commitInFlightRef.current = false
        setIsCommitting(false)
      }
    } catch (error) {
      toast.error(`放弃文本修改失败: ${getErrorMessage(error)}`)
    } finally {
      setIsResolvingAudioSync(false)
    }
  }, [audioSyncConflict, commitCurrentOptions, isResolvingAudioSync, jobId])

  // ---- Derived ----

  const forceDspSegments = useMemo(() => {
    if (!resource) return []
    return resource.segments.filter(
      (s) => s.alignment_method === "force_dsp",
    )
  }, [resource])

  // D44 — segments whose draft wav deviates from the slot's target
  // duration by >20%. γ publish will DSP-stretch the draft to match
  // the slot no matter the ratio, but extreme ratios (≥1.5x or ≤0.67x)
  // produce audible quality loss (chipmunk / slow-mo). Warn the user
  // at edit time so they can shorten / lengthen the Chinese text
  // before committing.
  const draftDurationMismatchSegments = useMemo(() => {
    if (!resource) return []
    return resource.segments
      .map((seg) => {
        const target =
          typeof seg.target_duration_ms === "number"
            ? seg.target_duration_ms
            : null
        const draft =
          typeof seg.draft_wav_duration_ms === "number"
            ? seg.draft_wav_duration_ms
            : null
        if (target === null || draft === null || target <= 0) return null
        const ratio = draft / target
        // Soft threshold — atempo within [0.8x, 1.2x] is imperceptible.
        if (ratio >= 0.8 && ratio <= 1.2) return null
        return { seg, ratio, target, draft }
      })
      .filter(
        (x): x is { seg: EditingSegment; ratio: number; target: number; draft: number } =>
          x !== null,
      )
  }, [resource])

  const ttsLengthGuidanceWarningSegments = useMemo(() => {
    if (!resource) return []
    return resource.segments.filter((seg) => {
      const status = resource.segment_status[seg.segment_id] ?? "accepted"
      return hasTtsLengthGuidanceWarning(seg, status)
    })
  }, [resource])

  const scrollToSegment = useCallback((segmentId: string) => {
    // Prefer the virtual-list imperative API (it knows which items are
    // currently mounted and where each will land post-scroll). Fallback
    // to raw DOM anchor for non-virtualized layouts (e.g. early render
    // before the list mounts).
    if (virtualListRef.current) {
      virtualListRef.current.scrollToId(segmentId, { align: "center", stickyOffset: stickyOffsetRef.current })
      return
    }
    const el = document.getElementById(`segment-card-${segmentId}`)
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" })
    }
  }, [])

  // ---- Video player + segment sync (plan §7.2 / §9.2) ----
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const playerSyncSegments = useMemo<PlayerSyncSegment[]>(() => {
    if (!resource) return []
    return resource.segments
      .filter(
        (s): s is EditingSegment & { start_ms: number; end_ms: number } =>
          typeof s.start_ms === "number" && typeof s.end_ms === "number",
      )
      .map((s) => ({
        segmentId: s.segment_id,
        startMs: s.start_ms,
        endMs: s.end_ms,
      }))
  }, [resource])
  const { activeSegmentId } = usePlayerSegmentSync(videoRef, playerSyncSegments)
  // Auto-scroll to active segment is now handled inside SegmentVirtualList
  // (prop: activeSegmentId). No page-level effect needed.

  // Click on segment card → seek video to that segment's start
  const seekToSegment = useCallback((segmentId: string) => {
    const seg = resource?.segments.find((s) => s.segment_id === segmentId)
    const video = videoRef.current
    if (!seg || !video || typeof seg.start_ms !== "number") return
    video.currentTime = seg.start_ms / 1000
    // Don't autoplay; let the user decide (they may be editing silently).
  }, [resource])

  const dirtyCount = useMemo(() => {
    if (!resource) return 0
    return Object.values(resource.segment_status).filter(
      (s) => s === "text_dirty" || s === "voice_dirty" || s === "tts_failed",
    ).length
  }, [resource])

  // ---- Render ----

  if (!POST_EDIT_ENABLED) {
    return (
      <div className="max-w-2xl mx-auto mt-12 p-6 text-center">
        <h1 className="text-xl font-bold">视频修改功能未启用</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          本功能需要运维将环境变量 NEXT_PUBLIC_ENABLE_POST_EDIT 设为 1
          并重新部署前端。
        </p>
        <Link href={`/workspace/${jobId}`} className="mt-4 inline-block secondary-button">
          返回工作区
        </Link>
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[40vh]">
        <Loader2 className="h-6 w-6 animate-spin text-primary" />
        <span className="ml-3 text-sm text-muted-foreground">正在准备修改环境...</span>
      </div>
    )
  }

  if (pageError || !job || !resource) {
    return (
      <div className="max-w-2xl mx-auto mt-12 p-6 text-center">
        <h1 className="text-xl font-bold text-red-500">无法加载修改界面</h1>
        <p className="mt-2 text-sm text-muted-foreground">{pageError ?? "未知错误"}</p>
        <Link href={`/workspace/${jobId}`} className="mt-4 inline-block secondary-button">
          返回工作区
        </Link>
      </div>
    )
  }

  // Phase 1 redesign (Task 5): derive active segment + speaker name for the
  // left-column CurrentSegmentOpsPanel. activeSegmentId comes from
  // usePlayerSegmentSync; resolution to a full segment + friendly speaker
  // name happens at the page level so both left ops panel and right list
  // see the same source of truth.
  const activeSegment = activeSegmentId
    ? resource.segments.find((s) => s.segment_id === activeSegmentId) ?? null
    : null
  const activeSegmentStatus = activeSegment
    ? (resource.segment_status[activeSegment.segment_id] ?? "accepted")
    : null
  const activeSpeakerName = (() => {
    if (!activeSegment?.speaker_id) return null
    const raw = activeSegment.speaker_id
    if (speakerNameMap[raw]) return speakerNameMap[raw]
    const sp = editingSpeakers.find((e) => e.speaker_id === raw)
    return sp?.display_name || raw
  })()

  return (
    // Phase 1 redesign: left video + ops panel / right tab + segment list.
    // Mobile (< 1024px) stacks: header → sticky video → tab + list.
    <div className="space-y-3 lg:space-y-4 max-w-7xl mx-auto px-3 sm:px-0">
      {/* Header */}
      <section className="surface-card p-3 flex flex-wrap items-center gap-3 sticky top-0 z-20 backdrop-blur bg-card/85">
        <Link
          href={`/workspace/${jobId}`}
          className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted/30"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          返回
        </Link>
        <h1 className="text-base font-bold truncate min-w-0 flex-1">
          {getJobDisplayTitle(t, job)}
          {resource.edit_generation > 0 && (
            <span className="ml-2 text-xs text-muted-foreground font-normal">
              · 已修改 {resource.edit_generation} 次
            </span>
          )}
        </h1>
        {dirtyCount > 0 && (
          <span
            className="hidden sm:inline-flex items-center gap-1 rounded-full bg-primary/10 px-2.5 py-0.5 text-xs text-primary font-medium"
            aria-live="polite"
          >
            {dirtyCount} 段待处理
          </span>
        )}
        <div className="flex items-center gap-2 shrink-0">
          <button
            className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-1.5 text-xs font-medium text-red-400 hover:bg-red-500/20"
            onClick={handleAbandon}
            type="button"
          >
            放弃修改
          </button>
          <button
            className="rounded-md bg-primary px-4 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            onClick={handleOpenCommitModal}
            disabled={isCommitting}
            type="button"
          >
            确认修改 ↗
          </button>
        </div>
      </section>

      {/* Two-column layout: video+ops on left (sticky on desktop), tab+list on right */}
      <div className="grid grid-cols-1 lg:grid-cols-[minmax(320px,380px)_1fr] gap-3 lg:gap-4">
        {/* ── Left column ── */}
        <aside
          className="space-y-2 lg:sticky lg:top-[72px] lg:self-start"
          aria-label="视频与当前段操作"
        >
          {/* Sticky on both mobile (top of page) and desktop (top of left col).
           *  On mobile the height is measured (ResizeObserver) and passed to
           *  SegmentVirtualList as stickyOffsetForAutoScroll so the active
           *  row isn't occluded. Plan §2.2 + §8b.1. */}
          <div
            ref={stickyVideoRef}
            data-sticky-video
            className="sticky top-[52px] lg:top-0 z-10 lg:z-auto bg-background lg:bg-transparent"
          >
            <video
              ref={videoRef}
              className="w-full max-h-[30vh] lg:max-h-none lg:aspect-video rounded-md bg-black object-contain"
              controls
              preload="metadata"
              src={buildStreamUrl(jobId, "video")}
              poster={buildStreamUrl(jobId, "poster")}
              aria-label="译制视频"
            >
              您的浏览器不支持 video 标签
            </video>
          </div>
          {activeTab === "text" && (
            <CurrentSegmentOpsPanel
              jobId={jobId}
              segment={activeSegment}
              status={activeSegmentStatus}
              isRegenerating={
                activeSegment ? regeneratingSegmentIds.has(activeSegment.segment_id) : false
              }
              isSaving={
                activeSegment ? savingSegmentIds.has(activeSegment.segment_id) : false
              }
              isBatchRegenerating={isBatchRegenerating}
              speakerName={activeSpeakerName}
              onRegenerate={handleRegenerate}
              onAcceptDraft={handleAcceptDraft}
              onDiscardDraft={handleDiscardDraft}
              onSplit={(sid) => setSplitDialogSegmentId(sid)}
              onPreviewSource={handlePreviewSource}
            />
          )}
        </aside>

        {/* ── Right column ── */}
        <div className="space-y-3 min-w-0">
          {/* Tab switcher — full ARIA tabs pattern for screen readers. */}
          <nav
            className="flex items-center gap-1 border-b border-border"
            role="tablist"
            aria-label="修改阶段切换"
          >
        <button
          type="button"
          role="tab"
          id="tab-text"
          aria-selected={activeTab === "text"}
          aria-controls="panel-text"
          onClick={() => setActiveTab("text")}
          className={`px-4 py-2 text-sm font-medium -mb-px border-b-2 transition-colors min-h-[44px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 ${
            activeTab === "text"
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground"
          }`}
        >
          翻译修改
        </button>
        <button
          type="button"
          role="tab"
          id="tab-voice"
          aria-selected={activeTab === "voice"}
          aria-controls="panel-voice"
          onClick={() => setActiveTab("voice")}
          className={`px-4 py-2 text-sm font-medium -mb-px border-b-2 transition-colors min-h-[44px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 ${
            activeTab === "voice"
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground"
          }`}
        >
          音色修改
          {Object.keys(voiceMap).length > 0 && (
            <span className="ml-2 text-xs px-1.5 py-0.5 rounded-full bg-[color:var(--ochre)]/15 text-[color:var(--ochre)]">
              {new Set(
                Object.keys(voiceMap)
                  .map((sid) => resource.segments.find((s) => s.segment_id === sid)?.speaker_id)
                  .filter(Boolean),
              ).size}
            </span>
          )}
        </button>
      </nav>

      {activeTab === "text" ? (
        <main
          id="panel-text"
          role="tabpanel"
          aria-labelledby="tab-text"
          className="space-y-6"
        >
          {/* Batch actions + anomaly summary */}
          <section
            className="surface-card p-4 flex flex-wrap items-center gap-3"
            aria-label="批量操作与异常摘要"
          >
            <div
              className="flex-1 min-w-0 text-sm text-muted-foreground"
              role="status"
              aria-live="polite"
            >
              {dirtyCount > 0 ? (
                <span>
                  有 <strong className="text-foreground">{dirtyCount}</strong> 段待重合成。
                </span>
              ) : (
                <span>所有段落 TTS 都是最新的。</span>
              )}
              {forceDspSegments.length > 0 && (
                // 2026-04-21 plan §7.1 / D44: 用户要能"点击定位"到首个异常段。
                // 改成 button 复用 draft-duration-mismatch 的交互模式。
                <button
                  type="button"
                  className="ml-2 text-[color:var(--ochre)] underline decoration-dotted hover:opacity-80 min-h-[32px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ochre)] focus-visible:ring-offset-1 rounded"
                  onClick={() => scrollToSegment(forceDspSegments[0].segment_id)}
                  title="对齐器重写 2 次仍超/过短，建议精简译文。点击定位第一段。"
                >
                  ⚠ {forceDspSegments.length} 段时长异常（重写 2 次仍超/过短），点击定位
                </button>
              )}
              {ttsLengthGuidanceWarningSegments.length > 0 && (
                <button
                  type="button"
                  className="ml-2 text-[color:var(--ochre)] underline decoration-dotted hover:opacity-80 min-h-[32px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ochre)] focus-visible:ring-offset-1 rounded"
                  onClick={() =>
                    scrollToSegment(ttsLengthGuidanceWarningSegments[0].segment_id)
                  }
                  title="译文预计时长偏离目标。仍可强制合成，合成后会按真实音频时长走 DSP 对齐。点击定位第一段。"
                >
                  ⚠ {ttsLengthGuidanceWarningSegments.length} 段译文预计时长偏离目标，点击定位
                </button>
              )}
              {draftDurationMismatchSegments.length > 0 && (
                <button
                  type="button"
                  className="ml-2 text-[color:var(--ochre)] underline decoration-dotted hover:opacity-80 min-h-[32px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ochre)] focus-visible:ring-offset-1 rounded"
                  onClick={() =>
                    scrollToSegment(draftDurationMismatchSegments[0].seg.segment_id)
                  }
                  title="保存后新 TTS 将被 DSP 压缩/拉伸到目标时长，偏差过大时音质会明显下降。点击定位第一段。"
                >
                  ⚠ {draftDurationMismatchSegments.length} 段新 TTS 时长与目标偏差大，点击定位
                </button>
              )}
            </div>
            <div className="flex items-center gap-2">
              <button
                className="rounded-md border border-border px-4 py-1.5 text-xs inline-flex items-center gap-1 disabled:opacity-50 min-h-[40px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 hover:bg-muted"
                onClick={() => {
                  setBulkReplacePreview(null)
                  setBulkReplaceOpen(true)
                }}
                disabled={isBatchRegenerating}
                type="button"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                批量替换术语
              </button>
              <button
                className="rounded-md bg-primary/80 text-primary-foreground px-4 py-1.5 text-xs inline-flex items-center gap-1 disabled:opacity-50 min-h-[40px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
                onClick={handleBatchRegenerate}
                disabled={isBatchRegenerating || dirtyCount === 0}
                aria-busy={isBatchRegenerating}
                type="button"
              >
                {isBatchRegenerating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                一键合成所有未合成段落
              </button>
              {/* D39 §7.10: 批量合成进行中可取消。后端在当前段合成完后
               *   停止，已完成段保留，未处理段保持 dirty 状态下次可继续。*/}
              {isBatchRegenerating && batchTaskId && (
                <button
                  className="rounded-md px-3 py-1.5 text-xs inline-flex items-center gap-1 disabled:opacity-50 min-h-[40px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 border border-[color:var(--ochre)]/60 text-[color:var(--ochre)] hover:bg-[color:var(--ochre)]/10 focus-visible:ring-[color:var(--ochre)]"
                  onClick={handleCancelBatch}
                  disabled={isCancellingBatch}
                  aria-busy={isCancellingBatch}
                  type="button"
                  title="停止批量合成（完成当前段后停止，已合成的段保留）"
                >
                  {isCancellingBatch ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <X className="h-3.5 w-3.5" />
                  )}
                  {isCancellingBatch ? "正在取消…" : "取消批量合成"}
                </button>
              )}
            </div>
          </section>

          {/* Segment list — virtualized so 200+ segments don't balloon
              the DOM. Small tasks (≤ a few dozen segs) still render
              fine, the overhead is minimal. */}
          <section aria-label="段落编辑区">
            <SegmentVirtualList
              ref={virtualListRef}
              items={resource.segments}
              getId={(s) => s.segment_id}
              activeSegmentId={activeSegmentId}
              stickyOffsetForAutoScroll={stickyOffsetPx}
              estimatedItemHeight={200}
              maxHeight="70vh"
              className="pr-1"
              renderItem={(seg, idx) => (
                <SegmentRow
                  jobId={jobId}
                  index={idx}
                  segment={seg}
                  status={resource.segment_status[seg.segment_id] ?? "accepted"}
                  isSaving={savingSegmentIds.has(seg.segment_id)}
                  isRegenerating={regeneratingSegmentIds.has(seg.segment_id)}
                  isActive={activeSegmentId === seg.segment_id}
                  isBatchRegenerating={isBatchRegenerating}
                  availableSpeakerIds={availableSpeakerIds}
                  editingSpeakers={editingSpeakers}
                  speakerNameMap={speakerNameMap}
                  onTextChange={handleTextChange}
                  onSourceTextChange={handleSourceTextChange}
                  onSpeakerChange={handleSpeakerChange}
                  onRequestCreateSpeaker={() => setCreateSpeakerDialogOpen(true)}
                  onRegenerate={handleRegenerate}
                  onAcceptDraft={handleAcceptDraft}
                  onDiscardDraft={handleDiscardDraft}
                  onSeek={seekToSegment}
                  onSplit={(sid) => setSplitDialogSegmentId(sid)}
                  onPreviewSource={handlePreviewSource}
                />
              )}
            />
          </section>
        </main>
      ) : (
        <main
          id="panel-voice"
          role="tabpanel"
          aria-labelledby="tab-voice"
        >
          <VoiceModifyTab
            jobId={jobId}
            segments={resource.segments}
            voiceMap={voiceMap}
            onVoiceMapChange={setVoiceMap}
            editingSpeakers={editingSpeakers}
            onRequestCreateSpeaker={() => setCreateSpeakerDialogOpen(true)}
            onRetryProfile={handleRetryProfile}
          />
        </main>
      )}
        </div>
      </div>

      <SplitSegmentDialog
        open={splitDialogSegmentId !== null}
        jobId={jobId}
        segment={
          splitDialogSegmentId
            ? resource.segments.find((s) => s.segment_id === splitDialogSegmentId) ?? null
            : null
        }
        availableSpeakerIds={availableSpeakerIds}
        speakerNameMap={speakerNameMap}
        videoTitle={job ? getJobDisplayTitle(t, job) : undefined}
        onClose={() => setSplitDialogSegmentId(null)}
        onSubmit={async (sid, body) => {
          // Phase 2a: dialog always submits multi-cut payload. Backend's
          // split-many endpoint handles 1-cut (= 2 pieces) case fine.
          await handleSplitSegmentMany(sid, body)
        }}
      />

      {bulkReplaceOpen && (
        <BulkReplaceModal
          find={bulkReplaceFind}
          replace={bulkReplaceValue}
          preview={bulkReplacePreview}
          speakerNameMap={speakerNameMap}
          isPreviewing={isBulkReplacePreviewing}
          isApplying={isBulkReplaceApplying || isBatchRegenerating}
          onFindChange={(value) => {
            setBulkReplaceFind(value)
            setBulkReplacePreview(null)
          }}
          onReplaceChange={(value) => {
            setBulkReplaceValue(value)
            setBulkReplacePreview(null)
          }}
          onPreview={handleBulkReplacePreview}
          onApply={handleBulkReplaceApply}
          onClose={() => {
            if (isBulkReplaceApplying) return
            setBulkReplaceOpen(false)
          }}
        />
      )}

      {commitModalOpen && (
        <CommitModal
          strategy={commitStrategy}
          copyDisplayName={copyDisplayName}
          isSubmitting={isCommitting}
          onStrategyChange={setCommitStrategy}
          onCopyNameChange={setCopyDisplayName}
          onClose={() => setCommitModalOpen(false)}
          onSubmit={handleCommit}
        />
      )}

      {audioSyncConflict && (
        <AudioSyncConflictModal
          segments={audioSyncConflict}
          isSubmitting={isResolvingAudioSync || isBatchRegenerating || isCommitting}
          onRegenerateAndContinue={handleRegenerateConflictAndCommit}
          onRevertAndContinue={handleRevertConflictTextAndCommit}
          onClose={() => setAudioSyncConflict(null)}
        />
      )}

      <EditPageSpeakerCreateDialog
        jobId={jobId}
        open={createSpeakerDialogOpen}
        existingNames={existingSpeakerNames}
        onClose={() => setCreateSpeakerDialogOpen(false)}
        onCreated={handleSpeakerCreated}
      />

      {confirmDialog}
    </div>
  )
}

// ---------------------------------------------------------------------------
// SegmentCard moved to components/workspace/edit/SegmentRow.tsx (Phase 1 task 2)
// StatusChip + local formatMs were SegmentCard-private and deleted with it.
// ---------------------------------------------------------------------------


// ---------------------------------------------------------------------------
// CommitModal
// ---------------------------------------------------------------------------

interface BulkReplaceModalProps {
  find: string
  replace: string
  preview: BulkReplacePreviewResponse | null
  speakerNameMap: Record<string, string>
  isPreviewing: boolean
  isApplying: boolean
  onFindChange: (value: string) => void
  onReplaceChange: (value: string) => void
  onPreview: () => void
  onApply: () => void
  onClose: () => void
}

function BulkReplaceModal({
  find,
  replace,
  preview,
  speakerNameMap,
  isPreviewing,
  isApplying,
  onFindChange,
  onReplaceChange,
  onPreview,
  onApply,
  onClose,
}: BulkReplaceModalProps) {
  const shown = preview?.matches ?? []
  return (
    <div
      className="fixed inset-0 z-[115] flex items-center justify-center bg-black/65 p-4 backdrop-blur-[2px]"
      role="dialog"
      aria-modal="true"
      aria-labelledby="bulk-replace-modal-title"
    >
      <div className="w-full max-w-3xl max-h-[calc(100vh-2rem)] overflow-y-auto rounded-xl border border-border bg-card p-5 text-card-foreground shadow-2xl ring-1 ring-black/10 space-y-4 dark:ring-white/10">
        <div className="flex items-center justify-between gap-3">
          <h2 id="bulk-replace-modal-title" className="text-base font-bold">批量替换术语</h2>
          <button onClick={onClose} type="button" className="text-muted-foreground hover:text-foreground" disabled={isApplying}>
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1 text-sm">
            <span className="font-medium">查找</span>
            <input
              className="form-input w-full text-sm"
              value={find}
              onChange={(event) => onFindChange(event.currentTarget.value)}
              placeholder="令牌"
              disabled={isApplying}
            />
          </label>
          <label className="space-y-1 text-sm">
            <span className="font-medium">替换为</span>
            <input
              className="form-input w-full text-sm"
              value={replace}
              onChange={(event) => onReplaceChange(event.currentTarget.value)}
              placeholder="词元"
              disabled={isApplying}
            />
          </label>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-xs text-muted-foreground">
            {preview
              ? `命中 ${preview.segment_count} 段，${preview.total_matches} 处`
              : "先预览，再确认替换并合成对应段落音频"}
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onPreview}
            disabled={isPreviewing || isApplying || !find}
          >
            {isPreviewing ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
            )}
            预览替换
          </Button>
        </div>
        {preview && (
          <div className="max-h-[44vh] overflow-y-auto rounded-lg border border-border bg-background/60 text-xs">
            {shown.length === 0 ? (
              <div className="p-3 text-muted-foreground">没有匹配段落。</div>
            ) : (
              shown.map((match) => {
                const speakerName =
                  speakerNameMap[match.speaker_id] ||
                  match.speaker_display_name ||
                  match.speaker_id ||
                  "未命名说话人"
                return (
                  <div key={match.segment_id} className="border-b border-border/70 p-3 last:border-b-0">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-medium text-foreground">
                      <span>段落 {match.segment_id}</span>
                      <span>说话人 {speakerName}</span>
                      <span>音色 {match.provider || "默认"} / {match.voice_id || "默认"}</span>
                      {match.tts_model_key && <span>模型 {match.tts_model_key}</span>}
                    </div>
                    <div className="mt-2 grid gap-2 md:grid-cols-2">
                      <div className="break-words rounded-md bg-muted/60 p-2">
                        <div className="mb-1 text-muted-foreground">替换前</div>
                        {match.before_text}
                      </div>
                      <div className="break-words rounded-md bg-muted/60 p-2">
                        <div className="mb-1 text-muted-foreground">替换后</div>
                        {match.after_text}
                      </div>
                    </div>
                  </div>
                )
              })
            )}
          </div>
        )}
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="ghost" size="sm" onClick={onClose} disabled={isApplying}>
            取消
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={onApply}
            disabled={isApplying || !preview || preview.segment_count === 0}
          >
            {isApplying ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <Sparkles className="mr-1 h-3.5 w-3.5" />
            )}
            确认替换并合成 {preview?.segment_count ?? 0} 段
          </Button>
        </div>
      </div>
    </div>
  )
}

interface AudioSyncConflictModalProps {
  segments: UnsyncedTextSegment[]
  isSubmitting: boolean
  onRegenerateAndContinue: () => void
  onRevertAndContinue: () => void
  onClose: () => void
}

function AudioSyncConflictModal({
  segments,
  isSubmitting,
  onRegenerateAndContinue,
  onRevertAndContinue,
  onClose,
}: AudioSyncConflictModalProps) {
  const shown = segments.slice(0, 6)
  const extraCount = Math.max(0, segments.length - shown.length)
  return (
    <div
      className="fixed inset-0 z-[110] flex items-center justify-center bg-black/65 p-4 backdrop-blur-[2px]"
      role="dialog"
      aria-modal="true"
      aria-labelledby="audio-sync-modal-title"
    >
      <div className="w-full max-w-xl max-h-[calc(100vh-2rem)] overflow-y-auto rounded-xl border border-border bg-card p-5 text-card-foreground shadow-2xl ring-1 ring-black/10 space-y-4 dark:ring-white/10">
        <div className="flex items-center justify-between gap-3">
          <h2 id="audio-sync-modal-title" className="text-base font-bold">有文本修改尚未重新合成音频</h2>
          <button onClick={onClose} type="button" className="text-muted-foreground hover:text-foreground" disabled={isSubmitting}>
            <X className="h-4 w-4" />
          </button>
        </div>
        <p className="text-sm text-muted-foreground">
          这些段落当前音频仍对应修改前的文本。继续提交前，需要重新合成音频，或明确放弃这些文本修改。
        </p>
        <div className="space-y-2 rounded-lg border border-border bg-background/60 p-3 text-xs">
          {shown.map((segment) => (
            <div key={segment.segment_id} className="border-b border-border/70 pb-2 last:border-b-0 last:pb-0">
              <div className="font-medium text-foreground">
                段落 {segment.segment_id}
                {segment.display_name ? ` · ${segment.display_name}` : ""}
              </div>
              <div className="mt-1 line-clamp-2 text-muted-foreground">
                当前文本：{segment.current_cn_text || "空"}
              </div>
              <div className="mt-1 line-clamp-2 text-muted-foreground">
                当前音频对应：{segment.audio_cn_text || "空"}
              </div>
            </div>
          ))}
          {extraCount > 0 && (
            <div className="text-muted-foreground">还有 {extraCount} 段未显示。</div>
          )}
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="ghost" size="sm" onClick={onClose} disabled={isSubmitting}>
            返回编辑
          </Button>
          <Button type="button" variant="outline" size="sm" onClick={onRevertAndContinue} disabled={isSubmitting}>
            放弃这些文本修改并继续
          </Button>
          <Button type="button" size="sm" onClick={onRegenerateAndContinue} disabled={isSubmitting}>
            {isSubmitting ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
            )}
            重新合成并继续提交
          </Button>
        </div>
      </div>
    </div>
  )
}

interface CommitModalProps {
  strategy: CommitStrategy
  copyDisplayName: string
  isSubmitting: boolean
  onStrategyChange: (s: CommitStrategy) => void
  onCopyNameChange: (s: string) => void
  onClose: () => void
  onSubmit: () => void
}

function CommitModal({
  strategy,
  copyDisplayName,
  isSubmitting,
  onStrategyChange,
  onCopyNameChange,
  onClose,
  onSubmit,
}: CommitModalProps) {
  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/65 p-4 backdrop-blur-[2px]"
      role="dialog"
      aria-modal="true"
      aria-labelledby="commit-modal-title"
    >
      <div className="w-full max-w-lg max-h-[calc(100vh-2rem)] overflow-y-auto rounded-xl border border-border bg-card p-5 text-card-foreground shadow-2xl ring-1 ring-black/10 space-y-4 dark:ring-white/10">
        <div className="flex items-center justify-between">
          <h2 id="commit-modal-title" className="text-base font-bold">确认修改并重新生成视频</h2>
          <button onClick={onClose} type="button" className="text-muted-foreground hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-3 text-sm">
          <label className="flex items-start gap-2 cursor-pointer">
            <input
              type="radio"
              className="mt-1"
              checked={strategy === "overwrite"}
              onChange={() => onStrategyChange("overwrite")}
            />
            <div>
              <p className="font-medium">覆盖原任务（推荐）</p>
              <p className="text-xs text-muted-foreground mt-1">
                原配音视频 / 素材包 / 字幕会被替换。过期时间不变。
              </p>
            </div>
          </label>
          <label className="flex items-start gap-2 cursor-pointer">
            <input
              type="radio"
              className="mt-1"
              checked={strategy === "copy_as_new"}
              onChange={() => onStrategyChange("copy_as_new")}
            />
            <div className="flex-1">
              <p className="font-medium">保存为副本</p>
              <p className="text-xs text-muted-foreground mt-1">
                原任务保持不变。
              </p>
              {strategy === "copy_as_new" && (
                <input
                  className="mt-2 w-full form-input text-sm"
                  value={copyDisplayName}
                  onChange={(e) => onCopyNameChange(e.currentTarget.value)}
                  placeholder="副本名"
                />
              )}
            </div>
          </label>
        </div>
        <div className="text-xs text-muted-foreground border-t border-border pt-3">
          本次合成不额外扣费。已消耗的 TTS / 克隆点数不会因此退回。
          重新生成后无法回到本次编辑状态。
        </div>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" size="sm" onClick={onClose}>取消</Button>
          <Button
            type="button"
            size="sm"
            onClick={onSubmit}
            disabled={isSubmitting}
          >
            {isSubmitting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" />
            ) : (
              <PlaySquare className="h-3.5 w-3.5 mr-1" />
            )}
            确认并开始生成
          </Button>
        </div>
      </div>
    </div>
  )
}
