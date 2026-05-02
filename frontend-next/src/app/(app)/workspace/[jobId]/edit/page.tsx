"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import Link from "next/link"
import { useParams, useRouter } from "next/navigation"
import { toast } from "sonner"
import {
  ArrowLeft,
  Check,
  Loader2,
  Play,
  PlaySquare,
  RefreshCw,
  Scissors,
  Sparkles,
  Trash2,
  X,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  acceptSegmentDraft,
  cancelEditing,
  commitEditing,
  discardSegmentDraft,
  enterEditing,
  getEditingSegments,
  getVoiceMap,
  patchSegmentText,
  previewEditingSegmentSource,
  buildPreviewSourceStreamUrl,
  regenerateSegmentTts,
  regenerateAllDirtyTts,
  getRegenerateAllStatus,
  cancelRegenerateAll,
  splitEditingSegment,
  type CommitStrategy,
  type EditingSegment,
  type EditingSegmentsResponse,
  type SegmentStatus,
  type VoiceMapEntry,
} from "@/lib/api/editing"
import { getJob } from "@/lib/api/jobs"
import { ApiError, apiClient } from "@/lib/api/client"
import { buildDraftAudioUrl, buildStreamUrl } from "@/lib/api/downloads"
import { getErrorMessage } from "@/lib/api/errors"
import { getJobDisplayTitle } from "@/features/jobs/presentation"
import {
  usePlayerSegmentSync,
  type PlayerSyncSegment,
} from "@/lib/react/usePlayerSegmentSync"
import {
  SegmentVirtualList,
  type SegmentVirtualListRef,
} from "@/components/workspace/segments/SegmentVirtualList"
import type { JobSummary } from "@/types/jobs"
import { VoiceModifyTab } from "./VoiceModifyTab"

// Feature flag: gating frontend entry so the page is never rendered when
// the backend won't honour the endpoints. Must match gateway
// AVT_ENABLE_POST_EDIT. Without this flag, the fetch calls still hit 404
// and the page would show an unhelpful error.
const POST_EDIT_ENABLED = process.env.NEXT_PUBLIC_ENABLE_POST_EDIT === "1"


