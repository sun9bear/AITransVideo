"use client"

/**
 * SplitSegmentDialog — Phase 1 (single-cut).
 *
 * Replaces the inline split panel that lived inside SegmentCard. Opens
 * when the user clicks 拆分 on a segment row or current-segment ops
 * panel.
 *
 * Phase 1:  splits one segment into exactly 2 halves via existing
 *           POST /jobs/{id}/segments/{sid}/split. No backend change.
 * Phase 2:  multi-cut + smart-prefill (deferred).
 *
 * Visual: closer to mockup split-ux.html Option A — the text panel
 * shows the cut as a red dashed vertical bar mid-text (not just an
 * "A: ... B: ..." rectangle), and the preview is a 2-card stack with
 * circle index + timecode + bilingual snippets + speaker dropdown.
 *
 * Plan refs: §5 (modal structure) + §5.5 (Phase 1 simplification + hint).
 */

import { useEffect, useMemo, useState } from "react"
import { Loader2, RotateCcw } from "lucide-react"
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

function durationLabel(startMs: number | undefined, endMs: number | undefined): string {
  if (startMs === undefined || endMs === undefined) return ""
  const sec = (endMs - startMs) / 1000
  return `${sec.toFixed(1)}s`
}

/** Text block with an inline red-dashed cut marker placed at `cutIndex`.
 *  Click anywhere on a character to position the cut on its boundary. */
