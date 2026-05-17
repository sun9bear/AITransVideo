"use client"

/**
 * SplitSegmentDialog — Phase 1 (single-cut).
 *
 * Replaces the inline split panel that used to live inside SegmentCard.
 * Opens when the user clicks the "拆分" button on a segment row or the
 * current-segment ops panel.
 *
 * Phase 1:  maxCuts = 1  →  splits one segment into exactly 2 halves.
 *           Reuses existing backend POST /jobs/{id}/segments/{sid}/split.
 * Phase 2:  maxCuts undefined  →  N cuts → N+1 segments via split-many.
 *           Smart-prefill + speaker-aware (deferred).
 *
 * Plan refs: §5 (modal structure) + §5.5 (Phase 1 simplification + hint).
 */

import { useEffect, useMemo, useState } from "react"
import { Loader2 } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import type { EditingSegment } from "@/lib/api/editing"

export interface SplitSegmentDialogProps {
  open: boolean
  segment: EditingSegment | null
  availableSpeakerIds: string[]
  speakerNameMap: Record<string, string>
  onClose(): void
  onSubmit(
    segmentId: string,
    body: {
      split_source_index: number
      split_cn_index: number
      speaker_a: string
      speaker_b: string
    },
  ): Promise<void> | void
}

function formatMs(ms: number | undefined): string {
  if (ms === undefined) return ""
  const total = Math.floor(ms / 1000)
  const m = Math.floor(total / 60).toString().padStart(2, "0")
  const s = (total % 60).toString().padStart(2, "0")
  return `${m}:${s}`
}

export function SplitSegmentDialog({
  open,
  segment,
  availableSpeakerIds,
  speakerNameMap,
  onClose,
  onSubmit,
}: SplitSegmentDialogProps) {
  const sourceText = segment?.source_text ?? ""
  const cnText = segment?.cn_text ?? ""

  const [splitSourcePos, setSplitSourcePos] = useState(0)
  const [splitCnPos, setSplitCnPos] = useState(0)
  const [speakerA, setSpeakerA] = useState("")
  const [speakerB, setSpeakerB] = useState("")
  const [isSubmitting, setIsSubmitting] = useState(false)

  // Re-seed when a new segment opens.
  useEffect(() => {
    if (!segment || !open) return
    const srcMid = Math.max(1, Math.floor(sourceText.length / 2))
    const cnMid = Math.max(1, Math.floor(cnText.length / 2))
    setSplitSourcePos(srcMid)
    setSplitCnPos(cnMid)
    const fallback = availableSpeakerIds[0] ?? ""
    setSpeakerA(segment.speaker_id ?? fallback)
    setSpeakerB(segment.speaker_id ?? fallback)
    setIsSubmitting(false)
  }, [segment, open, sourceText.length, cnText.length, availableSpeakerIds])

  const speakerLabel = (sid: string): string => speakerNameMap[sid] || sid

  const valid =
    !!segment
    && splitSourcePos > 0
    && splitSourcePos < sourceText.length
    && splitCnPos > 0
    && splitCnPos < cnText.length
    && !!speakerA
    && !!speakerB

  const aSrc = sourceText.slice(0, splitSourcePos)
  const bSrc = sourceText.slice(splitSourcePos)
  const aCn = cnText.slice(0, splitCnPos)
  const bCn = cnText.slice(splitCnPos)

  const timeLabel = useMemo(() => {
    if (!segment) return ""
    return `${formatMs(segment.start_ms)} – ${formatMs(segment.end_ms)}`
  }, [segment])

  const handleSubmit = async () => {
    if (!segment || !valid || isSubmitting) return
    setIsSubmitting(true)
    try {
      await onSubmit(segment.segment_id, {
        split_source_index: splitSourcePos,
        split_cn_index: splitCnPos,
        speaker_a: speakerA,
        speaker_b: speakerB,
      })
      onClose()
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            拆分段落
            {segment && (
              <span className="ml-2 text-xs font-normal text-muted-foreground tabular-nums">
                {timeLabel}
              </span>
            )}
          </DialogTitle>
        </DialogHeader>

        {!segment ? (
          <p className="text-sm text-muted-foreground">无选中段落</p>
        ) : (
          <div className="space-y-3">
            {/* Phase 2 placeholder: no smart-prefill banner */}

            {/* Source (English) cut picker */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-xs">
                <label className="text-muted-foreground">原文切点</label>
                <span className="font-mono text-[color:var(--ochre)] tabular-nums">
                  {splitSourcePos} / {sourceText.length}
                </span>
              </div>
              <input
                type="range"
                min={1}
                max={Math.max(1, sourceText.length - 1)}
                value={splitSourcePos}
                onChange={(e) => setSplitSourcePos(parseInt(e.currentTarget.value, 10))}
                className="w-full accent-[color:var(--ochre)]"
                aria-label="原文拆分位置"
              />
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded bg-muted/40 px-2 py-1 text-muted-foreground break-words">
                  A：{aSrc || "（空）"}
                </div>
                <div className="rounded bg-muted/40 px-2 py-1 text-muted-foreground break-words">
                  B：{bSrc || "（空）"}
                </div>
              </div>
            </div>

            {/* CN translation cut picker */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-xs">
                <label className="text-muted-foreground">译文切点</label>
                <span className="font-mono text-[color:var(--ochre)] tabular-nums">
                  {splitCnPos} / {cnText.length}
                </span>
              </div>
              <input
                type="range"
                min={1}
                max={Math.max(1, cnText.length - 1)}
                value={splitCnPos}
                onChange={(e) => setSplitCnPos(parseInt(e.currentTarget.value, 10))}
                className="w-full accent-[color:var(--ochre)]"
                aria-label="译文拆分位置"
              />
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded bg-muted/40 px-2 py-1 break-words">
                  A：{aCn || "（空）"}
                </div>
                <div className="rounded bg-muted/40 px-2 py-1 break-words">
                  B：{bCn || "（空）"}
                </div>
              </div>
            </div>

            {/* Speakers per half */}
            <div className="grid grid-cols-2 gap-3 text-xs">
              <label className="space-y-1">
                <span className="text-muted-foreground">A 段说话人</span>
                <select
                  className="w-full rounded border border-border bg-background px-2 py-1.5 text-foreground"
                  value={speakerA}
                  onChange={(e) => setSpeakerA(e.currentTarget.value)}
                >
                  {availableSpeakerIds.map((sid) => (
                    <option key={sid} value={sid}>
                      {speakerLabel(sid)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="space-y-1">
                <span className="text-muted-foreground">B 段说话人</span>
                <select
                  className="w-full rounded border border-border bg-background px-2 py-1.5 text-foreground"
                  value={speakerB}
                  onChange={(e) => setSpeakerB(e.currentTarget.value)}
                >
                  {availableSpeakerIds.map((sid) => (
                    <option key={sid} value={sid}>
                      {speakerLabel(sid)}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            {/* Phase 1 hint about coming Phase 2 multi-split */}
            <p className="text-[10.5px] text-muted-foreground bg-muted/30 rounded px-2 py-1.5 border border-border/40">
              当前支持拆分为 2 段。一段内有多个说话人时，多段一次性拆分将在下个版本支持。
            </p>
          </div>
        )}

        <DialogFooter>
          <Button
            variant="ghost"
            onClick={onClose}
            disabled={isSubmitting}
          >
            取消
          </Button>
          <Button
            variant="default"
            onClick={handleSubmit}
            disabled={!valid || isSubmitting}
          >
            {isSubmitting && <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" />}
            拆分为 2 段
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
