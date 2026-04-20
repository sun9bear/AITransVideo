"use client"

/**
 * Plan §9.2 — 播放器进度 → 当前段 ID 的联动 hook。
 *
 * 监听 `<video>` 的 `timeupdate`（throttle 200ms，浏览器原生 ~4/s 也在
 * 这区间），用二分查找把 currentTime 落位到某个段，返回 `activeSegmentId`
 * 供调用方（1）高亮段卡（2）把段卡滚进视野。
 *
 * 约定：
 * - `segments` 必须按 `startMs` 升序排列（调用方责任，方案 §9.1 里
 *   段列表本来就按时间顺序）。空数组 / 未 mount ref → 返回 null。
 * - 默认"最新开始且还没结束"的段胜出——即如果两段时间重叠，currentTime
 *   处于交界 ms 时取 endMs 还没过的那个。罕见，但对齐流水线段 split 语
 *   义（split 后新段 startMs 严格递增）。
 * - currentTime 先于所有段 or 超过最后段 endMs → null（UI 表现为"没
 *   有高亮段"，滚动保持不动）。
 * - Throttle 纯通过时间戳比较；不依赖 lodash / requestAnimationFrame，
 *   单 hook 无依赖。
 */

import { useEffect, useRef, useState, type RefObject } from "react"

export interface PlayerSyncSegment {
  segmentId: string
  startMs: number
  endMs: number
}

const THROTTLE_MS = 200

function findActiveSegmentId(
  segments: ReadonlyArray<PlayerSyncSegment>,
  currentMs: number,
): string | null {
  if (segments.length === 0) return null
  // Binary search for the largest startMs <= currentMs.
  let lo = 0
  let hi = segments.length - 1
  let candidateIdx = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const seg = segments[mid]
    if (seg.startMs <= currentMs) {
      candidateIdx = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  if (candidateIdx < 0) return null
  const candidate = segments[candidateIdx]
  if (currentMs > candidate.endMs) return null
  return candidate.segmentId
}

export function usePlayerSegmentSync(
  videoRef: RefObject<HTMLVideoElement | null>,
  segments: ReadonlyArray<PlayerSyncSegment>,
): { activeSegmentId: string | null } {
  const [activeSegmentId, setActiveSegmentId] = useState<string | null>(null)
  // Keep the last applied time in a ref so we can debounce without a
  // render loop; compare ms-granularity to skip redundant setStates.
  const lastAppliedMsRef = useRef<number>(-1)
  const lastTickRef = useRef<number>(0)

  useEffect(() => {
    const video = videoRef.current
    if (!video) return

    const handler = () => {
      const now = performance.now()
      if (now - lastTickRef.current < THROTTLE_MS) return
      lastTickRef.current = now

      const ms = Math.floor(video.currentTime * 1000)
      if (ms === lastAppliedMsRef.current) return
      lastAppliedMsRef.current = ms

      const next = findActiveSegmentId(segments, ms)
      setActiveSegmentId((prev) => (prev === next ? prev : next))
    }

    // Fire once on mount so initial frame is mapped (no wait for first
    // timeupdate event, which only fires during play).
    handler()

    video.addEventListener("timeupdate", handler)
    video.addEventListener("seeked", handler)
    return () => {
      video.removeEventListener("timeupdate", handler)
      video.removeEventListener("seeked", handler)
    }
  }, [videoRef, segments])

  return { activeSegmentId }
}
