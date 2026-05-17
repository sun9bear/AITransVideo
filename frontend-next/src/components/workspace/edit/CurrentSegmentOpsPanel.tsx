"use client"

/**
 * CurrentSegmentOpsPanel — Phase 1.
 *
 * Sits below the sticky video on the left column. Shows the active
 * segment's metadata + actions (regen / draft preview / accept / discard).
 * activeSegmentId comes from usePlayerSegmentSync at the page level.
 *
 * Plan refs:
 *   - §3.2 (content matrix by status)
 *   - §8a3.1 (callback sharing — handlers must be the SAME refs passed
 *     to SegmentRow; do NOT define parallel implementations here)
 *
 * State branches (plan §3.2):
 *   null              → placeholder 「点选段落开始修改」
 *   accepted          → time/speaker + 试听原音
 *   text_dirty / voice_dirty → + 待合成 button
 *   tts_loading       → spinner + 「合成中…」
 *   tts_dirty         → draft <audio> + 试听原音 + 接受 / 丢弃
 *   tts_failed        → error message + 重试合成
 */

import { useState } from "react"
import {
  Check,
  Loader2,
  Play,
  RefreshCw,
  Scissors,
  Trash2,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { buildDraftAudioUrl } from "@/lib/api/downloads"
import type { EditingSegment, SegmentStatus } from "@/lib/api/editing"

function formatMs(ms: number): string {
  const total = Math.floor(ms / 1000)
  const m = Math.floor(total / 60).toString().padStart(2, "0")
  const s = (total % 60).toString().padStart(2, "0")
  return `${m}:${s}`
}

export interface CurrentSegmentOpsPanelProps {
  jobId: string
  /** The currently-active segment (from usePlayerSegmentSync). Null when
   *  no segment is selected. */
  segment: EditingSegment | null
  status: SegmentStatus | null
  isRegenerating: boolean
  /** True if the page has a pending patch text/speaker save for this
   *  segment in flight. Used to gate split (Codex round-7 P2 #2:
   *  splitting during a patch save races the JSON write). */
  isSaving: boolean
  isBatchRegenerating: boolean
  /** Friendly name for the segment's speaker_id, resolved at page level. */
  speakerName: string | null
  onRegenerate(segmentId: string): void
  onAcceptDraft(segmentId: string): void
  onDiscardDraft(segmentId: string): void
  onSplit(segmentId: string): void
  onPreviewSource(segmentId: string): Promise<string | null>
}

export function CurrentSegmentOpsPanel({
  jobId,
  segment,
  status,
  isRegenerating,
  isSaving,
  isBatchRegenerating,
  speakerName,
  onRegenerate,
  onAcceptDraft,
  onDiscardDraft,
  onSplit,
  onPreviewSource,
}: CurrentSegmentOpsPanelProps) {
  const [sourceAudioUrl, setSourceAudioUrl] = useState<string | null>(null)
  const [isFetchingSource, setIsFetchingSource] = useState(false)

  if (!segment || !status) {
    return (
      <section
        className="rounded-md border border-border bg-card/40 p-3 text-xs text-muted-foreground"
        aria-label="当前段操作区"
      >
        点选段落开始修改
      </section>
    )
  }

  const handlePlaySource = async () => {
    if (sourceAudioUrl) return  // <audio controls> 已渲染，用户自己点
    setIsFetchingSource(true)
    try {
      const url = await onPreviewSource(segment.segment_id)
      if (url) setSourceAudioUrl(url)
    } finally {
      setIsFetchingSource(false)
    }
  }

  const buttonsDisabled = isBatchRegenerating

  const timeLabel =
    segment.start_ms !== undefined && segment.end_ms !== undefined
      ? `${formatMs(segment.start_ms)} – ${formatMs(segment.end_ms)}`
      : ""

  const draftDuration =
    typeof segment.draft_wav_duration_ms === "number"
      ? `${(segment.draft_wav_duration_ms / 1000).toFixed(1)}s`
      : ""

  return (
    <section
      className="rounded-md border border-border bg-card p-3 space-y-2 text-xs"
      aria-label="当前段操作区"
    >
      {/* Header — time + speaker */}
      <div className="flex items-baseline gap-2 text-foreground">
        <span className="font-semibold tabular-nums">{timeLabel}</span>
        {speakerName && (
          <span className="text-muted-foreground">· {speakerName}</span>
        )}
      </div>

      {/* Failed status — error message */}
      {status === "tts_failed" && (
        <p className="text-[11px] text-destructive">⚠ 上次合成失败</p>
      )}

      {/* Loading status */}
      {(status === "tts_loading" || isRegenerating) && (
        <p className="inline-flex items-center gap-1 text-[11px] text-[color:var(--ochre)]">
          <Loader2 className="h-3 w-3 animate-spin" />
          合成中…
        </p>
      )}

      {/* Draft inline audio (tts_dirty) */}
      {status === "tts_dirty" && (
        <div className="space-y-1">
          <p className="text-[11px] text-[color:var(--ochre)]">
            新草稿{draftDuration ? ` · ${draftDuration}` : ""}
          </p>
          <audio
            key={`current-draft-${segment.segment_id}-${segment.draft_wav_duration_ms ?? ""}`}
            controls
            preload="metadata"
            className="w-full h-8"
            src={buildDraftAudioUrl(jobId, segment.segment_id)}
          />
          <div className="flex gap-1.5">
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onDiscardDraft(segment.segment_id)}
              disabled={buttonsDisabled}
              className="flex-1 h-7 text-[11px]"
            >
              <Trash2 className="h-3 w-3 mr-1" />
              丢弃
            </Button>
            <Button
              size="sm"
              variant="default"
              onClick={() => onAcceptDraft(segment.segment_id)}
              disabled={buttonsDisabled}
              className="flex-1 h-7 text-[11px] bg-[color:var(--ochre)] text-primary-foreground hover:bg-[color:var(--ochre)]/90"
            >
              <Check className="h-3 w-3 mr-1" />
              接受
            </Button>
          </div>
        </div>
      )}

      {/* Source audio preview — collapsible */}
      <div className="space-y-1">
        {!sourceAudioUrl ? (
          <Button
            size="sm"
            variant="outline"
            onClick={handlePlaySource}
            disabled={isFetchingSource || buttonsDisabled}
            className="w-full h-7 text-[11px]"
          >
            {isFetchingSource ? (
              <Loader2 className="h-3 w-3 animate-spin mr-1" />
            ) : (
              <Play className="h-3 w-3 mr-1" />
            )}
            试听原音
          </Button>
        ) : (
          <audio
            controls
            preload="metadata"
            className="w-full h-8"
            src={sourceAudioUrl}
          />
        )}
      </div>

      {/* Bottom action row — split + regen */}
      <div className="flex gap-1.5">
        <Button
          size="sm"
          variant="outline"
          onClick={() => onSplit(segment.segment_id)}
          disabled={
            buttonsDisabled
            || isRegenerating
            || status === "tts_loading"
            || isSaving
            || (segment.source_text ?? "").length < 2
            || (segment.cn_text ?? "").length < 2
          }
          title={
            isRegenerating || status === "tts_loading"
              ? "该段正在合成，拆分不可用（避免 orphan draft）"
              : isSaving
                ? "正在保存编辑，请稍候"
                : "把这段拆成两段"
          }
          className="flex-1 h-7 text-[11px]"
        >
          <Scissors className="h-3 w-3 mr-1" />
          拆分
        </Button>
        {status !== "tts_dirty" && (
          <Button
            size="sm"
            variant={
              status === "text_dirty" || status === "voice_dirty"
                ? "default"
                : status === "tts_failed"
                  ? "outline"
                  : "outline"
            }
            onClick={() => onRegenerate(segment.segment_id)}
            disabled={isRegenerating || status === "tts_loading" || buttonsDisabled}
            className={`flex-1 h-7 text-[11px] ${
              status === "tts_failed" ? "border-destructive text-destructive" : ""
            }`}
          >
            {(isRegenerating || status === "tts_loading") ? (
              <Loader2 className="h-3 w-3 animate-spin mr-1" />
            ) : (
              <RefreshCw className="h-3 w-3 mr-1" />
            )}
            {status === "text_dirty" || status === "voice_dirty"
              ? "待合成"
              : status === "tts_failed"
                ? "重试合成"
                : "重合成"}
          </Button>
        )}
      </div>
    </section>
  )
}
