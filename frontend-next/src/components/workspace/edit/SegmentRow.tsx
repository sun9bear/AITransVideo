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
import { Check, ChevronDown, Loader2, Pause, Play, RefreshCw, Scissors, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { buildDraftAudioUrl } from "@/lib/api/downloads"
import type { EditingSegment, EditingSpeaker, SegmentStatus } from "@/lib/api/editing"
import { formatDurationSeconds, formatMs } from "@/lib/format"

// ---------- helpers ----------

// PR-G: subtitle textarea auto-size, script-aware. CJK glyphs are ~2x wide so
// ~40 fit a row; Latin ~80. Pick the divisor from the text's OWN script rather
// than the source/target slot, so a non-default pair (e.g. zh source / en target)
// sizes correctly. For the en->zh default (en source / zh target) this yields the
// legacy 80 / 40 — byte-identical.
const _CJK_RE = /[㐀-鿿豈-﫿぀-ヿ가-힯]/g
function rowsForText(text: string): number {
  const len = text.length || 1
  const cjk = (text.match(_CJK_RE) || []).length
  const perRow = cjk * 2 >= len ? 40 : 80 // CJK-dominant → 40, else Latin-width 80
  return Math.max(1, Math.ceil(len / perRow))
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

  // ---- source audio preview (offscreen Audio API per plan §3.4) ----
  const audioInstanceRef = useRef<HTMLAudioElement | null>(null)
  const [isFetchingSource, setIsFetchingSource] = useState(false)
  const [isSourcePlaying, setIsSourcePlaying] = useState(false)

  // ---- draft audio (also Audio API, custom compact controls per
  // user feedback 2026-05-17: native <audio controls> too tall/ugly) ----
  const draftAudioRef = useRef<HTMLAudioElement | null>(null)
  const [isDraftPlaying, setIsDraftPlaying] = useState(false)
  const [draftCurTime, setDraftCurTime] = useState(0)
  const [draftTotalTime, setDraftTotalTime] = useState(0)

  useEffect(() => {
    // Clean up source audio when component unmounts so audio doesn't
    // keep playing after the row leaves the virtualization window.
    return () => {
      const inst = audioInstanceRef.current
      if (inst) {
        inst.pause()
        inst.src = ""
        audioInstanceRef.current = null
      }
      const dr = draftAudioRef.current
      if (dr) {
        dr.pause()
        dr.src = ""
        draftAudioRef.current = null
      }
    }
  }, [])

  // Lazy-load draft audio when segment enters tts_dirty. Re-init on
  // draft duration change (i.e., user re-regenerated → new wav).
  useEffect(() => {
    if (status !== "tts_dirty") {
      // Tear down draft player when leaving tts_dirty (accept / discard).
      const prev = draftAudioRef.current
      if (prev) {
        prev.pause()
        prev.src = ""
        draftAudioRef.current = null
      }
      setIsDraftPlaying(false)
      setDraftCurTime(0)
      setDraftTotalTime(0)
      return
    }
    const url = buildDraftAudioUrl(jobId, segment.segment_id)
    const audio = new Audio(url)
    audio.preload = "metadata"
    const onMeta = () => setDraftTotalTime(audio.duration || 0)
    const onPlay = () => setIsDraftPlaying(true)
    const onPause = () => setIsDraftPlaying(false)
    const onEnd = () => {
      setIsDraftPlaying(false)
      setDraftCurTime(0)
    }
    const onTime = () => setDraftCurTime(audio.currentTime)
    audio.addEventListener("loadedmetadata", onMeta)
    audio.addEventListener("play", onPlay)
    audio.addEventListener("pause", onPause)
    audio.addEventListener("ended", onEnd)
    audio.addEventListener("timeupdate", onTime)
    draftAudioRef.current = audio
    return () => {
      audio.pause()
      audio.removeEventListener("loadedmetadata", onMeta)
      audio.removeEventListener("play", onPlay)
      audio.removeEventListener("pause", onPause)
      audio.removeEventListener("ended", onEnd)
      audio.removeEventListener("timeupdate", onTime)
      audio.src = ""
      draftAudioRef.current = null
    }
    // Re-init on new draft wav (duration changes after re-regenerate).
  }, [status, jobId, segment.segment_id, segment.draft_wav_duration_ms])

  const toggleDraftPlay = () => {
    const a = draftAudioRef.current
    if (!a) return
    if (a.paused) {
      a.play().catch(() => {})
    } else {
      a.pause()
    }
  }

  const handlePlaySource = async () => {
    const inst = audioInstanceRef.current
    if (inst) {
      if (!inst.paused) {
        inst.pause()
        return
      }
      try {
        inst.currentTime = 0
        await inst.play()
      } catch {
        // autoplay policy may block
      }
      return
    }
    setIsFetchingSource(true)
    try {
      const url = await onPreviewSource(segment.segment_id)
      if (url) {
        const audio = new Audio(url)
        audio.addEventListener("play", () => setIsSourcePlaying(true))
        audio.addEventListener("pause", () => setIsSourcePlaying(false))
        audio.addEventListener("ended", () => setIsSourcePlaying(false))
        audioInstanceRef.current = audio
        await audio.play().catch(() => {})
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
  // Tension bar 双色分阶（plan 2026-05-21 §3.3）。
  // ARIA spec 要求 aria-valuenow 落在 [valuemin, valuemax] 区间内。
  // 视觉宽度也按 ±20% clamp，超限时条带触底由颜色表达"超限"语义。
  // 文本仍显示真实 deviationPct（屏幕阅读器会读到文本节点）。
  const deviationPct =
    draftRatio !== null ? Math.round((draftRatio - 1) * 100) : 0
  const clampedDeviationPct = Math.max(-20, Math.min(20, deviationPct))
  const showTensionBar = draftRatio !== null && Math.abs(deviationPct) >= 5
  const isSevere = Math.abs(deviationPct) > 20

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
        // Mobile (<768px): 2-col grid (70px meta + 1fr text), actions row spans col 2 below.
        // Tablet/desktop (≥768px): 3-col grid (100px / 1fr / 230px actions on the right).
        "grid grid-cols-[70px_1fr] sm:grid-cols-[100px_1fr_230px] gap-2 sm:gap-3 py-2.5 px-3 border-b border-border last:border-b-0",
        // Zebra: even rows get a slightly raised tint so adjacent rows
        // are visually separable even with bilingual content (user request).
        // hover: tint primary at very low alpha for the row under cursor.
        // active overrides both with the cinnabar left-border + bg-primary.
        "transition-colors",
        isActive
          ? "border-l-2 border-l-primary -ml-[2px] pl-[14px] bg-primary/[0.06]"
          : [
              "border-l-2 border-l-transparent -ml-[2px] pl-[14px]",
              index % 2 === 1 ? "bg-muted/30" : "bg-transparent",
              "hover:bg-primary/[0.04]",
            ].join(" "),
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
          rows={rowsForText(localSource)}
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
          rows={rowsForText(localText)}
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

        {/* Force-DSP indicator — 张力偏差由下方双色 progress bar 负责 */}
        {isAnomalous && (
          <div className="text-[10px] text-destructive">
            <span>
              ⚠ 时长异常
              {segment.duration_diff_ratio !== undefined && (
                <span className="ml-1">（{Math.round(segment.duration_diff_ratio * 100)}%）</span>
              )}
            </span>
          </div>
        )}

        {/* Tension bar — 替换原 hasDraftMismatch 单行文本（plan 2026-05-21 §3.3）。
         *  5%-20%: 灰色非警示刻度；>20%: 朱砂警示。视觉宽度与 aria 用 clamp 值，
         *  文本仍显示真实 deviationPct，屏幕阅读器读文本节点。
         *  禁止任何 @keyframes / blur / backdrop-filter 动画（plan §1.5 硬约束）。 */}
        {showTensionBar && (
          <div className="flex items-center gap-2 mt-1">
            <div
              className="relative h-[3px] flex-1 rounded-full bg-muted/50 overflow-hidden"
              role="meter"
              aria-valuenow={clampedDeviationPct}
              aria-valuemin={-20}
              aria-valuemax={20}
              aria-label="新 TTS 时长偏差（百分比）"
            >
              <div
                className={`absolute top-0 h-full ${
                  isSevere ? "bg-[color:var(--cinnabar)]" : "bg-[color:var(--ink-gray-3)]"
                }`}
                style={{
                  left: clampedDeviationPct >= 0 ? "50%" : `${50 + clampedDeviationPct * 2.5}%`,
                  width: `${Math.abs(clampedDeviationPct) * 2.5}%`,
                }}
              />
              <div className="absolute top-0 left-1/2 h-full w-px bg-border" />
            </div>
            <span
              className={`text-[10px] tabular-nums ${
                isSevere ? "text-[color:var(--cinnabar)]" : "text-muted-foreground"
              }`}
              title="新 TTS 与目标时长的偏差"
            >
              {deviationPct > 0 ? "+" : ""}{deviationPct}%
            </span>
          </div>
        )}

        {/* No inline <audio> for source preview — playback happens via
         *  the offscreen Audio API in handlePlaySource, with full
         *  controls available in the left CurrentSegmentOpsPanel
         *  (plan §3.2 "左侧 ops panel = 操作全集，右侧行内按钮 = 快捷方式"). */}

        {/* Draft panel — appears inline when tts_dirty (plan §3.3 primary
         *  location). Custom compact controls instead of native
         *  <audio controls> (user feedback 2026-05-17: native bar too
         *  tall/ugly + brought volume slider / kebab menu noise). */}
        {status === "tts_dirty" && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[10.5px] border-l-2 border-[color:var(--ochre)] bg-[color:var(--ochre)]/8 rounded-r pl-2 pr-2 py-1.5">
            <button
              type="button"
              onClick={toggleDraftPlay}
              disabled={buttonsDisabled}
              aria-label={isDraftPlaying ? "暂停草稿" : "播放草稿"}
              className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-[color:var(--ochre)] text-primary-foreground hover:bg-[color:var(--ochre)]/90 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ochre)] focus-visible:ring-offset-1"
            >
              {isDraftPlaying ? <Pause className="h-3 w-3" /> : <Play className="h-3 w-3 ml-[1px]" />}
            </button>
            <span className="text-[10.5px] font-medium text-[color:var(--ochre)]">
              新草稿
            </span>
            <span className="text-[10px] text-muted-foreground tabular-nums">
              {draftTotalTime > 0
                ? `${draftCurTime.toFixed(1)} / ${draftTotalTime.toFixed(1)}s`
                : draft !== null
                  ? `${(draft / 1000).toFixed(1)}s`
                  : "…"}
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

      {/* ── Right col (desktop) / second row (mobile): persistent action buttons ──
       *   Mobile (<sm): col-start-2 puts buttons in col 2 below text, justify-start.
       *   Tablet+    : col-auto (3rd col), justify-end. */}
      <div className="col-start-2 sm:col-auto flex items-start justify-start sm:justify-end gap-1.5 flex-wrap pt-1 sm:pt-0">
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handlePlaySource}
          disabled={isFetchingSource || buttonsDisabled}
          aria-label={isSourcePlaying ? "暂停" : "试听该段原文音频"}
          title={isSourcePlaying ? "暂停" : "试听原音"}
          className="h-7 px-2 text-[10.5px]"
        >
          {isFetchingSource ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : isSourcePlaying ? (
            <Pause className="h-3 w-3" />
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
            || isRegenerating
            || status === "tts_loading"
            || isSaving
            || (segment.source_text ?? "").length < 2
            || (segment.cn_text ?? "").length < 2
          }
          aria-label="拆分该段"
          title={
            buttonsDisabled
              ? "正在批量合成，拆分不可用"
              : isRegenerating || status === "tts_loading"
                ? "该段正在合成，拆分不可用（避免 orphan draft）"
                : isSaving
                  ? "正在保存编辑，请稍候"
                  : "把这段拆成两段"
          }
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
