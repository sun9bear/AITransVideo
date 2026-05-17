"use client"

/**
 * SegmentRow — Phase 1 redesign of the per-segment list item.
 *
 * Visual contract (plan 2026-05-17 §4, §6.1, mockup row-density-v4):
 *   grid 100px | 1fr | 230px
 *   left col:  [#] [time]  →  [说话人 ▾]   (stacked)
 *   middle:    EN muted (top) + CN foreground (below) + optional draft
 *              audio panel + optional error/warning rows
 *   right col: [拆分] [重合成] (always visible; regen button文案/颜色
 *              by 5-state mapping)
 *
 * Active row: border-l-2 border-primary + bg-primary/[0.06]
 * No hex color literals — Task 9 guard scans for them. Use tokens.
 *
 * Logic invariants preserved from old SegmentCard:
 *   - localText / localSource local buffers with sync effect
 *   - speaker dropdown with "+ 新增说话人..." sentinel
 *   - on-blur save (text + source)
 *   - lazy source-audio preview
 *   - tts_length_guidance + draft mismatch + force_dsp warnings
 *   - draft audio playback (tts_dirty)
 *   - isSaving spinner indicator
 *
 * Logic moved out (delegated):
 *   - inline split panel — now opened via onSplit(segmentId) → parent
 *     opens SplitSegmentDialog (plan §5 + Task 6)
 */

