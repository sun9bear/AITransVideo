"use client"

import { useEffect, useRef, useState } from "react"
import { useFeaturedDemos } from "./featured-demos-context"

/**
 * FeaturedDemoCard — single demo card.
 *
 * Owns:
 *   - Local `tab` state ("zh" | "en") for the EN/CN toggle
 *   - `videoRef` for imperative pause / currentTime preservation
 *
 * Reads:
 *   - `currentlyPlayingId` from context — pauses self when another card
 *     starts playing
 *
 * Writes:
 *   - On <video> `onPlay`, calls `setCurrentlyPlayingId(demo.id)`
 *
 * Tab swap behaviour: when the user toggles between zh and en, we capture
 * `currentTime` from the current video, swap the `src` (driven by `key=
 * activeSrc` remount), restore `currentTime` after the new <source> mounts,
 * and leave it paused. Both clips share the exact same time range so the
 * position is meaningful within each clip's local 0..duration timeline.
 */

export type Demo = {
  id: string
  display_name: string
  source_label: string
  segment_label: string
  original_src: string
  dubbed_src: string
  poster_src: string
  natural_width: number
  natural_height: number
}

export function FeaturedDemoCard({ demo, ariaHidden = false }: { demo: Demo; ariaHidden?: boolean }) {
  const [tab, setTab] = useState<"zh" | "en">("zh")
  const videoRef = useRef<HTMLVideoElement | null>(null)
  // Captured currentTime stashed across the src-swap remount — see handleTabChange.
  const pendingResumeTimeRef = useRef<number | null>(null)
  const { currentlyPlayingId, setCurrentlyPlayingId } = useFeaturedDemos()

  // Pause-others coordination: if another card is playing and ours isn't, pause us.
  useEffect(() => {
    if (currentlyPlayingId && currentlyPlayingId !== demo.id) {
      const v = videoRef.current
      if (v && !v.paused) v.pause()
    }
  }, [currentlyPlayingId, demo.id])

  function handleTabChange(next: "zh" | "en") {
    if (next === tab) return
    const v = videoRef.current
    pendingResumeTimeRef.current = v?.currentTime ?? 0
    setTab(next)
    // The currentTime restore happens in the new <video>'s onLoadedMetadata
    // callback — see below. Doing it here would race the React remount.
  }

  function handleLoadedMetadata() {
    const v = videoRef.current
    const t = pendingResumeTimeRef.current
    if (v && t != null) {
      try {
        v.currentTime = t
      } catch {
        /* setting currentTime before metadata is ready throws — but
           onLoadedMetadata guarantees metadata IS ready, so this almost
           never fires. Swallowed to avoid surfacing a benign edge case. */
      }
      pendingResumeTimeRef.current = null
    }
  }

  function handlePlay() {
    setCurrentlyPlayingId(demo.id)
  }

  const activeSrc = tab === "zh" ? demo.dubbed_src : demo.original_src

  return (
    <article
      className="demo-card group relative flex w-[320px] shrink-0 flex-col overflow-hidden rounded-xl border border-border bg-card shadow-sm transition-[transform,opacity,box-shadow] duration-200 ease-out md:w-[360px]"
      aria-hidden={ariaHidden ? true : undefined}
      // `aria-hidden` keeps screen readers from reading 10 cards instead
      // of 5. Keyboard isolation is handled per-element via tabIndex={-1}
      // on the tab <button>s and the <video>, NOT via `inert` on the
      // article — `inert` would block pointer events too, which kills
      // the CSS :hover effect on duplicate cards (they'd never trigger
      // the hover-shrink-others animation). Trade-off accepted: a sighted
      // mouse user can theoretically click a duplicate's video play
      // button, which is harmless (worst case, two copies of the same
      // demo play; the carousel rotates one offscreen quickly anyway).
      aria-label={demo.display_name}
    >
      {/* Tab row — segmented control above video */}
      <div role="tablist" aria-label="原片 / 配音版" className="flex border-b border-border">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "zh"}
          tabIndex={ariaHidden ? -1 : 0}
          onClick={() => handleTabChange("zh")}
          className={`flex-1 px-3 py-2 text-xs font-medium transition-colors ${
            tab === "zh"
              ? "bg-[color:var(--cinnabar,#C73E3A)] text-white"
              : "bg-[color:var(--cinnabar-soft,rgba(199,62,58,0.08))] text-[color:var(--cinnabar,#C73E3A)]"
          }`}
        >
          中文配音版
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "en"}
          tabIndex={ariaHidden ? -1 : 0}
          onClick={() => handleTabChange("en")}
          className={`flex-1 px-3 py-2 text-xs font-medium transition-colors ${
            tab === "en"
              ? "bg-[color:var(--cinnabar,#C73E3A)] text-white"
              : "bg-[color:var(--cinnabar-soft,rgba(199,62,58,0.08))] text-[color:var(--cinnabar,#C73E3A)]"
          }`}
        >
          英文原片
        </button>
      </div>

      {/* Video element. key={activeSrc} forces remount on src swap so the
          currentTime restore (in onLoadedMetadata) lands on the new element.
          tabIndex={-1} on duplicates removes them from keyboard tab order
          (paired with the same on tab buttons above). */}
      <div role="tabpanel" className="relative aspect-video bg-black">
        <video
          ref={videoRef}
          key={activeSrc}
          src={activeSrc}
          poster={demo.poster_src}
          controls
          preload="none"
          playsInline
          tabIndex={ariaHidden ? -1 : 0}
          onPlay={handlePlay}
          onLoadedMetadata={handleLoadedMetadata}
          width={demo.natural_width}
          height={demo.natural_height}
          className="h-full w-full"
        >
          您的浏览器不支持 video 标签。
        </video>
      </div>

      {/* Footer — title only. Source attribution (source_label) and time
          range (segment_label) intentionally dropped per user request
          2026-05-01: visitors see them as visual clutter when posters
          already carry the title via the cinnabar-overlay text. The
          display_name stays as a redundant cue for users who don't see
          the poster overlay clearly (small viewports, slow connections,
          or while the video is playing and the poster is gone). The
          fields remain in featured-demos.json + the JSON schema so
          Phase 2 admin UI / future redesigns can resurface them. */}
      <div className="px-4 py-3 text-xs text-muted-foreground">
        <span className="ink-heading text-sm font-semibold text-foreground">{demo.display_name}</span>
      </div>
    </article>
  )
}
