"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import Link from "next/link"
import { useParams, useRouter } from "next/navigation"
import { toast } from "sonner"
import {
  ArrowLeft,
  Check,
  Loader2,
  PlaySquare,
  RefreshCw,
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
  regenerateSegmentTts,
  regenerateAllDirtyTts,
  getRegenerateAllStatus,
  type CommitStrategy,
  type EditingSegment,
  type EditingSegmentsResponse,
  type SegmentStatus,
  type VoiceMapEntry,
} from "@/lib/api/editing"
import { getJob } from "@/lib/api/jobs"
import { ApiError } from "@/lib/api/client"
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
  const [isCommitting, setIsCommitting] = useState(false)
  const [commitModalOpen, setCommitModalOpen] = useState(false)
  const [commitStrategy, setCommitStrategy] = useState<CommitStrategy>("overwrite")
  const [copyDisplayName, setCopyDisplayName] = useState<string>("")
  const [activeTab, setActiveTab] = useState<"text" | "voice">("text")
  const [voiceMap, setVoiceMap] = useState<Record<string, VoiceMapEntry>>({})

  // ---- Bootstrap ----

  const loadData = useCallback(async (): Promise<void> => {
    if (!jobId) return
    try {
      const [nextJob, nextRes, vmRes] = await Promise.all([
        getJob(jobId),
        getEditingSegments(jobId),
        // voice_map failure shouldn't block text editing — default to empty.
        getVoiceMap(jobId).catch(() => ({ voice_map: {} })),
      ])
      setJob(nextJob)
      setResource(nextRes)
      setVoiceMap(vmRes.voice_map ?? {})
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
          try {
            await enterEditing(jobId)
          } catch (err) {
            const msg = getErrorMessage(err)
            if (!msg.includes("409")) {
              throw err
            }
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
        toast.success(`已改为说话人 ${speaker_id}；重合成时将使用其音色`)
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
    // D39 async batch: POST returns a task_id immediately; progress
    // comes from polling GET /regenerate-all-tts/status. The single
    // sonner toast gets updated in-place via its id so the user sees
    // "合成中 3/100 · 段: seg_004" → "合成中 70/100 · ..." → final summary.
    const toastId = toast.loading("正在启动批量合成…")
    const POLL_INTERVAL_MS = 1000
    const MAX_POLLS = 30 * 60  // 30 minutes; generous for 300+ segments
    try {
      const { task_id: taskId } = await regenerateAllDirtyTts(jobId)
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
    }
  }, [isBatchRegenerating, jobId, loadData])

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
    setCopyDisplayName(
      job ? `${getJobDisplayTitle(job)} · 副本 1` : "",
    )
    setCommitModalOpen(true)
  }, [job])

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
        toast.success(`副本 "${result.new_display_name}" 已创建，开始重合成`)
      } else {
        toast.success(`重合成开始 · 第 ${result.edit_generation} 次修改`)
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

  const virtualListRef = useRef<SegmentVirtualListRef>(null)
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
            <span className="ml-2 text-xs px-1.5 py-0.5 rounded-full bg-violet-500/15 text-violet-400">
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
                <span className="ml-2 text-amber-500">
                  ⚠ {forceDspSegments.length} 段时长异常（重写 2 次仍超/过短），建议修改
                </span>
              )}
              {draftDurationMismatchSegments.length > 0 && (
                <button
                  type="button"
                  className="ml-2 text-amber-500 underline decoration-dotted hover:text-amber-400 min-h-[32px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-1 rounded"
                  onClick={() =>
                    scrollToSegment(draftDurationMismatchSegments[0].seg.segment_id)
                  }
                  title="保存后新 TTS 将被 DSP 压缩/拉伸到目标时长，偏差过大时音质会明显下降。点击定位第一段。"
                >
                  ⚠ {draftDurationMismatchSegments.length} 段新 TTS 时长与目标偏差大，点击定位
                </button>
              )}
            </div>
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
                    onTextChange={handleTextChange}
                    onSpeakerChange={handleSpeakerChange}
                    onRegenerate={handleRegenerate}
                    onAcceptDraft={handleAcceptDraft}
                    onDiscardDraft={handleDiscardDraft}
                    onSeek={seekToSegment}
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
  onTextChange: (segmentId: string, cnText: string) => void
  onSpeakerChange: (segmentId: string, speakerId: string) => void
  onRegenerate: (segmentId: string) => void
  onAcceptDraft: (segmentId: string) => void
  onDiscardDraft: (segmentId: string) => void
  /** Jump video playback to this segment's start_ms (click the time
   *  label). No-op when video ref isn't ready or segment has no timing. */
  onSeek: (segmentId: string) => void
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
  onTextChange,
  onSpeakerChange,
  onRegenerate,
  onAcceptDraft,
  onDiscardDraft,
  onSeek,
}: SegmentCardProps) {
  const [localText, setLocalText] = useState(segment.cn_text ?? "")
  useEffect(() => { setLocalText(segment.cn_text ?? "") }, [segment.cn_text])

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
      ? "border-l-4 border-l-amber-500"
      : isAnomalous
      ? "border-l-4 border-l-red-500"
      : ""

  // Screen reader friendly summary for the whole segment card
  const screenReaderSummary = [
    `段落 ${index + 1}`,
    segment.speaker_id ? `说话人 ${segment.speaker_id}` : null,
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
                    {sid}
                  </option>
                ))}
              </select>
            ) : (
              <span>{segment.speaker_id}</span>
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
                : "text-amber-500"
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
      {segment.source_text && (
        <p className="text-xs text-muted-foreground mb-2">{segment.source_text}</p>
      )}
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
    accepted: "text-emerald-600",
    text_dirty: "text-amber-500",
    tts_loading: "text-cyan-500",
    tts_dirty: "text-violet-500",
    tts_failed: "text-red-500",
    voice_dirty: "text-amber-500",
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="max-w-lg w-full surface-card p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-bold">确认修改并重新生成视频</h2>
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
