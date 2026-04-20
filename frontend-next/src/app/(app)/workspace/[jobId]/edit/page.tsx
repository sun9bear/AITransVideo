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
  patchSegmentText,
  regenerateSegmentTts,
  regenerateAllDirtyTts,
  getRegenerateAllStatus,
  type CommitStrategy,
  type EditingSegment,
  type EditingSegmentsResponse,
  type SegmentStatus,
} from "@/lib/api/editing"
import { getJob } from "@/lib/api/jobs"
import { ApiError } from "@/lib/api/client"
import { getErrorMessage } from "@/lib/api/errors"
import { getJobDisplayTitle } from "@/features/jobs/presentation"
import type { JobSummary } from "@/types/jobs"

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

  // ---- Bootstrap ----

  const loadData = useCallback(async (): Promise<void> => {
    if (!jobId) return
    try {
      const [nextJob, nextRes] = await Promise.all([
        getJob(jobId),
        getEditingSegments(jobId),
      ])
      setJob(nextJob)
      setResource(nextRes)
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

  const scrollToSegment = useCallback((segmentId: string) => {
    const el = document.getElementById(`segment-card-${segmentId}`)
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" })
    }
  }, [])

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
    <div className="space-y-6 max-w-5xl mx-auto">
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

      {/* Batch actions + anomaly summary */}
      <section className="surface-card p-4 flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-0 text-sm text-muted-foreground">
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
              className="ml-2 text-amber-500 underline decoration-dotted hover:text-amber-400"
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
          className="rounded-md bg-primary/80 text-primary-foreground px-4 py-1.5 text-xs inline-flex items-center gap-1 disabled:opacity-50"
          onClick={handleBatchRegenerate}
          disabled={isBatchRegenerating || dirtyCount === 0}
          type="button"
        >
          {isBatchRegenerating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
          一键合成所有未合成段落
        </button>
      </section>

      {/* Segment list */}
      <section className="space-y-3">
        {resource.segments.map((seg, idx) => (
          <SegmentCard
            key={seg.segment_id}
            index={idx}
            segment={seg}
            status={resource.segment_status[seg.segment_id] ?? "accepted"}
            isSaving={savingSegmentIds.has(seg.segment_id)}
            isRegenerating={regeneratingSegmentIds.has(seg.segment_id)}
            onTextChange={handleTextChange}
            onRegenerate={handleRegenerate}
            onAcceptDraft={handleAcceptDraft}
            onDiscardDraft={handleDiscardDraft}
          />
        ))}
      </section>

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
  index: number
  segment: EditingSegment
  status: SegmentStatus
  isSaving: boolean
  isRegenerating: boolean
  onTextChange: (segmentId: string, cnText: string) => void
  onRegenerate: (segmentId: string) => void
  onAcceptDraft: (segmentId: string) => void
  onDiscardDraft: (segmentId: string) => void
}

function SegmentCard({
  index,
  segment,
  status,
  isSaving,
  isRegenerating,
  onTextChange,
  onRegenerate,
  onAcceptDraft,
  onDiscardDraft,
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

  return (
    <article
      id={`segment-card-${segment.segment_id}`}
      className={`surface-card p-4 ${borderClass}`}
    >
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground mb-2">
        <span className="font-mono">#{index + 1}</span>
        {timeLabel && <span>{timeLabel}</span>}
        {segment.speaker_id && <span>说话人 {segment.speaker_id}</span>}
        <StatusChip status={status} />
        {isAnomalous && (
          <span className="text-red-500">
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
      <div className="mt-2 flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="secondary"
          disabled={isRegenerating}
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
            <Button size="sm" variant="outline" onClick={() => onAcceptDraft(segment.segment_id)}>
              <Check className="h-3.5 w-3.5 mr-1" />接受
            </Button>
            <Button size="sm" variant="ghost" onClick={() => onDiscardDraft(segment.segment_id)}>
              <Trash2 className="h-3.5 w-3.5 mr-1" />丢弃
            </Button>
          </>
        )}
        {isSaving && (
          <span className="ml-2 text-xs text-muted-foreground inline-flex items-center gap-1">
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