function CutTextBlock({
  text,
  cutIndex,
  onSetCut,
}: {
  text: string
  cutIndex: number
  onSetCut(i: number): void
}) {
  if (!text) {
    return (
      <div className="rounded-md border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
        （文本为空）
      </div>
    )
  }
  const before = text.slice(0, cutIndex)
  const after = text.slice(cutIndex)
  return (
    <div className="rounded-md border border-border bg-muted/20 p-3 text-sm leading-relaxed select-none">
      <span>
        {before.split("").map((ch, i) => (
          <span
            key={`b-${i}`}
            className="cursor-pointer hover:bg-primary/20 rounded-sm"
            onClick={() => onSetCut(i + 1)}
            title={`在第 ${i + 1} 字处切分`}
          >
            {ch}
          </span>
        ))}
      </span>
      <span
        className="inline-block align-middle mx-0.5"
        aria-hidden="true"
        title="切点"
      >
        <span className="inline-block w-[2px] h-4 bg-primary align-middle" />
      </span>
      <span>
        {after.split("").map((ch, i) => (
          <span
            key={`a-${i}`}
            className="cursor-pointer hover:bg-primary/20 rounded-sm"
            onClick={() => onSetCut(cutIndex + i + 1)}
            title={`在第 ${cutIndex + i + 1} 字处切分`}
          >
            {ch}
          </span>
        ))}
      </span>
    </div>
  )
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

  // Auto-mirror CN cut position when source cut changes (proportional).
  const handleSourceCutChange = (i: number) => {
    const clamped = Math.max(1, Math.min(i, sourceText.length - 1))
    setSplitSourcePos(clamped)
    if (sourceText.length > 0 && cnText.length > 0) {
      const ratio = clamped / sourceText.length
      const mirrored = Math.max(1, Math.min(Math.round(ratio * cnText.length), cnText.length - 1))
      setSplitCnPos(mirrored)
    }
  }
  const handleCnCutChange = (i: number) => {
    setSplitCnPos(Math.max(1, Math.min(i, cnText.length - 1)))
  }

  const handleReset = () => {
    if (!segment) return
    const srcMid = Math.max(1, Math.floor(sourceText.length / 2))
    const cnMid = Math.max(1, Math.floor(cnText.length / 2))
    setSplitSourcePos(srcMid)
    setSplitCnPos(cnMid)
    const fallback = availableSpeakerIds[0] ?? ""
    setSpeakerA(segment.speaker_id ?? fallback)
    setSpeakerB(segment.speaker_id ?? fallback)
  }

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

  const totalLabel = useMemo(() => {
    if (!segment) return ""
    return `${formatMs(segment.start_ms)} – ${formatMs(segment.end_ms)}`
  }, [segment])

  // Linear time split — A gets the same source/cn ratio of the original
  // time window. Matches backend behavior of word-boundary-aligned splits.
  const splitTimeLabel = useMemo(() => {
    if (!segment || segment.start_ms === undefined || segment.end_ms === undefined) {
      return { aRange: "", aDur: "", bRange: "", bDur: "" }
    }
    const ratio = sourceText.length > 0 ? splitSourcePos / sourceText.length : 0.5
    const midMs = Math.round(segment.start_ms + (segment.end_ms - segment.start_ms) * ratio)
    return {
      aRange: `${formatMs(segment.start_ms)} – ${formatMs(midMs)}`,
      aDur: durationLabel(segment.start_ms, midMs),
      bRange: `${formatMs(midMs)} – ${formatMs(segment.end_ms)}`,
      bDur: durationLabel(midMs, segment.end_ms),
    }
  }, [segment, sourceText.length, splitSourcePos])

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
      <DialogContent className="w-[92vw] max-w-3xl lg:max-w-4xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-baseline gap-2">
            <span>拆分段落</span>
            {segment && (
              <span className="text-xs font-normal text-muted-foreground tabular-nums">
                {totalLabel}
              </span>
            )}
          </DialogTitle>
        </DialogHeader>

        {!segment ? (
          <p className="text-sm text-muted-foreground">无选中段落</p>
        ) : (
          <div className="space-y-4">
            {/* Source (English) — clickable text + slider */}
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs">
                <label className="text-muted-foreground">英文原文 · 点击文字位置设置切点</label>
                <span className="font-mono text-[color:var(--ochre)] tabular-nums">
                  {splitSourcePos} / {sourceText.length}
                </span>
              </div>
              <CutTextBlock
                text={sourceText}
                cutIndex={splitSourcePos}
                onSetCut={handleSourceCutChange}
              />
              <input
                type="range"
                min={1}
                max={Math.max(1, sourceText.length - 1)}
                value={splitSourcePos}
                onChange={(e) => handleSourceCutChange(parseInt(e.currentTarget.value, 10))}
                className="w-full accent-primary"
                aria-label="原文切点位置"
              />
            </div>

            {/* CN — same UI */}
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs">
                <label className="text-muted-foreground">中文译文 · 点击文字位置设置切点</label>
                <span className="font-mono text-[color:var(--ochre)] tabular-nums">
                  {splitCnPos} / {cnText.length}
                </span>
              </div>
              <CutTextBlock
                text={cnText}
                cutIndex={splitCnPos}
                onSetCut={handleCnCutChange}
              />
              <input
                type="range"
                min={1}
                max={Math.max(1, cnText.length - 1)}
                value={splitCnPos}
                onChange={(e) => handleCnCutChange(parseInt(e.currentTarget.value, 10))}
                className="w-full accent-primary"
                aria-label="译文切点位置"
              />
            </div>

            {/* Preview cards — circle index + time range + bilingual + speaker dropdown */}
            <div className="space-y-2">
              <div className="text-xs text-muted-foreground">将拆分为 2 段</div>
              {[
                {
                  num: 1,
                  range: splitTimeLabel.aRange,
                  dur: splitTimeLabel.aDur,
                  en: aSrc,
                  cn: aCn,
                  speaker: speakerA,
                  setSpeaker: setSpeakerA,
                },
                {
                  num: 2,
                  range: splitTimeLabel.bRange,
                  dur: splitTimeLabel.bDur,
                  en: bSrc,
                  cn: bCn,
                  speaker: speakerB,
                  setSpeaker: setSpeakerB,
                },
              ].map((piece) => (
                <div
                  key={piece.num}
                  className="rounded-md border border-border bg-muted/20 p-3 space-y-2"
                >
                  {/* Header row: circle + timecode + duration + speaker (all on one line).
                   *  User feedback 2026-05-17 #3: "说话人和时间、时长占一行" so the
                   *  bilingual text below gets full row width. */}
                  <div className="flex items-center gap-3">
                    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground text-xs font-semibold">
                      {piece.num}
                    </div>
                    <div className="flex-1 min-w-0 text-[11px] tabular-nums text-muted-foreground">
                      {piece.range}
                      {piece.dur && <span className="ml-1">({piece.dur})</span>}
                    </div>
                    <select
                      className="text-xs rounded border border-border bg-background px-2 py-1 text-foreground max-w-[180px]"
                      value={piece.speaker}
                      onChange={(e) => piece.setSpeaker(e.currentTarget.value)}
                      aria-label={`第 ${piece.num} 段说话人`}
                    >
                      {availableSpeakerIds.map((sid) => (
                        <option key={sid} value={sid}>
                          {speakerLabel(sid)}
                        </option>
                      ))}
                    </select>
                  </div>

                  {/* Body: EN full-width, then CN full-width. */}
                  <div className="text-[11px] text-muted-foreground break-words leading-relaxed">
                    {piece.en || "（空）"}
                  </div>
                  <div className="text-sm text-foreground break-words leading-relaxed">
                    {piece.cn || "（空）"}
                  </div>
                </div>
              ))}
            </div>

            {/* Phase 1 hint */}
            <p className="text-[10.5px] text-muted-foreground bg-muted/30 rounded px-2 py-1.5 border border-border/40">
              当前支持拆分为 2 段。一段内有多个说话人时，多段一次性拆分将在下个版本支持。
            </p>
          </div>
        )}

        <DialogFooter className="gap-2">
          <Button
            variant="ghost"
            onClick={handleReset}
            disabled={isSubmitting || !segment}
            className="mr-auto text-xs"
            aria-label="重置切点"
          >
            <RotateCcw className="h-3.5 w-3.5 mr-1" />
            重置切点
          </Button>
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