export default function VideoEditPage() {
  const params = useParams()
  const router = useRouter()
  const jobId = ((params.jobId as string) ?? "").trim()

  const [job, setJob] = useState<JobSummary | null>(null)
  const [resource, setResource] = useState<EditingSegmentsResponse | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)
  const [savingSegmentIds, setSavingSegmentIds] = useState<Set<string>>(new Set())
  const [regeneratingSegmentIds, setRegeneratingSegmentIds] = useState<Set<string>>(new Set())
  const [isBatchRegenerating, setIsBatchRegenerating] = useState(false)
  // D39 cancel: store the in-flight task_id so the "取消批量合成" button
  // can reach the backend cancel endpoint. null when no batch is running.
  const [batchTaskId, setBatchTaskId] = useState<string | null>(null)
  // Optimistic state: set true the instant the cancel button is clicked
  // so the UI doesn't keep showing "正在合成" while the signal propagates
  // (~1-2s between click and worker observing cancel_requested).
  const [isCancellingBatch, setIsCancellingBatch] = useState(false)
  const [isCommitting, setIsCommitting] = useState(false)
  const [commitModalOpen, setCommitModalOpen] = useState(false)
  const [commitStrategy, setCommitStrategy] = useState<CommitStrategy>("overwrite")
  const [copyDisplayName, setCopyDisplayName] = useState<string>("")
  const [activeTab, setActiveTab] = useState<"text" | "voice">("text")
  const [voiceMap, setVoiceMap] = useState<Record<string, VoiceMapEntry>>({})
  // Friendly speaker display names from review-state's
  // voice_selection_review stage. UI shows these instead of raw
  // `speaker_a` variable names. Empty map when review-state hasn't
  // been seeded (e.g. task never went through voice selection) — UI
  // falls back to the raw id.
  const [speakerNameMap, setSpeakerNameMap] = useState<Record<string, string>>({})
  const virtualListRef = useRef<SegmentVirtualListRef>(null)

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
    [jobId, speakerNameMap],
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

  // ---- Split segment ----
  // Plan §7.4: user chooses character positions in source_text + cn_text;
  // backend computes new timestamps proportionally and returns both halves
  // + refreshed status map for in-place splice into local state.
  const handleSplitSegment = useCallback(
    async (
      segmentId: string,
      body: {
        split_source_index: number
        split_cn_index: number
        speaker_a: string
        speaker_b: string
      },
    ) => {
      setSavingSegmentIds((prev) => new Set(prev).add(segmentId))
      try {
        const result = await splitEditingSegment(jobId, segmentId, body)
        const firstNewSegmentId = result.new_segments[0]?.segment_id
        const revealFirstNewSegment = () => {
          if (!firstNewSegmentId) return
          window.requestAnimationFrame(() => {
            virtualListRef.current?.scrollToId(firstNewSegmentId, { align: "start" })
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
          // Keep the optimistic split visible; the next normal reload will resync.
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

  const handleBatchRegenerate = useCallback(async () => {
    if (isBatchRegenerating) return
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
    try {
      const { task_id: taskId } = await regenerateAllDirtyTts(jobId)
      setBatchTaskId(taskId)
      let polls = 0
      let lastDisplayedProgress = ""

      while (polls < MAX_POLLS) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
        polls += 1
        const status = await getRegenerateAllStatus(jobId, taskId)

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
    } catch (error) {
      toast.error(`批量合成失败: ${getErrorMessage(error)}`, { id: toastId })
    } finally {
      setIsBatchRegenerating(false)
      setBatchTaskId(null)
      setIsCancellingBatch(false)
    }
  }, [isBatchRegenerating, jobId, loadData])

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

  // ---- Abandon / Commit ----

  const handleAbandon = useCallback(async () => {
    if (!window.confirm("确定要放弃本次修改吗？所有编辑将丢失，已消耗的点数不退。")) return
    try {
      await cancelEditing(jobId)
      toast.success("已放弃本次修改")
      router.push("/projects")
    } catch (error) {
      toast.error(`放弃失败: ${getErrorMessage(error)}`)
    }
  }, [jobId, router])

  const handleOpenCommitModal = useCallback(() => {
    setCommitStrategy("overwrite")
    // Optimistic local default; replaced below with the server's
    // collision-aware suggestion as soon as it arrives. We pre-seed so
    // the field isn't empty during the ~100-200 ms fetch.
    setCopyDisplayName(
      job ? `${getJobDisplayTitle(job)} · 副本 1` : "",
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

  const handleCommit = useCallback(async () => {
    if (isCommitting) return
    if (commitStrategy === "copy_as_new" && !copyDisplayName.trim()) {
      toast.error("副本名不能为空")
      return
    }
    if (!window.confirm("重新生成后无法回到本次编辑状态，是否继续？")) return
    setIsCommitting(true)
    try {
      const result = await commitEditing(jobId, commitStrategy, {
        copy_display_name:
          commitStrategy === "copy_as_new" ? copyDisplayName.trim() : undefined,
      })
      setCommitModalOpen(false)
      if (result.strategy === "copy_as_new") {
        // D8 plan §12: commit 会重跑 alignment → publish，三语 SRT 同步重
        // 生成，用户下载到的字幕是最新编辑后的内容。
        toast.success(`副本 "${result.new_display_name}" 已创建，开始重合成（视频 + 字幕）`)
      } else {
        toast.success(`重合成开始 · 第 ${result.edit_generation} 次修改（视频 + 字幕同步更新）`)
      }
      // Both strategies land on /projects: user's mental model after
      // 确认修改 is "back to the list to watch progress", parallel to
      // 放弃修改's /projects push.
      router.push("/projects")
    } catch (error) {
      toast.error(`合成失败: ${getErrorMessage(error)}`)
    } finally {
      setIsCommitting(false)
    }
  }, [commitStrategy, copyDisplayName, isCommitting, jobId, router])

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

  const scrollToSegment = useCallback((segmentId: string) => {
    // Prefer the virtual-list imperative API (it knows which items are
    // currently mounted and where each will land post-scroll). Fallback
    // to raw DOM anchor for non-virtualized layouts (e.g. early render
    // before the list mounts).
    if (virtualListRef.current) {
      virtualListRef.current.scrollToId(segmentId, { align: "center" })
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

  return (
    // §7.9 responsive: desktop 1024+ caps at max-w-5xl (1024px content),
    // phone/tablet span full width with adjusted padding. Section
    // `space-y` shrinks on narrow viewports to save vertical space.
    <div className="space-y-4 sm:space-y-6 max-w-5xl mx-auto px-3 sm:px-0">
      {/* Header */}
      <section className="surface-card p-4 flex flex-wrap items-center gap-3">
        <Link
          href={`/workspace/${jobId}`}
          className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted/30"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          返回
        </Link>
        <h1 className="text-lg font-bold truncate min-w-0 flex-1">
          {getJobDisplayTitle(job)}
          {resource.edit_generation > 0 && (
            <span className="ml-2 text-sm text-muted-foreground font-normal">
              · 已修改 {resource.edit_generation} 次
            </span>
          )}
        </h1>
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
            确认修改 ...
          </button>
        </div>
      </section>

      {/* Sticky video player — baseline (last-committed) dubbed video
          so users can audit text edits against the audio they're about
          to replace. §7.9 responsive: cap height 40vh on phones,
          45vh elsewhere. */}
      <aside
        className="sticky top-2 z-10 surface-card p-2"
        aria-label="视频预览"
      >
        <video
          ref={videoRef}
          className="w-full max-h-[40vh] sm:max-h-[45vh] rounded-md bg-black object-contain"
          controls
          preload="metadata"
          src={buildStreamUrl(jobId, "video")}
          poster={buildStreamUrl(jobId, "poster")}
        >
          您的浏览器不支持 video 标签
        </video>
      </aside>

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
              estimatedItemHeight={200}
              maxHeight="70vh"
              className="pr-1"
              renderItem={(seg, idx) => (
                <div className="pb-3">
                  <SegmentCard
                    jobId={jobId}
                    index={idx}
                    segment={seg}
                    status={resource.segment_status[seg.segment_id] ?? "accepted"}
                    isSaving={savingSegmentIds.has(seg.segment_id)}
                    isRegenerating={regeneratingSegmentIds.has(seg.segment_id)}
                    isActive={activeSegmentId === seg.segment_id}
                    availableSpeakerIds={availableSpeakerIds}
                    speakerNameMap={speakerNameMap}
                    onTextChange={handleTextChange}
                    onSourceTextChange={handleSourceTextChange}
                    onSpeakerChange={handleSpeakerChange}
                    onRegenerate={handleRegenerate}
                    onAcceptDraft={handleAcceptDraft}
                    onDiscardDraft={handleDiscardDraft}
                    onSeek={seekToSegment}
                    onSplit={handleSplitSegment}
                    onPreviewSource={handlePreviewSource}
                  />
                </div>
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
          />
        </main>
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
    </div>
  )
}

// ---------------------------------------------------------------------------
// SegmentCard — one row per segment
// ---------------------------------------------------------------------------

interface SegmentCardProps {
  jobId: string
  index: number
  segment: EditingSegment
  status: SegmentStatus
  isSaving: boolean
  isRegenerating: boolean
  /** True when the player is currently inside [start_ms, end_ms] of this
   *  segment (plan §9.2 usePlayerSegmentSync). Drives ring highlight. */
  isActive: boolean
  /** All speaker_ids currently used somewhere in the task — populates
   *  the reassignment dropdown. 2026-04-20: plan §7.4 speaker fix flow. */
  availableSpeakerIds: string[]
  /** Friendly display names per speaker_id (from review-state's
   *  voice_selection_review payload). UI shows these instead of raw
   *  variable-name speaker_ids. Missing entry → UI falls back to the id. */
  speakerNameMap: Record<string, string>
  onTextChange: (segmentId: string, cnText: string) => void
  /** 2026-04-21 plan §7.4: edit the English source_text. Backend marks
   *  text_dirty; user is responsible for also updating cn_text. */
  onSourceTextChange: (segmentId: string, sourceText: string) => void
  onSpeakerChange: (segmentId: string, speakerId: string) => void
  onRegenerate: (segmentId: string) => void
  onAcceptDraft: (segmentId: string) => void
  onDiscardDraft: (segmentId: string) => void
  /** Jump video playback to this segment's start_ms (click the time
   *  label). No-op when video ref isn't ready or segment has no timing. */
  onSeek: (segmentId: string) => void
  /** 2026-04-21 plan §7.4: split this segment at user-chosen positions. */
  onSplit: (
    segmentId: string,
    body: {
      split_source_index: number
      split_cn_index: number
      speaker_a: string
      speaker_b: string
    },
  ) => Promise<void> | void
  /** 2026-04-21 plan §7.4: fetch base64 source audio slice for this
   *  segment. Returns a ``data:audio/wav;base64,...`` URL on success, or
   *  ``null`` on failure (the handler has already shown a toast). */
  onPreviewSource: (segmentId: string) => Promise<string | null>
}

function SegmentCard({
  jobId,
  index,
  segment,
  status,
  isSaving,
  isRegenerating,
  isActive,
  availableSpeakerIds,
  speakerNameMap,
  onTextChange,
  onSourceTextChange,
  onSpeakerChange,
  onRegenerate,
  onAcceptDraft,
  onDiscardDraft,
  onSeek,
  onSplit,
  onPreviewSource,
}: SegmentCardProps) {
  // Prefer friendly display names when available; fall back to the raw
  // speaker_id so tasks that never went through voice_selection_review
  // (or ones where the name map failed to load) still render something.
  const speakerLabel = (sid: string | undefined): string => {
    if (!sid) return ""
    return speakerNameMap[sid] || sid
  }
  const [localText, setLocalText] = useState(segment.cn_text ?? "")
  useEffect(() => { setLocalText(segment.cn_text ?? "") }, [segment.cn_text])

  // English source text editor (2026-04-21 plan §7.4). Separate local
  // buffer + effect so reloading from the parent doesn't wipe in-flight
  // edits.
  const [localSource, setLocalSource] = useState(segment.source_text ?? "")
  useEffect(() => { setLocalSource(segment.source_text ?? "") }, [segment.source_text])

  // Source-audio preview state. URL is fetched lazily on first click so
  // we don't hit the backend for every rendered segment.
  const [sourceAudioUrl, setSourceAudioUrl] = useState<string | null>(null)
  const [isFetchingSource, setIsFetchingSource] = useState(false)
  const sourceAudioRef = useRef<HTMLAudioElement | null>(null)

  // Split panel — closed by default; open toggles inline below the editor
  // row so users see the impact immediately without a modal.
  const [splitOpen, setSplitOpen] = useState(false)
  const [splitSourcePos, setSplitSourcePos] = useState(0)
  const [splitCnPos, setSplitCnPos] = useState(0)
  const [splitSpeakerA, setSplitSpeakerA] = useState(segment.speaker_id ?? "")
  const [splitSpeakerB, setSplitSpeakerB] = useState(segment.speaker_id ?? "")
  const [isSplitting, setIsSplitting] = useState(false)

  const handleOpenSplit = () => {
    // Seed midpoints; both must be in (0, len) for the backend to accept.
    const srcLen = (segment.source_text ?? "").length
    const cnLen = (segment.cn_text ?? "").length
    setSplitSourcePos(srcLen > 1 ? Math.floor(srcLen / 2) : 1)
    setSplitCnPos(cnLen > 1 ? Math.floor(cnLen / 2) : 1)
    setSplitSpeakerA(segment.speaker_id ?? (availableSpeakerIds[0] ?? ""))
    setSplitSpeakerB(segment.speaker_id ?? (availableSpeakerIds[0] ?? ""))
    setSplitOpen(true)
  }

  const handleConfirmSplit = async () => {
    if (isSplitting) return
    setIsSplitting(true)
    try {
      await onSplit(segment.segment_id, {
        split_source_index: splitSourcePos,
        split_cn_index: splitCnPos,
        speaker_a: splitSpeakerA,
        speaker_b: splitSpeakerB,
      })
      setSplitOpen(false)
    } finally {
      setIsSplitting(false)
    }
  }

  const handlePlaySource = async () => {
    if (sourceAudioUrl) {
      // Already fetched — just replay.
      try {
        const el = sourceAudioRef.current
        if (el) {
          el.currentTime = 0
          await el.play()
        }
      } catch {
        // Browser auto-play policy may block without a gesture; the
        // visible <audio controls> element below lets the user click
        // through the default UI as a fallback.
      }
      return
    }
    setIsFetchingSource(true)
    try {
      const url = await onPreviewSource(segment.segment_id)
      if (url) {
        setSourceAudioUrl(url)
        // Attempt immediate playback; swallow rejection silently — the
        // rendered <audio controls> is the user-facing fallback.
        setTimeout(() => {
          sourceAudioRef.current?.play().catch(() => {})
        }, 50)
      }
    } finally {
      setIsFetchingSource(false)
    }
  }

  const isAnomalous = segment.alignment_method === "force_dsp"

  // D44 draft-duration mismatch (only when a draft wav exists AND
  // deviates >20% from the slot). See page-level memo for rationale.
  const target =
    typeof segment.target_duration_ms === "number"
      ? segment.target_duration_ms
      : null
  const draft =
    typeof segment.draft_wav_duration_ms === "number"
      ? segment.draft_wav_duration_ms
      : null
  const draftRatio =
    target !== null && draft !== null && target > 0 ? draft / target : null
  const hasDraftMismatch = draftRatio !== null && (draftRatio < 0.8 || draftRatio > 1.2)
  const draftMismatchSeverity =
    draftRatio === null
      ? null
      : draftRatio < 0.67 || draftRatio > 1.5
      ? "severe"
      : "mild"

  const timeLabel = segment.start_ms !== undefined && segment.end_ms !== undefined
    ? `${formatMs(segment.start_ms)} - ${formatMs(segment.end_ms)}`
    : ""

  const borderClass =
    hasDraftMismatch && draftMismatchSeverity === "severe"
      ? "border-l-4 border-l-red-500"
      : hasDraftMismatch
      ? "border-l-4 border-l-[color:var(--ochre)]"
      : isAnomalous
      ? "border-l-4 border-l-red-500"
      : ""

  // Screen reader friendly summary for the whole segment card
  const screenReaderSummary = [
    `段落 ${index + 1}`,
    segment.speaker_id ? `说话人 ${speakerLabel(segment.speaker_id)}` : null,
    timeLabel || null,
    isAnomalous
      ? `时长异常${
          segment.duration_diff_ratio !== undefined
            ? `，偏差 ${Math.round(segment.duration_diff_ratio * 100)}%`
            : ""
        }`
      : null,
  ].filter(Boolean).join("，")

  return (
    <article
      id={`segment-card-${segment.segment_id}`}
      aria-label={screenReaderSummary}
      className={`surface-card p-4 ${borderClass} ${
        isActive ? "ring-2 ring-primary/70 shadow-lg shadow-primary/10" : ""
      } transition-shadow`}
    >
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground mb-2">
        <span className="font-mono">#{index + 1}</span>
        {timeLabel && (
          <button
            type="button"
            className="text-primary/80 underline decoration-dotted underline-offset-2 hover:text-primary hover:decoration-solid focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 rounded"
            onClick={() => onSeek(segment.segment_id)}
            title="跳转视频到该段起点"
            aria-label={`跳转视频到 ${timeLabel}`}
          >
            {timeLabel}
          </button>
        )}
        {segment.speaker_id && (
          <label className="inline-flex items-center gap-1">
            <span>说话人</span>
            {availableSpeakerIds.length > 1 ? (
              <select
                className="rounded border border-border bg-background px-1.5 py-0.5 text-xs text-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
                value={segment.speaker_id}
                disabled={isSaving}
                onChange={(e) => {
                  const next = e.currentTarget.value
                  if (next && next !== segment.speaker_id) {
                    onSpeakerChange(segment.segment_id, next)
                  }
                }}
                title="改说话人归属：重合成时自动换成新说话人的音色"
                aria-label="修改该段说话人归属"
              >
                {availableSpeakerIds.map((sid) => (
                  <option key={sid} value={sid}>
                    {speakerLabel(sid)}
                  </option>
                ))}
              </select>
            ) : (
              <span>{speakerLabel(segment.speaker_id)}</span>
            )}
          </label>
        )}
        <StatusChip status={status} />
        {isAnomalous && (
          <span className="text-red-500" role="img" aria-label="时长异常">
            ⚠ 时长异常
            {segment.duration_diff_ratio !== undefined && (
              <span className="ml-1">
                （{(segment.duration_diff_ratio * 100).toFixed(0)}%）
              </span>
            )}
          </span>
        )}
        {hasDraftMismatch && draftRatio !== null && target !== null && draft !== null && (
          <span
            className={
              draftMismatchSeverity === "severe"
                ? "text-red-500"
                : "text-[color:var(--ochre)]"
            }
            title={
              `新 TTS ${(draft / 1000).toFixed(1)}s / 目标 ${(target / 1000).toFixed(1)}s。` +
              "保存后会被 DSP 拉伸到目标时长，偏差越大音质下降越明显。" +
              (draftMismatchSeverity === "severe"
                ? "建议精简译文后重新合成。"
                : "")
            }
          >
            ⚠ 新 TTS {(draft / 1000).toFixed(1)}s / 目标 {(target / 1000).toFixed(1)}s
            <span className="ml-1">
              （{draftRatio > 1 ? "+" : ""}{((draftRatio - 1) * 100).toFixed(0)}%）
            </span>
          </span>
        )}
      </div>
      {/* English source text — editable on blur. Users correct upstream
       *  ASR mistakes here; backend marks text_dirty so the next re-TTS
       *  picks up the new content. User still needs to update the
       *  translation below (no auto-retranslate). */}
      <div className="mb-2">
        <div className="flex items-center justify-between mb-1">
          <label className="text-xs text-muted-foreground">原文</label>
          <div className="flex items-center gap-1">
            <Button
              size="sm"
              variant="ghost"
              onClick={handlePlaySource}
              disabled={isFetchingSource}
              aria-label="试听该段原文音频"
              title="播放原文"
            >
              {isFetchingSource ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" />
              ) : (
                <Play className="h-3.5 w-3.5 mr-1" />
              )}
              播放原文
            </Button>
          </div>
        </div>
        <textarea
          className="w-full min-h-[56px] rounded-md border border-border bg-background/60 p-2 text-xs text-muted-foreground font-sans"
          value={localSource}
          onChange={(e) => setLocalSource(e.currentTarget.value)}
          onBlur={() => {
            if (localSource !== (segment.source_text ?? "")) {
              onSourceTextChange(segment.segment_id, localSource)
            }
          }}
        />
        {sourceAudioUrl && (
          <audio
            ref={sourceAudioRef}
            key={`src-${segment.segment_id}`}
            controls
            preload="metadata"
            className="mt-1 w-full max-w-md h-8"
            src={sourceAudioUrl}
          />
        )}
      </div>
      <label className="text-xs text-muted-foreground block mb-1">译文</label>
      <textarea
        className="w-full min-h-[72px] rounded-md border border-border bg-background p-2 text-sm"
        value={localText}
        onChange={(e) => setLocalText(e.currentTarget.value)}
        onBlur={() => {
          if (localText !== (segment.cn_text ?? "")) {
            onTextChange(segment.segment_id, localText)
          }
        }}
      />
      {/* Split panel — inline so users see the halves live. Mirrors the
       *  main flow TranslationReviewPanel's amber-outlined panel. */}
      {splitOpen && (
        <div className="mt-2 rounded-md border border-[color:var(--ochre)]/30 bg-[color:var(--ochre)]/8 p-3 space-y-3">
          <div>
            <div className="flex items-center justify-between text-xs mb-1">
              <span className="text-muted-foreground">原文拆分位置</span>
              <span className="font-mono text-[color:var(--ochre)]">
                {splitSourcePos} / {(segment.source_text ?? "").length}
              </span>
            </div>
            <input
              type="range"
              min={1}
              max={Math.max(1, (segment.source_text ?? "").length - 1)}
              value={splitSourcePos}
              onChange={(e) => setSplitSourcePos(parseInt(e.currentTarget.value, 10))}
              className="w-full accent-[color:var(--ochre)]"
              aria-label="原文拆分位置"
            />
            <div className="mt-1 text-xs grid grid-cols-2 gap-2">
              <div className="rounded bg-background/60 px-2 py-1 text-muted-foreground">
                A：{(segment.source_text ?? "").slice(0, splitSourcePos) || "（空）"}
              </div>
              <div className="rounded bg-background/60 px-2 py-1 text-muted-foreground">
                B：{(segment.source_text ?? "").slice(splitSourcePos) || "（空）"}
              </div>
            </div>
          </div>
          <div>
            <div className="flex items-center justify-between text-xs mb-1">
              <span className="text-muted-foreground">译文拆分位置</span>
              <span className="font-mono text-[color:var(--ochre)]">
                {splitCnPos} / {(segment.cn_text ?? "").length}
              </span>
            </div>
            <input
              type="range"
              min={1}
              max={Math.max(1, (segment.cn_text ?? "").length - 1)}
              value={splitCnPos}
              onChange={(e) => setSplitCnPos(parseInt(e.currentTarget.value, 10))}
              className="w-full accent-[color:var(--ochre)]"
              aria-label="译文拆分位置"
            />
            <div className="mt-1 text-xs grid grid-cols-2 gap-2">
              <div className="rounded bg-background/60 px-2 py-1">
                A：{(segment.cn_text ?? "").slice(0, splitCnPos) || "（空）"}
              </div>
              <div className="rounded bg-background/60 px-2 py-1">
                B：{(segment.cn_text ?? "").slice(splitCnPos) || "（空）"}
              </div>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2 text-xs">
            <label className="space-y-1">
              <span className="text-muted-foreground">A 段说话人</span>
              <select
                className="w-full rounded border border-border bg-background px-1.5 py-1"
                value={splitSpeakerA}
                onChange={(e) => setSplitSpeakerA(e.currentTarget.value)}
              >
                {availableSpeakerIds.map((sid) => (
                  <option key={sid} value={sid}>{speakerLabel(sid)}</option>
                ))}
              </select>
            </label>
            <label className="space-y-1">
              <span className="text-muted-foreground">B 段说话人</span>
              <select
                className="w-full rounded border border-border bg-background px-1.5 py-1"
                value={splitSpeakerB}
                onChange={(e) => setSplitSpeakerB(e.currentTarget.value)}
              >
                {availableSpeakerIds.map((sid) => (
                  <option key={sid} value={sid}>{speakerLabel(sid)}</option>
                ))}
              </select>
            </label>
          </div>
          <div className="flex gap-2 justify-end">
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setSplitOpen(false)}
              disabled={isSplitting}
            >
              取消
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={handleConfirmSplit}
              disabled={
                isSplitting
                || splitSourcePos <= 0
                || splitSourcePos >= (segment.source_text ?? "").length
                || splitCnPos <= 0
                || splitCnPos >= (segment.cn_text ?? "").length
                || !splitSpeakerA
                || !splitSpeakerB
              }
            >
              {isSplitting ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" />
              ) : null}
              确认拆分
            </Button>
          </div>
        </div>
      )}
      {status === "tts_dirty" && (
        // Draft preview — user can listen before accepting. `key` forces a
        // fresh <audio> element when a newer draft overwrites the file so
        // the browser doesn't cache a stale buffer from the previous version.
        <audio
          key={`draft-${segment.segment_id}-${segment.draft_wav_duration_ms ?? ""}`}
          controls
          preload="metadata"
          className="mt-2 w-full max-w-md h-9"
          src={buildDraftAudioUrl(jobId, segment.segment_id)}
        >
          您的浏览器不支持 audio 标签
        </audio>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="secondary"
          disabled={isRegenerating}
          aria-busy={isRegenerating}
          aria-label={isRegenerating ? "正在重新合成" : "重新合成该段音频"}
          onClick={() => onRegenerate(segment.segment_id)}
        >
          {isRegenerating ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5 mr-1" />
          )}
          重新合成
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={handleOpenSplit}
          disabled={
            splitOpen
            || (segment.source_text ?? "").length < 2
            || (segment.cn_text ?? "").length < 2
          }
          aria-label="拆分该段"
          title="把这段拆成两段"
        >
          <Scissors className="h-3.5 w-3.5 mr-1" />
          拆分
        </Button>
        {status === "tts_dirty" && (
          <>
            <Button
              size="sm"
              variant="outline"
              onClick={() => onAcceptDraft(segment.segment_id)}
              aria-label="接受新合成的音频"
            >
              <Check className="h-3.5 w-3.5 mr-1" />接受
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onDiscardDraft(segment.segment_id)}
              aria-label="丢弃新合成的音频，保留原音频"
            >
              <Trash2 className="h-3.5 w-3.5 mr-1" />丢弃
            </Button>
          </>
        )}
        {isSaving && (
          <span
            className="ml-2 text-xs text-muted-foreground inline-flex items-center gap-1"
            role="status"
            aria-live="polite"
          >
            <Loader2 className="h-3 w-3 animate-spin" />保存中...
          </span>
        )}
      </div>
    </article>
  )
}

function StatusChip({ status }: { status: SegmentStatus }) {
  const label: Record<SegmentStatus, string> = {
    accepted: "✓",
    text_dirty: "文字已改",
    tts_loading: "合成中",
    tts_dirty: "新版本待接受",
    tts_failed: "合成失败",
    voice_dirty: "音色已改",
  }
  const style: Record<SegmentStatus, string> = {
    accepted: "text-[color:var(--bamboo)]",
    text_dirty: "text-[color:var(--ochre)]",
    tts_loading: "text-[color:var(--cinnabar)]",
    tts_dirty: "text-[color:var(--ochre)]",
    tts_failed: "text-[color:var(--cinnabar)]",
    voice_dirty: "text-[color:var(--ochre)]",
  }
  return <span className={`font-medium ${style[status] ?? ""}`}>{label[status] ?? status}</span>
}

function formatMs(ms: number): string {
  const total = Math.floor(ms / 1000)
  const mm = Math.floor(total / 60).toString().padStart(2, "0")
  const ss = (total % 60).toString().padStart(2, "0")
  return `${mm}:${ss}`
}

// ---------------------------------------------------------------------------
// CommitModal
// ---------------------------------------------------------------------------

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
          <Button variant="ghost" size="sm" onClick={onClose}>取消</Button>
          <Button
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
