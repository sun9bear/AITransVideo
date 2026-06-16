"use client"

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react"
import { ChevronLeft, ChevronRight, Volume2, VolumeX } from "lucide-react"
import type { Demo } from "./featured-demo-card"

/**
 * HeroSamplePlayer — first-screen A/B sample player for the marketing hero.
 *
 * Reuses the proven interaction model from `FeaturedDemoCard`:
 *   - currentTime-preserving src swap between dubbed (中文配音) and original
 *     (英文原片) — both clips share the same local 0..duration timeline, so the
 *     position is meaningful after the swap;
 *   - desktop hover-to-play, gated on `matchMedia("(hover: hover)")` so touch
 *     devices (which fire a synthetic mouseenter on tap) are excluded;
 *   - autoplay-policy rejection is caught silently (poster stays, click works).
 *
 * Adds, per the anonymous-preview funnel plan (Phase 1):
 *   - large hero layout (vs the small carousel card);
 *   - translucent overlay A/B toggle (vs the card's tab strip above the video);
 *   - prev / next sample navigation — 上一段 is hidden on the first sample and
 *     appears once the visitor advances, exactly as specced;
 *   - mobile autoplay-in-viewport via IntersectionObserver (muted + playsInline),
 *     respecting prefers-reduced-motion;
 *   - "pause when scrolled out of view" on ALL devices, so the hero stops
 *     playing once the visitor scrolls down to the carousel below;
 *   - an explicit 开启声音 toggle: playback is MUTED by default so hover/mobile
 *     autoplay is reliable (a sound-on autoplay is blocked before any gesture),
 *     and the click to unmute doubles as the gesture that lets sound play.
 *
 * Defaults to the dubbed clip — the product's whole pitch is "你的视频也能这样
 * 开口说中文", so the Chinese dub is what plays first (the visitor taps
 * 开启声音 to hear it).
 */
// Hover-capability media query, read via useSyncExternalStore so SSR and the
// first client render agree (no hydration flash) before the real value applies.
function subscribeHover(callback: () => void) {
  const mq = window.matchMedia("(hover: hover)")
  mq.addEventListener("change", callback)
  return () => mq.removeEventListener("change", callback)
}
function getHoverSnapshot() {
  return window.matchMedia("(hover: hover)").matches
}
function getHoverServerSnapshot() {
  return false
}

