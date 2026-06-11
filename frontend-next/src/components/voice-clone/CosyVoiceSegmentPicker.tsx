"use client"

// frontend-next/src/components/voice-clone/CosyVoiceSegmentPicker.tsx
//
// Phase 4.2 E.2 — CosyVoice 克隆 modal 的 source_segments picker。
//
// **职责**：在 CosyVoiceCloneModal 的 sampleMode === "segments" 分支下，
// 渲染当前 speaker 的转写段列表，让用户勾选段落作为克隆样本来源。
//
// **关键安全约束（CLAUDE.md 付费 API 硬约束 + Phase 4.2 E.2 spec v2.1）**：
//
//   1. **只读端点**：本组件**只**调用 `getSpeakerAudioSegments(jobId, speakerId)`
//      —— 这是一个 GET，不进入任何 paid 流程。组件**禁止** import
//      `submitCosyvoiceClone` 或任何 `/api/voice/cosyvoice/clone` 相关
//      端点（守卫：test_e2_segment_picker_no_paid_api_imports +
//      test_e2_segment_picker_no_clone_endpoint_in_source）。
//
//   2. **跨 speaker 防御 #1**：getSpeakerAudioSegments 在后端按 speaker 过滤，
//      返回值都属于 prop speakerId。picker UI 不显示其他 speaker 的段（守卫：
//      test_e2_segment_picker_speaker_id_required_in_api_call）。
//
//   3. **跨 speaker 防御 #2（v2.1 锁定的 onAvailableSegmentIdsChange 契约）**：
//      加载完成后立即把段全集 id 数组回传给父 modal，父 modal 包装成
//      `Set<number>` 用于提交前的子集 assert。这层防御覆盖"用户在 modal
//      打开期间切换 speaker 导致 UI 状态泄漏"的场景（守卫：
//      test_e2_picker_declares_on_available_segment_ids_change_prop +
//      test_e2_modal_passes_on_available_segment_ids_change_to_picker）。
//
//   4. **类型严格 `number[]`**：选段 id 永远是 number；防止 string 漂移
//      让后端 strict `type(x) is int` 检查被绕过（守卫：
//      test_e2_no_string_segment_id_drift）。
//
//   5. **3.0–60.0 秒阈值**：与 backend `gateway/cosyvoice_clone/sample_validator.py`
//      `MIN_DURATION_MS = 3_000` / `MAX_DURATION_MS = 60_000` 一致。客户端
//      提前禁用提交，后端仍是最终权威（守卫：
//      test_e2_modal_three_to_sixty_second_threshold_literal cross-file 校验）。

import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import {
  getSpeakerAudioSegments,
  type SpeakerAudioSegment,
} from "@/lib/api/voiceSelection"
import { formatTimecode } from "@/lib/format"

/**
 * Props 契约（Phase 4.2 E.2 spec v2.1 §4 锁定）。
 *
 * 任何字段类型 / 名称改动都需要同步守卫测试 + spec 文档。
 */
export interface CosyVoiceSegmentPickerProps {
  /**
   * 必填 — picker 只显示此 speaker 的段。跨 speaker 防御层 #1（getSpeakerAudioSegments
   * 后端按 speaker filter）+ 层 #3（父 modal 提交前用回传的全集做子集 assert）。
   */
  speakerId: string
  /** 必填 — `getSpeakerAudioSegments(jobId, speakerId)` 的入参。 */
  jobId: string
  /**
   * 当前选中段 id 集合。强类型 `number[]`，防止 string 漂移（守卫：
   * test_e2_no_string_segment_id_drift）。父 modal (`CosyVoiceCloneModal`)
   * 持有 state；picker 不持有 selection state。
   */
  selectedSegmentIds: number[]
  /** 选择变更回调 —— 父 modal 用 `setSelectedSegmentIds` 替换。 */
  onChange: (next: number[]) => void
  /**
   * **v2.1 锁定的 picker → modal 回传契约**。
   *
   * Picker 加载完 `getSpeakerAudioSegments` 后**立即**调用一次此 callback，
   * 传入**段全集**（`segments.map(s => s.segmentId)`）。父 modal 收到后
   * 包装成 `Set<number>` 用于提交前的子集 assert：
   *
   * ```ts
   * setAvailableSegmentIds(new Set(ids))
   * ```
   *
   * 必填 —— 任何调用点不传都会被守卫拒绝（守卫：
   * test_e2_modal_passes_on_available_segment_ids_change_to_picker）。
   */
  onAvailableSegmentIdsChange: (ids: number[]) => void
  /**
   * 已选段总时长（**毫秒**）变化回调。每当 selectedSegmentIds 或 segments
   * 列表变化，picker 重新计算并回传当前选段的总时长——单位严格毫秒，与
   * backend `MIN_DURATION_MS = 3_000` / `MAX_DURATION_MS = 60_000` 完全同
   * 精度（spec v2.2 §0 决策 2）。
   *
   * **v2.2 关键决策（Codex PR #16 P2 fix）**：聚合源用 `endMs - startMs`
   * 精确毫秒差值，**不**用 `SpeakerAudioSegment.durationS`（一位小数 round
   * 值，会让 2.96s → 3.0s 边界漂移，前端放行被后端拒收）。
   *
   * 父 modal 用此值在 `canRequestConsent` 中校验 `[3000, 60000]` ms 区间
   * （spec §4 E.2.4 L1）。UI 展示时可以 `ms / 1000` 后 `toFixed(1)`；
   * 校验**不读**展示值。
   */
  onSelectedDurationMsChange: (ms: number) => void
  /**
   * 可选 —— editing 模式下父组件可以传入预加载的段列表，跳过网络请求。
   *
   * **注意：E.2 阶段 editing 路径不接 picker（spec v2.1 §0 决策 1），此 prop
   * 保留为 future hook，目前不会有调用点传入。**
   */
  preloadedSegments?: SpeakerAudioSegment[]
  /** 禁用整个 picker（提交中 / 网络错误等）。 */
  disabled?: boolean
}

