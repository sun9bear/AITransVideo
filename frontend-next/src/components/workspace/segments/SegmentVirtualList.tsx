"use client"

/**
 * Plan §9.1 SegmentVirtualList — 轻量段列表虚拟滚动。
 *
 * 为什么手写：项目 deps 极度克制（14 个 runtime），加
 * @tanstack/react-virtual 需要权衡；方案里本来就写了"无则手写
 * IntersectionObserver + buffer 5 项"的 fallback。
 *
 * 策略（react-window 风格的 absolute-positioning 变体）：
 * - items 全部按 index 占位，用 `position: absolute + top: offset`
 * - 首次 offset 按 `estimatedItemHeight` 累加；item 渲染后通过
 *   ResizeObserver 测真实高度，写回 heightMap，下一帧重排
 * - 可见窗口 = [scrollTop .. scrollTop + viewportHeight]，二分查找
 *   确定 visibleStart / visibleEnd，外扩 buffer 5 项后渲染
 * - buffer 外的 index 不挂载 children（节省 DOM / reconciliation
 *   成本）；包裹 div 本身仍挂着以占位 + 锚定 scroll
 *
 * activeSegmentId 变化时自动 scroll 到该 item（center-align，
 * smooth）。`prefers-reduced-motion: reduce` 下改为 instant。
 *
 * 外部通过 `ref.scrollToId(id)` 触发命令式跳转（edit 页的
 * "⚠ 定位" 按钮、段点击跳转视频等路径用它）。
 */

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react"

export interface SegmentVirtualListRef {
  /** Imperatively scroll until the item with the given id is in view.
   *  Opts.align='center' (default) mirrors scrollIntoView behaviour.
   *  Falls back to no-op when id isn't in the current items list. */
  scrollToId(id: string, opts?: { align?: "center" | "start" }): void
}

interface SegmentVirtualListProps<T> {
  items: T[]
  /** Stable id extraction. Used for keys + activeSegmentId match. */
  getId: (item: T) => string
  /** Estimated per-item height in px; real heights are measured and
   *  cached on first render. Too small → excessive re-layout; too big
   *  → bigger initial scrollbar. Default 180 matches a typical edit
   *  segment card (textarea + 2 button rows). */
  estimatedItemHeight?: number
  /** Pages above/below the viewport to keep mounted to avoid blank flashes
   *  on fast scroll. 5 is the plan-specified default. */
  overscan?: number
  /** When this id changes, the list auto-scrolls the corresponding item
   *  into center view (respects prefers-reduced-motion). */
  activeSegmentId?: string | null
  /** Render callback for a single item. MUST return a single block
   *  element whose outer box lives inside the absolutely-positioned
   *  wrapper the list controls. */
  renderItem: (item: T, index: number) => ReactNode
  className?: string
  /** Total scroll viewport height. Plan §7.9 桌面 70vh desktop. */
  maxHeight?: string
}

const DEFAULT_ESTIMATED_HEIGHT = 180
const DEFAULT_OVERSCAN = 5

function binarySearchOffset(offsets: readonly number[], target: number): number {
  // Return the largest index i such that offsets[i] <= target.
  let lo = 0
  let hi = offsets.length - 1
  let candidate = 0
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (offsets[mid] <= target) {
      candidate = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return candidate
}

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches
}