import { useEffect, useMemo, useRef, useState } from "react"
import { Check, ChevronDown, Loader2, Play, RefreshCw, Scissors, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { buildDraftAudioUrl } from "@/lib/api/downloads"
import type { EditingSegment, EditingSpeaker, SegmentStatus } from "@/lib/api/editing"

// ---------- helpers ----------

function formatMs(ms: number): string {
  const totalSec = Math.floor(ms / 1000)
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  return `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`
}

function formatDurationSeconds(ms: number | null | undefined): string {
  if (typeof ms !== "number") return ""
  return `${(ms / 1000).toFixed(1)}s`
}

function isPreTtsLengthWarningStatus(status: SegmentStatus): boolean {
  return status === "text_dirty" || status === "voice_dirty" || status === "tts_failed"
}

function hasTtsLengthGuidanceWarning(seg: EditingSegment, status: SegmentStatus): boolean {
  const g = seg.tts_length_guidance
  if (!g) return false
  if (!isPreTtsLengthWarningStatus(status)) return false
  return g.severity !== null
}

// ---------- props ----------

export interface SegmentRowProps {
  jobId: string
  index: number
  segment: EditingSegment
  status: SegmentStatus
  isSaving: boolean
  isRegenerating: boolean
  /** True when the player is currently inside [start_ms, end_ms]. */
  isActive: boolean
  /** True when the page-level "一键合成 N 段" batch task is running.
   *  Single-segment buttons are disabled with explanatory tooltip
   *  (plan §13 "已决开放问题"). */
  isBatchRegenerating: boolean
  availableSpeakerIds: string[]
  editingSpeakers: EditingSpeaker[]
  speakerNameMap: Record<string, string>
  onTextChange(segmentId: string, cnText: string): void
  onSourceTextChange(segmentId: string, sourceText: string): void
  onSpeakerChange(segmentId: string, speakerId: string): void
  onRequestCreateSpeaker(): void
  onRegenerate(segmentId: string): void
  onAcceptDraft(segmentId: string): void
  onDiscardDraft(segmentId: string): void
  onSeek(segmentId: string): void
  /** Phase 1: opens the SplitSegmentDialog at the page level. The dialog
   *  collects cut positions and speaker assignments, then calls the
   *  page's split handler. */
  onSplit(segmentId: string): void
  onPreviewSource(segmentId: string): Promise<string | null>
}

// ---------- component ----------

export function SegmentRow({
  jobId,
  index,
  segment,
  status,
  isSaving,
  isRegenerating,
  isActive,
  isBatchRegenerating,
  availableSpeakerIds,
  editingSpeakers,
  speakerNameMap,
  onTextChange,
  onSourceTextChange,
  onSpeakerChange,
  onRequestCreateSpeaker,
  onRegenerate,
  onAcceptDraft,
  onDiscardDraft,
  onSeek,
  onSplit,
  onPreviewSource,
}: SegmentRowProps) {
  // ---- speaker label resolution ----
  const editingSpeakerNameMap = useMemo(() => {
    const m: Record<string, string> = {}
    for (const sp of editingSpeakers) {
      if (sp.speaker_id) m[sp.speaker_id] = sp.display_name || sp.speaker_id
    }
    return m
  }, [editingSpeakers])

  const speakerLabel = (sid: string | undefined): string => {
    if (!sid) return ""
    return speakerNameMap[sid] || editingSpeakerNameMap[sid] || sid
  }

  const dropdownSpeakerIds = useMemo(() => {
    const seen = new Set(availableSpeakerIds)
    const extras: string[] = []
    for (const sp of editingSpeakers) {
      if (sp.speaker_id && !seen.has(sp.speaker_id)) {
        seen.add(sp.speaker_id)
        extras.push(sp.speaker_id)
      }
    }
    return extras.length === 0 ? availableSpeakerIds : [...availableSpeakerIds, ...extras]
  }, [availableSpeakerIds, editingSpeakers])

  // ---- local text buffers ----
  const [localText, setLocalText] = useState(segment.cn_text ?? "")
  useEffect(() => {
    setLocalText(segment.cn_text ?? "")
  }, [segment.cn_text])

  const [localSource, setLocalSource] = useState(segment.source_text ?? "")
  useEffect(() => {
    setLocalSource(segment.source_text ?? "")
  }, [segment.source_text])

  // ---- source audio preview (lazy) ----
  const [sourceAudioUrl, setSourceAudioUrl] = useState<string | null>(null)
  const [isFetchingSource, setIsFetchingSource] = useState(false)
  const sourceAudioRef = useRef<HTMLAudioElement | null>(null)

  const handlePlaySource = async () => {
    if (sourceAudioUrl) {
      try {
        const el = sourceAudioRef.current
        if (el) {
          el.currentTime = 0
          await el.play()
        }
      } catch {
        // autoplay policy may block; <audio controls> below is fallback
      }
      return
    }
    setIsFetchingSource(true)
    try {
      const url = await onPreviewSource(segment.segment_id)
      if (url) {
        setSourceAudioUrl(url)
        setTimeout(() => {
          sourceAudioRef.current?.play().catch(() => {})
        }, 50)
      }
    } finally {
      setIsFetchingSource(false)
    }
  }

  // ---- derived warning flags ----
  const isAnomalous = segment.alignment_method === "force_dsp"

  const target =
    typeof segment.target_duration_ms === "number" ? segment.target_duration_ms : null
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

  const ttsLengthGuidance = segment.tts_length_guidance
  const hasTtsLengthWarning = hasTtsLengthGuidanceWarning(segment, status)
  const ttsLengthWarningSeverity = ttsLengthGuidance?.severity ?? null
  const ttsLengthSuggestedRange =
    typeof ttsLengthGuidance?.suggested_min_chars === "number"
    && typeof ttsLengthGuidance?.suggested_max_chars === "number"
      ? `${ttsLengthGuidance.suggested_min_chars}-${ttsLengthGuidance.suggested_max_chars}`
      : null

  const timeLabel =
    segment.start_ms !== undefined ? formatMs(segment.start_ms) : ""

  // ---- regen button state mapping (plan §6.1) ----
  type RegenVisual = {
    label: string
    className: string
    showSpinner: boolean
    disabled: boolean
  }
  const regenVisual: RegenVisual = (() => {
    if (isRegenerating || status === "tts_loading") {
      return {
        label: "合成中…",
        className:
          "bg-[color:var(--ochre)]/12 text-[color:var(--ochre)] border-[color:var(--ochre)]/40",
        showSpinner: true,
        disabled: true,
      }
    }
    if (status === "tts_dirty") {
      return {
        label: "草稿待审 ↓",
        className:
          "bg-[color:var(--ochre)]/12 text-[color:var(--ochre)] border-[color:var(--ochre)]/40",
        showSpinner: false,
        disabled: false,
      }
    }
    if (status === "tts_failed") {
      return {
        label: "重试合成",
        className: "border-destructive text-destructive bg-transparent",
        showSpinner: false,
        disabled: false,
      }
    }
    if (status === "text_dirty" || status === "voice_dirty") {
      const dur = formatDurationSeconds(segment.target_duration_ms)
      return {
        label: dur ? `待合成（${dur}）` : "待合成",
        className:
          "bg-primary text-primary-foreground border-primary font-medium",
        showSpinner: false,
        disabled: false,
      }
    }
    // accepted
    return {
      label: "重合成",
      className: "border-border text-muted-foreground bg-transparent",
      showSpinner: false,
      disabled: false,
    }
  })()

  // Both action buttons disable during page-level batch (plan §13).
  const buttonsDisabled = isBatchRegenerating

  // Screen reader summary
  const screenReaderSummary = [
    `段落 ${index + 1}`,
    segment.speaker_id ? `说话人 ${speakerLabel(segment.speaker_id)}` : null,
    timeLabel || null,
    status === "text_dirty" || status === "voice_dirty" ? "待合成" : null,
    status === "tts_loading" ? "合成中" : null,
    status === "tts_dirty" ? "草稿待审" : null,
    status === "tts_failed" ? "合成失败" : null,
  ]
    .filter(Boolean)
    .join("，")

  // ---- render ----
  return (
    <article
      id={`segment-row-${segment.segment_id}`}
      aria-label={screenReaderSummary}
      aria-current={isActive ? "true" : undefined}
      role="listitem"
      className={[
        "grid grid-cols-[100px_1fr_230px] gap-3 py-2.5 px-3 border-b border-border last:border-b-0",
        isActive
          ? "border-l-2 border-l-primary -ml-[2px] pl-[14px] bg-primary/[0.06]"
          : "border-l-2 border-l-transparent -ml-[2px] pl-[14px]",
      ].join(" ")}
    >
      {/* ── Left col: index + time (horizontal) + speaker dropdown (below) ── */}
      <div className="flex flex-col gap-1 min-w-0">
        <div className="flex items-baseline gap-1.5 tabular-nums">
          <span className="text-[11px] font-semibold text-foreground">
            {String(index + 1).padStart(2, "0")}
          </span>
          {timeLabel && (
            <button
              type="button"
              className="text-[10px] text-muted-foreground hover:text-foreground underline decoration-dotted underline-offset-2 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
              onClick={() => onSeek(segment.segment_id)}
              title="跳转视频到该段起点"
              aria-label={`跳转视频到 ${timeLabel}`}
            >
              {timeLabel}
            </button>
          )}
        </div>
        {segment.speaker_id && (
          dropdownSpeakerIds.length > 1 ? (
            <div className="relative inline-flex items-center">
              <select
                className="appearance-none w-full text-[10px] text-foreground bg-background/60 border border-border rounded px-1.5 py-0.5 pr-5 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
                value={segment.speaker_id}
                disabled={isSaving || buttonsDisabled}
                onChange={(e) => {
                  const next = e.currentTarget.value
                  if (next === "__create__") {
                    e.currentTarget.value = segment.speaker_id ?? ""
                    onRequestCreateSpeaker()
                    return
                  }
                  if (next && next !== segment.speaker_id) {
                    onSpeakerChange(segment.segment_id, next)
                  }
                }}
                title="改说话人归属：重合成时自动换成新说话人的音色"
                aria-label="修改该段说话人归属"
              >
                {dropdownSpeakerIds.map((sid) => (
                  <option key={sid} value={sid}>
                    {speakerLabel(sid)}
                  </option>
                ))}
                <option value="__create__">+ 新增说话人…</option>
              </select>
              <ChevronDown className="absolute right-1 h-3 w-3 text-muted-foreground pointer-events-none" />
            </div>
          ) : (
            <span className="text-[10px] text-foreground inline-flex items-center gap-1">
              <span className="bg-background/60 border border-border rounded px-1.5 py-0.5">
                {speakerLabel(segment.speaker_id)}
              </span>
              <button
                type="button"
                onClick={onRequestCreateSpeaker}
                className="text-[10px] text-primary hover:underline rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
              >
                + 新增
              </button>
            </span>
          )
        )}
      </div>

      {/* ── Middle col: bilingual text (EN top muted, CN below) + draft panel + warnings ── */}
      <div className="min-w-0 space-y-1">
        {/* English source — inline editable */}
        <textarea
          className="w-full text-[10.5px] leading-snug text-muted-foreground font-sans bg-transparent border-0 resize-none p-0 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary/40 focus-visible:ring-offset-0 rounded"
          rows={Math.max(1, Math.ceil((localSource.length || 1) / 80))}
          value={localSource}
          disabled={buttonsDisabled}
          onChange={(e) => setLocalSource(e.currentTarget.value)}
          onBlur={() => {
            if (localSource !== (segment.source_text ?? "")) {
              onSourceTextChange(segment.segment_id, localSource)
            }
          }}
          aria-label="英文原文"
        />

        {/* Chinese translation — primary edit target */}
        <textarea
          className="w-full text-[12px] leading-relaxed text-foreground bg-transparent border-0 resize-none p-0 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary/40 focus-visible:ring-offset-0 rounded"
          rows={Math.max(1, Math.ceil((localText.length || 1) / 40))}
          value={localText}
          disabled={buttonsDisabled}
          onChange={(e) => setLocalText(e.currentTarget.value)}
          onBlur={() => {
            if (localText !== (segment.cn_text ?? "")) {
              onTextChange(segment.segment_id, localText)
            }
          }}
          aria-label="中文译文"
        />

        {/* Pre-TTS length warning (plan §6.1 + existing post-edit guidance) */}
        {hasTtsLengthWarning && ttsLengthGuidance && (
          <div
            className={
              ttsLengthWarningSeverity === "severe"
                ? "text-[10px] text-destructive border-l-2 border-destructive/50 pl-2"
                : "text-[10px] text-[color:var(--ochre)] border-l-2 border-[color:var(--ochre)]/50 pl-2"
            }
            title="合成前估算提示，不阻止重新合成"
          >
            当前 {ttsLengthGuidance.current_chars} 字
            {ttsLengthSuggestedRange ? `（建议 ${ttsLengthSuggestedRange}）` : ""}
            · 预计 {formatDurationSeconds(ttsLengthGuidance.estimated_duration_ms)}
            {" / "}
            目标 {formatDurationSeconds(ttsLengthGuidance.target_duration_ms)}
          </div>
        )}

        {/* Force-DSP / draft-mismatch indicator — compact one-liner */}
        {(isAnomalous || hasDraftMismatch) && (
          <div
            className={
              draftMismatchSeverity === "severe" || isAnomalous
                ? "text-[10px] text-destructive"
                : "text-[10px] text-[color:var(--ochre)]"
            }
          >
            {isAnomalous && (
              <span>
                ⚠ 时长异常
                {segment.duration_diff_ratio !== undefined && (
                  <span className="ml-1">（{Math.round(segment.duration_diff_ratio * 100)}%）</span>
                )}
              </span>
            )}
            {hasDraftMismatch && draftRatio !== null && target !== null && draft !== null && (
              <span className={isAnomalous ? "ml-2" : ""}>
                ⚠ 新 TTS {(draft / 1000).toFixed(1)}s / 目标 {(target / 1000).toFixed(1)}s
                （{draftRatio > 1 ? "+" : ""}{((draftRatio - 1) * 100).toFixed(0)}%）
              </span>
            )}
          </div>
        )}

        {/* Source audio inline player (after first preview fetch) */}
        {sourceAudioUrl && (
          <audio
            ref={sourceAudioRef}
            key={`src-${segment.segment_id}`}
            controls
            preload="metadata"
            className="mt-1 w-full max-w-md h-7"
            src={sourceAudioUrl}
          />
        )}

        {/* Draft panel — appears inline when tts_dirty. Compact: play + duration delta + discard/accept */}
        {status === "tts_dirty" && (
          <div className="mt-1 flex items-center gap-2 text-[10.5px] border-l-2 border-[color:var(--ochre)] bg-[color:var(--ochre)]/8 rounded-r pl-2 pr-2 py-1.5">
            <audio
              key={`draft-${segment.segment_id}-${segment.draft_wav_duration_ms ?? ""}`}
              controls
              preload="metadata"
              className="h-7 flex-shrink-0 max-w-[200px]"
              src={buildDraftAudioUrl(jobId, segment.segment_id)}
            />
            <span className="text-[10px] text-muted-foreground">
              新草稿{draft !== null ? ` · ${(draft / 1000).toFixed(1)}s` : ""}
            </span>
            <div className="ml-auto inline-flex items-center gap-1">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => onDiscardDraft(segment.segment_id)}
                disabled={buttonsDisabled}
                aria-label="丢弃新合成的音频"
                className="h-7 px-2 text-[10px]"
              >
                <Trash2 className="h-3 w-3 mr-1" />
                丢弃
              </Button>
              <Button
                size="sm"
                variant="default"
                onClick={() => onAcceptDraft(segment.segment_id)}
                disabled={buttonsDisabled}
                aria-label="接受新合成的音频"
                className="h-7 px-2 text-[10px] bg-[color:var(--ochre)] text-primary-foreground hover:bg-[color:var(--ochre)]/90"
              >
                <Check className="h-3 w-3 mr-1" />
                接受
              </Button>
            </div>
          </div>
        )}

        {/* Saving indicator */}
        {isSaving && (
          <span
            className="text-[10px] text-muted-foreground inline-flex items-center gap-1"
            role="status"
            aria-live="polite"
          >
            <Loader2 className="h-3 w-3 animate-spin" />
            保存中…
          </span>
        )}
      </div>

      {/* ── Right col: persistent action buttons ── */}
      <div className="flex items-start justify-end gap-1.5 flex-wrap">
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handlePlaySource}
          disabled={isFetchingSource || buttonsDisabled}
          aria-label="试听该段原文音频"
          title="试听原音"
          className="h-7 px-2 text-[10.5px]"
        >
          {isFetchingSource ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Play className="h-3 w-3" />
          )}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => onSplit(segment.segment_id)}
          disabled={
            buttonsDisabled
            || (segment.source_text ?? "").length < 2
            || (segment.cn_text ?? "").length < 2
          }
          aria-label="拆分该段"
          title={buttonsDisabled ? "正在批量合成，拆分不可用" : "把这段拆成两段"}
          className="h-7 px-2 text-[10.5px]"
        >
          <Scissors className="h-3 w-3 mr-1" />
          拆分
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={regenVisual.disabled || buttonsDisabled}
          aria-busy={regenVisual.showSpinner}
          aria-label={regenVisual.label}
          title={buttonsDisabled ? "正在批量合成，请等待批量完成" : regenVisual.label}
          onClick={() => onRegenerate(segment.segment_id)}
          className={`h-7 px-2 text-[10.5px] ${regenVisual.className}`}
        >
          {regenVisual.showSpinner ? (
            <Loader2 className="h-3 w-3 animate-spin mr-1" />
          ) : status === "accepted" ? (
            <RefreshCw className="h-3 w-3 mr-1" />
          ) : null}
          {regenVisual.label}
        </Button>
      </div>
    </article>
  )
}