/**
 * 客户端时长阈值（**毫秒**），与后端
 * `gateway/cosyvoice_clone/sample_validator.py` 的
 * `MIN_DURATION_MS = 3_000` / `MAX_DURATION_MS = 60_000` **完全同单位**。
 *
 * **v2.2 决策（Codex PR #16 P2 fix）**：前端聚合 + 校验全用毫秒，避免
 * `durationS` 一位小数 round 边界漂移。
 *
 * 这些字面量必须出现在本文件 + modal 文件中，被守卫 #11 ms-precision
 * cross-check 扫到才算合规。
 */
const MIN_DURATION_MS = 3000
const MAX_DURATION_MS = 60000

/** 推荐区间（毫秒，仅提示，不强制）。后端 `RECOMMENDED_MIN/MAX_DURATION_MS`. */
const RECOMMENDED_MIN_MS = 10000
const RECOMMENDED_MAX_MS = 20000

export function CosyVoiceSegmentPicker({
  speakerId,
  jobId,
  selectedSegmentIds,
  onChange,
  onAvailableSegmentIdsChange,
  onSelectedDurationMsChange,
  preloadedSegments,
  disabled = false,
}: CosyVoiceSegmentPickerProps) {
  // ---------------------------------------------------------------------------
  // 段加载（speakerId/jobId 变化时重新拉）
  // ---------------------------------------------------------------------------
  const [segments, setSegments] = useState<SpeakerAudioSegment[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)

  // **v2.1 锁定的回传契约的本地稳定引用**。避免 onAvailableSegmentIdsChange
  // 在父 modal 中是匿名箭头函数时频繁触发 effect。
  const onAvailableIdsRef = useRef(onAvailableSegmentIdsChange)
  useEffect(() => {
    onAvailableIdsRef.current = onAvailableSegmentIdsChange
  }, [onAvailableSegmentIdsChange])

  useEffect(() => {
    let cancelled = false

    // E.2 阶段不会走 preloadedSegments 分支（spec v2.1 §0 决策 1：editing
    // 路径只走 file upload），但 future hook 保留。
    if (preloadedSegments && preloadedSegments.length > 0) {
      const sorted = [...preloadedSegments].sort((a, b) => a.startMs - b.startMs)
      setSegments(sorted)
      onAvailableIdsRef.current(sorted.map((s) => s.segmentId))
      setIsLoading(false)
      setLoadError(null)
      return
    }

    setIsLoading(true)
    setLoadError(null)

    // 跨 speaker 防御 #1：后端按 speaker filter。这里以 prop speakerId 调，
    // 不允许写死或绕过（守卫：test_e2_segment_picker_speaker_id_required_in_api_call）。
    getSpeakerAudioSegments(jobId, speakerId)
      .then((resp) => {
        if (cancelled) return
        const sorted = [...resp.segments].sort((a, b) => a.startMs - b.startMs)
        setSegments(sorted)
        // **v2.1**：picker 加载完成后立刻回传段全集 id 给父 modal。
        // 父 modal 包装成 `Set<number>` 用于提交前子集 assert。
        onAvailableIdsRef.current(sorted.map((s) => s.segmentId))
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const msg =
          err instanceof Error
            ? err.message
            : "加载段列表失败，请稍后重试"
        setLoadError(msg)
        // 失败时回传空数组 —— 父 modal subset assert 一定不通过，提交按钮
        // 保持 disabled，避免用户误以为"什么都没选也能提交"。
        onAvailableIdsRef.current([])
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [jobId, speakerId, preloadedSegments])

  // ---------------------------------------------------------------------------
  // 选择 toggle —— 严格 `number[]`，去重，无 string 漂移
  // ---------------------------------------------------------------------------
  const selectedSet = useMemo(
    () => new Set(selectedSegmentIds),
    [selectedSegmentIds],
  )

  const toggleSegment = useCallback(
    (segmentId: number) => {
      if (disabled) return
      const next = new Set(selectedSet)
      if (next.has(segmentId)) {
        next.delete(segmentId)
      } else {
        next.add(segmentId)
      }
      // Array.from + sort by Array order — 数字升序保持稳定输出
      onChange(Array.from(next).sort((a, b) => a - b))
    },
    [selectedSet, onChange, disabled],
  )

  // ---------------------------------------------------------------------------
  // 总时长（已选段）+ 阈值校验（毫秒精度，spec v2.2 §0 决策 2）
  // ---------------------------------------------------------------------------
  // **v2.2**：聚合源用 `endMs - startMs` 而非 `durationS`，避免一位小数
  // round 边界漂移（真实 2.96s 不会显示成 3.0s 并被前端误放行）。
  const totalSelectedMs = useMemo(() => {
    let total = 0
    for (const seg of segments) {
      if (selectedSet.has(seg.segmentId)) {
        total += Math.max(0, seg.endMs - seg.startMs)
      }
    }
    return total
  }, [segments, selectedSet])

  // 总时长回传给父 modal（派生数据通道，单位严格 ms）。每当 selected ids
  // 或 segments 变化都报一次。父 modal 用此值校验 [3000, 60000] ms 区间
  // （spec §4 E.2.4 L1）。
  const onDurationMsRef = useRef(onSelectedDurationMsChange)
  useEffect(() => {
    onDurationMsRef.current = onSelectedDurationMsChange
  }, [onSelectedDurationMsChange])
  useEffect(() => {
    onDurationMsRef.current(totalSelectedMs)
  }, [totalSelectedMs])

  // 客户端阈值：< MIN_DURATION_MS 红色 + 还需 X 秒；> MAX_DURATION_MS 红色 +
  // 超出 X 秒；推荐范围内绿色 + ✓。展示层除 1000 后 toFixed(1)；校验不读
  // 展示值。常量字面量 3000 / 60000 必须出现在本文件，守卫 #11 ms-cross-check 扫。
  const durationStatus = useMemo(() => {
    if (selectedSegmentIds.length === 0) {
      return { kind: "empty" as const }
    }
    if (totalSelectedMs < MIN_DURATION_MS) {
      const needSec = (MIN_DURATION_MS - totalSelectedMs) / 1000
      return {
        kind: "too_short" as const,
        message: `还需 ${needSec.toFixed(1)}s 才能克隆（最少 ${MIN_DURATION_MS / 1000}s）`,
      }
    }
    if (totalSelectedMs > MAX_DURATION_MS) {
      const overSec = (totalSelectedMs - MAX_DURATION_MS) / 1000
      return {
        kind: "too_long" as const,
        message: `已超出 ${overSec.toFixed(1)}s（最多 ${MAX_DURATION_MS / 1000}s）`,
      }
    }
    const inRecommended =
      totalSelectedMs >= RECOMMENDED_MIN_MS &&
      totalSelectedMs <= RECOMMENDED_MAX_MS
    return {
      kind: "ok" as const,
      inRecommended,
    }
  }, [selectedSegmentIds.length, totalSelectedMs])

  // ---------------------------------------------------------------------------
  // 试听 —— 同时只允许一段播放（参考 VoiceSelectionPanel 模式）
  // ---------------------------------------------------------------------------
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [playingSegmentId, setPlayingSegmentId] = useState<number | null>(null)

  useEffect(() => {
    return () => {
      if (audioRef.current) {
        audioRef.current.pause()
        audioRef.current = null
      }
    }
  }, [])

  const playSegment = useCallback(
    (seg: SpeakerAudioSegment) => {
      if (audioRef.current) {
        audioRef.current.pause()
        audioRef.current = null
      }
      if (playingSegmentId === seg.segmentId) {
        setPlayingSegmentId(null)
        return
      }
      const audio = new Audio(seg.audioUrl)
      audio.onended = () => setPlayingSegmentId(null)
      audio.onerror = () => setPlayingSegmentId(null)
      audio.play().catch(() => setPlayingSegmentId(null))
      audioRef.current = audio
      setPlayingSegmentId(seg.segmentId)
    },
    [playingSegmentId],
  )

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  if (isLoading) {
    return (
      <div className="min-w-0 rounded-lg border border-border bg-muted/20 p-4 text-sm text-muted-foreground">
        加载当前说话人的转写段...
      </div>
    )
  }

  if (loadError) {
    return (
      <div className="min-w-0 space-y-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
        <p>{loadError}</p>
        <p className="text-muted-foreground">
          可改用「上传音频文件」模式，或稍后重试。
        </p>
      </div>
    )
  }

  if (segments.length === 0) {
    return (
      <div className="min-w-0 rounded-lg border border-border bg-muted/20 p-3 text-xs text-muted-foreground">
        当前说话人没有可拼段。请改用「上传音频文件」模式克隆。
      </div>
    )
  }

  return (
    <div className="min-w-0 space-y-2">
      {/* 总时长指示器 */}
      <div
        data-picker-status={durationStatus.kind}
        className={
          "min-w-0 break-words rounded-md border px-3 py-2 text-xs " +
          (durationStatus.kind === "ok"
            ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
            : durationStatus.kind === "empty"
              ? "border-border bg-muted/20 text-muted-foreground"
              : "border-destructive/40 bg-destructive/10 text-destructive")
        }
      >
        {durationStatus.kind === "empty" && (
          <span>
            勾选 {MIN_DURATION_MS / 1000}-{MAX_DURATION_MS / 1000} 秒的段作为
            克隆样本（推荐 {RECOMMENDED_MIN_MS / 1000}-
            {RECOMMENDED_MAX_MS / 1000} 秒效果最好）
          </span>
        )}
        {durationStatus.kind === "ok" && (
          <span>
            ✓ 已选 {selectedSegmentIds.length} 段 · 共{" "}
            {(totalSelectedMs / 1000).toFixed(1)}s
            {!durationStatus.inRecommended &&
              ` · 建议落到 ${RECOMMENDED_MIN_MS / 1000}-${RECOMMENDED_MAX_MS / 1000}s`}
          </span>
        )}
        {(durationStatus.kind === "too_short" ||
          durationStatus.kind === "too_long") && (
          <span>{durationStatus.message}</span>
        )}
      </div>

      {/* 段列表 */}
      <div className="max-h-72 w-full min-w-0 overflow-x-hidden overflow-y-auto rounded-lg border border-border bg-card/30">
        <ul className="min-w-0 divide-y divide-border/60">
          {segments.map((seg) => {
            const isSelected = selectedSet.has(seg.segmentId)
            const isPlaying = playingSegmentId === seg.segmentId
            // v2.2 ms-precision：与总时长校验同一精度源
            const segDurationMs = Math.max(0, seg.endMs - seg.startMs)
            const isShortSingle = segDurationMs < MIN_DURATION_MS
            return (
              <li
                key={seg.segmentId}
                className={
                  "flex min-w-0 items-start gap-2 px-3 py-2 transition " +
                  (isSelected ? "bg-primary/5" : "hover:bg-muted/30")
                }
              >
                <input
                  type="checkbox"
                  checked={isSelected}
                  onChange={() => toggleSegment(seg.segmentId)}
                  disabled={disabled}
                  className="mt-1 h-4 w-4 shrink-0"
                  aria-label={`选段 ${seg.segmentId}`}
                />
                <button
                  type="button"
                  onClick={() => playSegment(seg)}
                  disabled={disabled}
                  className="h-7 w-7 shrink-0 rounded-full border border-border flex items-center justify-center hover:bg-muted transition disabled:opacity-50"
                  aria-label={isPlaying ? "停止" : "试听"}
                >
                  {isPlaying ? (
                    <svg
                      className="h-3 w-3 text-primary"
                      fill="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <rect height="16" rx="1" width="4" x="6" y="4" />
                      <rect height="16" rx="1" width="4" x="14" y="4" />
                    </svg>
                  ) : (
                    <svg
                      className="h-3 w-3 text-slate-500"
                      fill="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path d="M8 5v14l11-7z" />
                    </svg>
                  )}
                </button>
                <span className="w-12 shrink-0 text-xs tabular-nums text-slate-400">
                  {formatTimecode(seg.startMs)}
                </span>
                <span
                  className="min-w-0 flex-1 overflow-hidden break-words text-xs leading-5 text-foreground"
                  title={seg.sourceText || `片段 ${seg.segmentId}`}
                >
                  {seg.sourceText || `片段 ${seg.segmentId}`}
                </span>
                <span
                  className={
                    "w-12 shrink-0 text-right text-xs tabular-nums " +
                    (isShortSingle
                      ? "text-amber-600 dark:text-amber-400"
                      : "text-muted-foreground")
                  }
                  title={
                    isShortSingle
                      ? "单段较短，建议组合多段拼到 3 秒以上"
                      : undefined
                  }
                >
                  {(segDurationMs / 1000).toFixed(1)}s
                </span>
              </li>
            )
          })}
        </ul>
      </div>
    </div>
  )
}