export function HeroSamplePlayer({ demos }: { demos: Demo[] }) {
  const [index, setIndex] = useState(0)
  const [tab, setTab] = useState<"dubbed" | "original">("dubbed")

  const videoRef = useRef<HTMLVideoElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  // currentTime stashed across the src-swap remount — restored in
  // onLoadedMetadata. null means "start the new clip from 0" (used on demo nav).
  const pendingResumeTimeRef = useRef<number | null>(null)
  const wasPlayingRef = useRef(false)

  // Hover capability: false during SSR + the first client render (no hydration
  // mismatch), then the real value. Gates desktop hover-play vs mobile in-view
  // autoplay — it does NOT drive sound.
  const canHover = useSyncExternalStore(subscribeHover, getHoverSnapshot, getHoverServerSnapshot)

  // Sound is OFF by default on every device: both hover-play and mobile in-view
  // autoplay need `muted` to satisfy the browser autoplay policy without a user
  // gesture (a sound-on autoplay on first visit is silently blocked). The
  // visitor turns sound on with the overlay button — that click IS the gesture,
  // so playback unmutes reliably.
  const [soundOn, setSoundOn] = useState(false)
  const muted = !soundOn

  const demo = demos[index]
  const activeSrc = tab === "dubbed" ? demo?.dubbed_src : demo?.original_src
  const hasPrev = index > 0
  const hasNext = index < demos.length - 1

  // Keep the <video>.muted DOM *property* in sync. React's `muted` JSX prop is
  // historically unreliable for the property (vs attribute); muted autoplay
  // depends on the property, so we assert it imperatively too.
  useEffect(() => {
    if (videoRef.current) videoRef.current.muted = muted
  }, [muted, index, tab])

  // Viewport coordination. Attach once; the callback reads matchMedia live so
  // there's no stale-state race between this and the hover-detection effect.
  //   - leaving view → pause (every device);
  //   - entering view → muted autoplay on touch devices only, and only when the
  //     visitor hasn't asked for reduced motion. Desktop waits for hover.
  useEffect(() => {
    if (typeof window === "undefined") return
    const el = containerRef.current
    if (!el) return
    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[0]
        const v = videoRef.current
        if (!entry || !v) return
        if (entry.isIntersecting) {
          const isTouch = !window.matchMedia("(hover: hover)").matches
          const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches
          if (isTouch && !reducedMotion) {
            // muted state is already current (sync effect); don't force-mute
            // here or we'd re-mute a visitor who turned sound on.
            void v.play().catch(() => {})
          }
        } else if (!v.paused) {
          v.pause()
        }
      },
      { threshold: 0.5 },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  function handleMouseEnter() {
    if (!canHover) return
    // Muted by default, so this hover-play is allowed without a prior gesture;
    // the catch stays as belt-and-suspenders (e.g. once sound has been turned on).
    void videoRef.current?.play().catch(() => {})
  }

  function handleMouseLeave() {
    if (!canHover) return
    const v = videoRef.current
    if (v && !v.paused) v.pause() // currentTime preserved — next hover resumes
  }

  function toggleSound() {
    const v = videoRef.current
    const next = !soundOn
    setSoundOn(next)
    if (v) {
      v.muted = !next
      // Turning sound on is a user gesture — start playback if paused so the
      // visitor immediately hears the dub.
      if (next && v.paused) void v.play().catch(() => {})
    }
  }

  function handleTabChange(next: "dubbed" | "original") {
    if (next === tab) return
    const v = videoRef.current
    pendingResumeTimeRef.current = v?.currentTime ?? 0 // preserve position across A/B
    wasPlayingRef.current = !!(v && !v.paused)
    setTab(next)
  }

  const changeDemo = useCallback(
    (nextIndex: number) => {
      if (nextIndex < 0 || nextIndex >= demos.length || nextIndex === index) return
      const v = videoRef.current
      wasPlayingRef.current = !!(v && !v.paused)
      pendingResumeTimeRef.current = null // a new sample starts from 0
      setTab("dubbed") // every new sample leads with the dubbed version
      setIndex(nextIndex)
    },
    [demos.length, index],
  )

  function handleLoadedMetadata() {
    const v = videoRef.current
    if (!v) return
    v.muted = muted
    const t = pendingResumeTimeRef.current
    if (t != null) {
      try {
        v.currentTime = t
      } catch {
        /* setting currentTime is safe once metadata is ready (this callback) */
      }
      pendingResumeTimeRef.current = null
    }
    if (wasPlayingRef.current) {
      void v.play().catch(() => {})
      wasPlayingRef.current = false
    }
  }

  if (!demo) return null

  const toggleBase =
    "px-3 py-1.5 text-xs font-medium transition-colors sm:text-sm"
  const toggleActive = "bg-[color:var(--cinnabar,#C73E3A)] text-white"
  const toggleIdle = "text-white/80 hover:text-white"
  const navButton =
    "absolute top-1/2 z-10 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full border border-white/25 bg-black/45 text-white backdrop-blur-md transition hover:bg-black/65 focus-visible:outline-2 focus-visible:outline-white"

  return (
    <div className="w-full">
      <div
        ref={containerRef}
        className="group relative aspect-video w-full overflow-hidden rounded-xl border border-border bg-black shadow-[0_24px_60px_-30px_rgba(26,26,26,0.55)]"
        onMouseEnter={handleMouseEnter}
        onMouseLeave={handleMouseLeave}
      >
        <video
          ref={videoRef}
          key={activeSrc}
          src={activeSrc}
          poster={demo.poster_src}
          controls
          controlsList="nodownload noremoteplayback"
          disablePictureInPicture
          muted={muted}
          playsInline
          preload="metadata"
          onLoadedMetadata={handleLoadedMetadata}
          onContextMenu={(e) => e.preventDefault()}
          width={demo.natural_width}
          height={demo.natural_height}
          className="h-full w-full object-contain"
        >
          您的浏览器不支持 video 标签。
        </video>

        {/* Translucent A/B toggle — overlaid top-right, clear of native controls
            (which sit along the bottom). Default highlights 中文配音. */}
        <div
          role="tablist"
          aria-label="配音版 / 原片切换"
          className="absolute right-3 top-3 z-10 flex overflow-hidden rounded-full border border-white/25 bg-black/45 backdrop-blur-md"
        >
          <button
            type="button"
            role="tab"
            aria-selected={tab === "dubbed"}
            onClick={() => handleTabChange("dubbed")}
            className={`${toggleBase} ${tab === "dubbed" ? toggleActive : toggleIdle}`}
          >
            中文配音
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "original"}
            onClick={() => handleTabChange("original")}
            className={`${toggleBase} ${tab === "original" ? toggleActive : toggleIdle}`}
          >
            英文原片
          </button>
        </div>

        {/* Sound toggle — muted by default for reliable hover/mobile autoplay;
            tap to 试听 the dub (the click satisfies the autoplay policy). */}
        <button
          type="button"
          onClick={toggleSound}
          aria-label={soundOn ? "静音" : "开启声音试听"}
          className="absolute left-3 top-3 z-10 flex items-center gap-1 rounded-full border border-white/25 bg-black/45 px-3 py-1.5 text-xs font-medium text-white backdrop-blur-md transition hover:bg-black/65"
        >
          {soundOn ? (
            <VolumeX className="h-4 w-4" aria-hidden="true" />
          ) : (
            <>
              <Volume2 className="h-4 w-4" aria-hidden="true" />
              开启声音
            </>
          )}
        </button>

        {/* Prev / next sample. 上一段 only appears after advancing past the
            first sample, exactly as specced. */}
        {hasPrev && (
          <button
            type="button"
            aria-label="上一段样本"
            onClick={() => changeDemo(index - 1)}
            className={`${navButton} left-2`}
          >
            <ChevronLeft className="h-5 w-5" aria-hidden="true" />
          </button>
        )}
        {hasNext && (
          <button
            type="button"
            aria-label="下一段样本"
            onClick={() => changeDemo(index + 1)}
            className={`${navButton} right-2`}
          >
            <ChevronRight className="h-5 w-5" aria-hidden="true" />
          </button>
        )}
      </div>

      {/* Caption + position dots below the frame. */}
      <div className="mt-3 flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-foreground">{demo.display_name}</p>
          <p className="truncate text-xs text-muted-foreground">
            {demo.source_label} · {demo.segment_label}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5" aria-hidden="true">
          {demos.map((d, i) => (
            <span
              key={d.id}
              className={`h-1.5 rounded-full transition-all ${
                i === index ? "w-5 bg-[color:var(--cinnabar,#C73E3A)]" : "w-1.5 bg-border"
              }`}
            />
          ))}
        </div>
      </div>
    </div>
  )
}