function SegmentVirtualListInner<T>(
  {
    items,
    getId,
    estimatedItemHeight = DEFAULT_ESTIMATED_HEIGHT,
    overscan = DEFAULT_OVERSCAN,
    activeSegmentId,
    renderItem,
    className = "",
    maxHeight = "70vh",
  }: SegmentVirtualListProps<T>,
  ref: React.Ref<SegmentVirtualListRef>,
) {
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const itemRefs = useRef<Map<string, HTMLDivElement>>(new Map())
  const [heightMap, setHeightMap] = useState<Record<string, number>>({})
  const [scrollTop, setScrollTop] = useState(0)
  const [viewportHeight, setViewportHeight] = useState(600)

  // ---- Precompute per-item offsets + total height ----
  const { offsets, totalHeight } = useMemo(() => {
    const off: number[] = new Array(items.length)
    let cursor = 0
    for (let i = 0; i < items.length; i++) {
      off[i] = cursor
      const h = heightMap[getId(items[i])] ?? estimatedItemHeight
      cursor += h
    }
    return { offsets: off, totalHeight: cursor }
  }, [items, getId, heightMap, estimatedItemHeight])

  // ---- Determine visible range + overscan ----
  const { startIndex, endIndex } = useMemo(() => {
    if (items.length === 0) return { startIndex: 0, endIndex: -1 }
    const firstVisible = binarySearchOffset(offsets, scrollTop)
    const lastVisibleTarget = scrollTop + viewportHeight
    // Linear scan forward from firstVisible — cheaper than re-searching
    // since viewport typically covers <20 items.
    let last = firstVisible
    while (
      last < items.length - 1
      && offsets[last + 1] <= lastVisibleTarget
    ) {
      last += 1
    }
    return {
      startIndex: Math.max(0, firstVisible - overscan),
      endIndex: Math.min(items.length - 1, last + overscan),
    }
  }, [items.length, offsets, scrollTop, viewportHeight, overscan])

  // ---- Scroll listener ----
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    const handler = () => setScrollTop(el.scrollTop)
    el.addEventListener("scroll", handler, { passive: true })
    return () => el.removeEventListener("scroll", handler)
  }, [])

  // ---- Viewport resize ----
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    const sync = () => setViewportHeight(el.clientHeight)
    sync()
    const obs = new ResizeObserver(sync)
    obs.observe(el)
    return () => obs.disconnect()
  }, [])

  // ---- Measure actually-rendered item heights ----
  const registerItemRef = useCallback((id: string) => (node: HTMLDivElement | null) => {
    if (node) {
      itemRefs.current.set(id, node)
    } else {
      itemRefs.current.delete(id)
    }
  }, [])

  useLayoutEffect(() => {
    // Measure currently-rendered items and diff against heightMap.
    // Write back only changed entries to keep setState minimal.
    const updates: Record<string, number> = {}
    itemRefs.current.forEach((node, id) => {
      const rect = node.getBoundingClientRect()
      const h = Math.ceil(rect.height)
      if (h > 0 && heightMap[id] !== h) {
        updates[id] = h
      }
    })
    if (Object.keys(updates).length > 0) {
      setHeightMap((prev) => ({ ...prev, ...updates }))
    }
  })

  // ---- Imperative handle ----
  useImperativeHandle(ref, () => ({
    scrollToId(id, opts) {
      const container = scrollContainerRef.current
      if (!container) return
      const idx = items.findIndex((it) => getId(it) === id)
      if (idx < 0) return
      const offset = offsets[idx]
      const itemHeight = heightMap[id] ?? estimatedItemHeight
      const align = opts?.align ?? "center"
      let target = offset
      if (align === "center") {
        target = offset - (container.clientHeight - itemHeight) / 2
      }
      target = Math.max(0, Math.min(target, totalHeight - container.clientHeight))
      container.scrollTo({
        top: target,
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      })
    },
  }), [items, getId, offsets, heightMap, estimatedItemHeight, totalHeight])

  // ---- Auto-scroll on activeSegmentId change ----
  useEffect(() => {
    if (!activeSegmentId) return
    const container = scrollContainerRef.current
    if (!container) return
    // Skip if user is interacting with a textarea (prevent fighting)
    const active = document.activeElement
    if (active && active.tagName === "TEXTAREA") return
    const idx = items.findIndex((it) => getId(it) === activeSegmentId)
    if (idx < 0) return
    const offset = offsets[idx]
    const itemHeight = heightMap[activeSegmentId] ?? estimatedItemHeight
    const target = Math.max(
      0,
      Math.min(
        offset - (container.clientHeight - itemHeight) / 2,
        totalHeight - container.clientHeight,
      ),
    )
    // Only scroll if the item isn't already reasonably visible (±20%
    // of viewport) — avoids jumpy rebounds when active flickers.
    const current = container.scrollTop
    const itemVisible =
      offset + itemHeight > current
      && offset < current + container.clientHeight
    const driftFromCenter = Math.abs(target - current)
    if (itemVisible && driftFromCenter < container.clientHeight * 0.2) return

    container.scrollTo({
      top: target,
      behavior: prefersReducedMotion() ? "auto" : "smooth",
    })
    // offsets/heightMap/totalHeight are derived from items — the active
    // trigger should respond only to activeSegmentId changes. eslint
    // exhaustive-deps would loop if we listed all derived sources.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSegmentId])

  // ---- Render ----
  const rendered: ReactNode[] = []
  for (let i = startIndex; i <= endIndex; i++) {
    const item = items[i]
    if (!item) continue
    const id = getId(item)
    const top = offsets[i]
    rendered.push(
      <div
        key={id}
        ref={registerItemRef(id)}
        data-segment-id={id}
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          top: `${top}px`,
        }}
      >
        {renderItem(item, i)}
      </div>,
    )
  }

  const renderedCount = rendered.length
  return (
    <div
      ref={scrollContainerRef}
      className={`overflow-y-auto scrollbar-themed relative border border-border/40 rounded-md ${className}`}
      style={{ maxHeight }}
      // data-* attrs let DevTools inspectors verify virtualization
      // effectiveness: `data-rendered` shows live-mounted item count vs
      // `data-total`. Expect rendered << total for long lists.
      data-virtualized="segment-list"
      data-rendered={renderedCount}
      data-total={items.length}
      data-range={`${startIndex}-${endIndex}`}
    >
      {/* Sticky header — tells the user at a glance "only a window is
          mounted" so "为什么 scrollbar handle 这么小" doesn't read as
          "前端把 200 段全塞 DOM 了". Minimal UI, z-10 keeps it above
          absolute-positioned items below. */}
      {items.length > 0 && (
        <div className="sticky top-0 z-10 px-3 py-1.5 text-xs text-muted-foreground bg-background/85 backdrop-blur border-b border-border/40 flex items-center justify-between">
          <span>
            段落 <strong className="text-foreground">{items.length}</strong>
            <span className="mx-2 text-border">·</span>
            当前 DOM 已挂载 <strong className="text-foreground">{renderedCount}</strong> 段
            <span className="ml-2 text-muted-foreground/60">
              （#{startIndex + 1} – #{endIndex + 1}，其他按需渲染）
            </span>
          </span>
          <span className="text-muted-foreground/60">
            {totalHeight > 0 && `总高 ${(totalHeight / 1000).toFixed(1)}k px`}
          </span>
        </div>
      )}
      <div
        style={{
          position: "relative",
          height: `${totalHeight}px`,
          width: "100%",
        }}
      >
        {rendered}
      </div>
    </div>
  )
}

// Generic forwardRef helper — TS needs the hand-rolled cast because
// forwardRef loses the generic parameter.
export const SegmentVirtualList = forwardRef(SegmentVirtualListInner) as <T>(
  props: SegmentVirtualListProps<T> & { ref?: React.Ref<SegmentVirtualListRef> },
) => ReturnType<typeof SegmentVirtualListInner>
