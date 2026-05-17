"use client"

/**
 * SplitSegmentDialog — Phase 2a (multi-cut).
 *
 * User picks 1..N cuts in source text; CN cuts auto-mirror proportionally.
 * Each resulting piece gets a speaker assignment.
 *
 * Plan refs:
 *   - §5 modal structure
 *   - §5.6 backend split_editing_segment_many + write-ahead journal
 *   - §6.4 close (Phase 1 single-cut hint removed)
 *
 * Backend endpoint (POST /jobs/{id}/segments/{sid}/split-many) wraps
 * atomic 3-file rename in a write-ahead journal; this dialog only
 * collects the user's intent.
 */

import { useEffect, useMemo, useRef, useState } from "react"
import { Loader2, RotateCcw, Plus, X } from "lucide-react"
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
  /** Phase 2a multi-cut payload. cuts strictly increasing; speaker_ids
   *  length = cuts.length + 1. */
  onSubmit(
    segmentId: string,
    body: {
      cuts: Array<{ source_index: number; cn_index: number }>
      speaker_ids: string[]
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

/** Render text with N cut bars inline. Clicking any character adds a
 *  cut just after that char; clicking × above a cut bar removes it;
 *  mousedown on a cut bar starts drag-to-reposition.
 *
 *  Layout: × badge floats ABOVE the vertical bar (absolute positioning)
 *  so it doesn't visually overlap the line. Bar itself is clickable +
 *  draggable; cursor switches to ew-resize on hover.
 */
function CutTextBlockMulti({
  text,
  cuts,
  onAddCut,
  onRemoveCut,
  onMoveCut,
}: {
  text: string
  cuts: number[]  // sorted ascending, in (0, text.length)
  onAddCut(charIndexAfter: number): void
  onRemoveCut(cutArrayIndex: number): void
  onMoveCut?(cutArrayIndex: number, newCutPos: number): void
}) {
  // Track active drag — refs avoid re-renders during mousemove storm.
  const draggingRef = useRef<number | null>(null)

  const startDrag = (cutArrayIndex: number) => (e: React.MouseEvent) => {
    if (!onMoveCut) return
    e.preventDefault()
    e.stopPropagation()
    draggingRef.current = cutArrayIndex
    const prevUserSelect = document.body.style.userSelect
    const prevCursor = document.body.style.cursor
    document.body.style.userSelect = "none"
    document.body.style.cursor = "ew-resize"

    const handleMove = (ev: MouseEvent) => {
      if (draggingRef.current === null) return
      const el = document.elementFromPoint(ev.clientX, ev.clientY) as HTMLElement | null
      if (!el) return
      // Walk up to find the nearest element with data-char-idx (the
      // char span). elementFromPoint may return a child like the
      // hover bg wrapper.
      let node: HTMLElement | null = el
      while (node && node.dataset?.charIdx === undefined) {
        node = node.parentElement
        if (!node || node === document.body) {
          node = null
          break
        }
      }
      if (!node) return
      const charIdxStr = node.dataset.charIdx
      if (charIdxStr === undefined) return
      const charIdx = parseInt(charIdxStr, 10)
      if (Number.isNaN(charIdx)) return
      onMoveCut(draggingRef.current, charIdx + 1)
    }

    const handleUp = () => {
      draggingRef.current = null
      document.body.style.userSelect = prevUserSelect
      document.body.style.cursor = prevCursor
      document.removeEventListener("mousemove", handleMove)
      document.removeEventListener("mouseup", handleUp)
    }

    document.addEventListener("mousemove", handleMove)
    document.addEventListener("mouseup", handleUp)
  }

  if (!text) {
    return (
      <div className="rounded-md border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
        （文本为空）
      </div>
    )
  }
  // Compute piece boundaries [0, c1, c2, ..., text.length]
  const boundaries = [0, ...cuts, text.length]
  return (
    // pt-4 leaves room for the × badges that sit above the cut bars.
    <div className="rounded-md border border-border bg-muted/20 pt-4 pb-3 px-3 text-sm leading-relaxed select-none">
      {boundaries.slice(0, -1).map((start, pieceIdx) => {
        const end = boundaries[pieceIdx + 1]
        const piece = text.slice(start, end)
        return (
          <span key={`piece-${pieceIdx}`}>
            {piece.split("").map((ch, j) => {
              const absIndex = start + j
              return (
                <span
                  key={`c-${absIndex}`}
                  data-char-idx={absIndex}
                  className="cursor-pointer hover:bg-primary/20 rounded-sm"
                  onClick={() => onAddCut(absIndex + 1)}
                  title={`在第 ${absIndex + 1} 字处加切点`}
                >
                  {ch}
                </span>
              )
            })}
            {/* Cut marker between this piece and the next (if not last):
             *  × button floats ABOVE the vertical bar so the bar reads
             *  cleanly. Bar is draggable (cursor: ew-resize). */}
            {pieceIdx < boundaries.length - 2 && (
              <span
                className="relative inline-block align-middle"
                style={{ width: "10px", height: "20px" }}
                aria-hidden="true"
              >
                {/* × badge — absolutely positioned, above the bar */}
                <button
                  type="button"
                  onMouseDown={(e) => e.stopPropagation()}
                  onClick={(e) => {
                    e.stopPropagation()
                    onRemoveCut(pieceIdx)
                  }}
                  className="absolute -top-3 left-1/2 -translate-x-1/2 z-10 inline-flex items-center justify-center h-4 w-4 rounded-full bg-primary text-primary-foreground hover:bg-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
                  title="删除此切点"
                  aria-label="删除此切点"
                >
                  <X className="h-2.5 w-2.5" />
                </button>
                {/* Vertical bar — drag handle (8px hit-target wraps a 2px visual line) */}
                <span
                  role="separator"
                  onMouseDown={startDrag(pieceIdx)}
                  className="absolute top-0 left-1/2 -translate-x-1/2 flex items-center justify-center w-2 h-5 cursor-ew-resize"
                  title={onMoveCut ? "拖动调整切点位置" : "切点位置"}
                >
                  <span className="block w-[2px] h-full bg-primary rounded-sm" />
                </span>
              </span>
            )}
          </span>
        )
      })}
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

  // Phase 2a state: arbitrary N cuts. Each cut has matched source+cn
  // indices. cuts[].source_index strictly increasing.
  const [cuts, setCuts] = useState<Array<{ source_index: number; cn_index: number }>>([])
  const [speakerIds, setSpeakerIds] = useState<string[]>([])
  const [isSubmitting, setIsSubmitting] = useState(false)

  // Re-seed when a new segment opens: start with one cut at midpoint.
  useEffect(() => {
    if (!segment || !open) return
    const srcMid = Math.max(1, Math.floor(sourceText.length / 2))
    const cnMid = Math.max(1, Math.floor(cnText.length / 2))
    setCuts([{ source_index: srcMid, cn_index: cnMid }])
    const fallback = availableSpeakerIds[0] ?? ""
    setSpeakerIds([
      segment.speaker_id ?? fallback,
      segment.speaker_id ?? fallback,
    ])
    setIsSubmitting(false)
  }, [segment, open, sourceText.length, cnText.length, availableSpeakerIds])

  const speakerLabel = (sid: string): string => speakerNameMap[sid] || sid

  /** Add a cut at source_index. Auto-mirror to cn proportionally.
   *  De-dups if a cut already exists at that source position. */
  const handleAddSourceCut = (sourceIndex: number) => {
    if (sourceIndex <= 0 || sourceIndex >= sourceText.length) return
    setCuts((prev) => {
      // De-dup
      if (prev.some((c) => c.source_index === sourceIndex)) return prev
      // Auto-mirror to cn
      const cnRatio = sourceText.length > 0 ? sourceIndex / sourceText.length : 0.5
      const cnIndex = Math.max(1, Math.min(Math.round(cnRatio * cnText.length), cnText.length - 1))
      // Insert + sort
      const next = [...prev, { source_index: sourceIndex, cn_index: cnIndex }]
        .sort((a, b) => a.source_index - b.source_index)
      // Reject if any consecutive duplicates emerged from the mirror
      // (rare: CN much shorter than source, two source cuts map to same CN).
      // Caller can adjust manually below the text.
      return next
    })
    setSpeakerIds((prev) => {
      const fallback = availableSpeakerIds[0] ?? ""
      return [...prev, segment?.speaker_id ?? fallback]
    })
  }

  /** Add a cut at cn_index. Auto-mirror to source proportionally. */
  const handleAddCnCut = (cnIndex: number) => {
    if (cnIndex <= 0 || cnIndex >= cnText.length) return
    setCuts((prev) => {
      if (prev.some((c) => c.cn_index === cnIndex)) return prev
      const srcRatio = cnText.length > 0 ? cnIndex / cnText.length : 0.5
      const sourceIndex = Math.max(1, Math.min(Math.round(srcRatio * sourceText.length), sourceText.length - 1))
      if (prev.some((c) => c.source_index === sourceIndex)) return prev
      return [...prev, { source_index: sourceIndex, cn_index: cnIndex }]
        .sort((a, b) => a.source_index - b.source_index)
    })
    setSpeakerIds((prev) => {
      const fallback = availableSpeakerIds[0] ?? ""
      return [...prev, segment?.speaker_id ?? fallback]
    })
  }

  /** Move a source-side cut to a new char position. Clamped between
   *  the previous and next cuts so the cuts array stays strictly
   *  monotonic without needing a re-sort (which would break the
   *  positional speaker_ids mapping). CN cut auto-mirrored
   *  proportionally + clamped to its own neighbor range. */
  const handleMoveSourceCut = (cutArrayIndex: number, newSourceIndex: number) => {
    setCuts((prev) => {
      if (cutArrayIndex < 0 || cutArrayIndex >= prev.length) return prev
      const lo = (cutArrayIndex > 0 ? prev[cutArrayIndex - 1].source_index : 0) + 1
      const hi = (cutArrayIndex < prev.length - 1 ? prev[cutArrayIndex + 1].source_index : sourceText.length) - 1
      if (lo > hi) return prev
      const clampedSource = Math.max(lo, Math.min(hi, newSourceIndex))
      const cnRatio = sourceText.length > 0 ? clampedSource / sourceText.length : 0.5
      const cnLo = (cutArrayIndex > 0 ? prev[cutArrayIndex - 1].cn_index : 0) + 1
      const cnHi = (cutArrayIndex < prev.length - 1 ? prev[cutArrayIndex + 1].cn_index : cnText.length) - 1
      const cnTarget = Math.round(cnRatio * cnText.length)
      const clampedCn = Math.max(cnLo, Math.min(cnHi, cnTarget))
      if (
        prev[cutArrayIndex].source_index === clampedSource
        && prev[cutArrayIndex].cn_index === clampedCn
      ) return prev
      const next = [...prev]
      next[cutArrayIndex] = { source_index: clampedSource, cn_index: clampedCn }
      return next
    })
  }

  /** Move a cn-side cut. Mirrors source proportionally + clamps both. */
  const handleMoveCnCut = (cutArrayIndex: number, newCnIndex: number) => {
    setCuts((prev) => {
      if (cutArrayIndex < 0 || cutArrayIndex >= prev.length) return prev
      const cnLo = (cutArrayIndex > 0 ? prev[cutArrayIndex - 1].cn_index : 0) + 1
      const cnHi = (cutArrayIndex < prev.length - 1 ? prev[cutArrayIndex + 1].cn_index : cnText.length) - 1
      if (cnLo > cnHi) return prev
      const clampedCn = Math.max(cnLo, Math.min(cnHi, newCnIndex))
      const srcRatio = cnText.length > 0 ? clampedCn / cnText.length : 0.5
      const srcLo = (cutArrayIndex > 0 ? prev[cutArrayIndex - 1].source_index : 0) + 1
      const srcHi = (cutArrayIndex < prev.length - 1 ? prev[cutArrayIndex + 1].source_index : sourceText.length) - 1
      const srcTarget = Math.round(srcRatio * sourceText.length)
      const clampedSource = Math.max(srcLo, Math.min(srcHi, srcTarget))
      if (
        prev[cutArrayIndex].source_index === clampedSource
        && prev[cutArrayIndex].cn_index === clampedCn
      ) return prev
      const next = [...prev]
      next[cutArrayIndex] = { source_index: clampedSource, cn_index: clampedCn }
      return next
    })
  }

  /** Remove cut at cutArrayIndex; also drops one corresponding speaker. */
  const handleRemoveCut = (cutArrayIndex: number) => {
    if (cuts.length <= 1) return  // keep at least 1 cut → 2 pieces
    setCuts((prev) => prev.filter((_, i) => i !== cutArrayIndex))
    setSpeakerIds((prev) => {
      // Drop the speaker AFTER the removed cut (piece cutArrayIndex+1)
      // — merges that piece into the preceding one which keeps its speaker.
      if (prev.length <= 2) return prev
      const next = [...prev]
      next.splice(cutArrayIndex + 1, 1)
      return next
    })
  }

  const handleReset = () => {
    if (!segment) return
    const srcMid = Math.max(1, Math.floor(sourceText.length / 2))
    const cnMid = Math.max(1, Math.floor(cnText.length / 2))
    setCuts([{ source_index: srcMid, cn_index: cnMid }])
    const fallback = availableSpeakerIds[0] ?? ""
    setSpeakerIds([
      segment.speaker_id ?? fallback,
      segment.speaker_id ?? fallback,
    ])
  }

  // Validity: cuts strictly monotonic in BOTH indices, in-bounds,
  // speaker_ids count = cuts.length + 1, no empty speaker.
  const valid = useMemo(() => {
    if (!segment) return false
    if (cuts.length < 1) return false
    if (speakerIds.length !== cuts.length + 1) return false
    let prevSi = 0
    let prevCi = 0
    for (const c of cuts) {
      if (!(c.source_index > prevSi && c.source_index < sourceText.length)) return false
      if (!(c.cn_index > prevCi && c.cn_index < cnText.length)) return false
      prevSi = c.source_index
      prevCi = c.cn_index
    }
    if (speakerIds.some((s) => !s || !s.trim())) return false
    return true
  }, [segment, cuts, speakerIds, sourceText.length, cnText.length])

  // Build per-piece preview from cuts.
  const pieces = useMemo(() => {
    const sourceBounds = [0, ...cuts.map((c) => c.source_index), sourceText.length]
    const cnBounds = [0, ...cuts.map((c) => c.cn_index), cnText.length]
    const startMs = segment?.start_ms
    const endMs = segment?.end_ms
    const hasTime =
      typeof startMs === "number" && typeof endMs === "number" && endMs > startMs
    return sourceBounds.slice(0, -1).map((sStart, i) => {
      const sEnd = sourceBounds[i + 1]
      const cStart = cnBounds[i]
      const cEnd = cnBounds[i + 1]
      let pieceRange = ""
      let pieceDur = ""
      if (hasTime && sourceText.length > 0) {
        const ratioStart = sStart / sourceText.length
        const ratioEnd = sEnd / sourceText.length
        const ms0 = Math.round((startMs as number) + (endMs as number - (startMs as number)) * ratioStart)
        const ms1 = Math.round((startMs as number) + (endMs as number - (startMs as number)) * ratioEnd)
        pieceRange = `${formatMs(ms0)} – ${formatMs(ms1)}`
        pieceDur = durationLabel(ms0, ms1)
      }
      return {
        num: i + 1,
        en: sourceText.slice(sStart, sEnd),
        cn: cnText.slice(cStart, cEnd),
        range: pieceRange,
        dur: pieceDur,
      }
    })
  }, [cuts, sourceText, cnText, segment?.start_ms, segment?.end_ms])

  const totalLabel = useMemo(() => {
    if (!segment) return ""
    return `${formatMs(segment.start_ms)} – ${formatMs(segment.end_ms)}`
  }, [segment])

  const handleSubmit = async () => {
    if (!segment || !valid || isSubmitting) return
    setIsSubmitting(true)
    try {
      await onSubmit(segment.segment_id, {
        cuts: cuts.map((c) => ({ source_index: c.source_index, cn_index: c.cn_index })),
        speaker_ids: speakerIds,
      })
      onClose()
    } finally {
      setIsSubmitting(false)
    }
  }

  /** Add a cut at midpoint of the longest piece (for users who can't
   *  click precisely). */
  const handleAddCutAtMidpoint = () => {
    // Find the longest current piece (by source-text length).
    const sourceBounds = [0, ...cuts.map((c) => c.source_index), sourceText.length]
    let longestStart = 0
    let longestEnd = sourceText.length
    let longestLen = 0
    for (let i = 0; i < sourceBounds.length - 1; i++) {
      const len = sourceBounds[i + 1] - sourceBounds[i]
      if (len > longestLen) {
        longestLen = len
        longestStart = sourceBounds[i]
        longestEnd = sourceBounds[i + 1]
      }
    }
    if (longestLen < 4) return  // too short to split
    const mid = Math.floor((longestStart + longestEnd) / 2)
    handleAddSourceCut(mid)
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
            {/* Source (English) — click chars to add cuts, × on a bar to remove */}
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs">
                <label className="text-muted-foreground">
                  英文原文 · 点击文字加切点 · 拖动切线微调 · 切点 × 删除
                </label>
                <span className="font-mono text-[color:var(--ochre)] tabular-nums">
                  {cuts.length} 个切点
                </span>
              </div>
              <CutTextBlockMulti
                text={sourceText}
                cuts={cuts.map((c) => c.source_index)}
                onAddCut={handleAddSourceCut}
                onRemoveCut={handleRemoveCut}
                onMoveCut={handleMoveSourceCut}
              />
            </div>

            {/* CN — independent click-to-add (also auto-mirrored when adding
             *  on source side) */}
            <div className="space-y-2">
              <div className="flex items-center justify-between text-xs">
                <label className="text-muted-foreground">
                  中文译文 · 点击文字位置加切点 · 拖动切线微调
                </label>
                <span className="text-[10px] text-muted-foreground">
                  切点跟英文自动联动
                </span>
              </div>
              <CutTextBlockMulti
                text={cnText}
                cuts={cuts.map((c) => c.cn_index)}
                onAddCut={handleAddCnCut}
                onRemoveCut={handleRemoveCut}
                onMoveCut={handleMoveCnCut}
              />
            </div>

            {/* Add-cut shortcut */}
            <div>
              <Button
                size="sm"
                variant="outline"
                onClick={handleAddCutAtMidpoint}
                className="text-xs"
              >
                <Plus className="h-3.5 w-3.5 mr-1" />
                在最长段落中点加切点
              </Button>
            </div>

            {/* Preview cards — N+1 pieces */}
            <div className="space-y-2">
              <div className="text-xs text-muted-foreground">
                将拆分为 {pieces.length} 段
              </div>
              {pieces.map((piece, pieceIdx) => (
                <div
                  key={pieceIdx}
                  className="rounded-md border border-border bg-muted/20 p-3 space-y-2"
                >
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
                      value={speakerIds[pieceIdx] ?? ""}
                      onChange={(e) => {
                        const v = e.currentTarget.value
                        setSpeakerIds((prev) => {
                          const next = [...prev]
                          next[pieceIdx] = v
                          return next
                        })
                      }}
                      aria-label={`第 ${piece.num} 段说话人`}
                    >
                      {availableSpeakerIds.map((sid) => (
                        <option key={sid} value={sid}>
                          {speakerLabel(sid)}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="text-[11px] text-muted-foreground break-words leading-relaxed">
                    {piece.en || "（空）"}
                  </div>
                  <div className="text-sm text-foreground break-words leading-relaxed">
                    {piece.cn || "（空）"}
                  </div>
                </div>
              ))}
            </div>
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
            拆分为 {Math.max(2, cuts.length + 1)} 段
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
