"use client"

import { FeaturedDemosProvider } from "./featured-demos-context"
import { FeaturedDemoCard, type Demo } from "./featured-demo-card"

/**
 * FeaturedDemosClient — the "use client" shell that:
 *
 *   1. Provides FeaturedDemosContext (playing-id coordination)
 *   2. Renders the section heading + the duplicated-twice card track
 *   3. Applies the auto-scroll + hover + reduced-motion styling via classes
 *
 * The card list is rendered **twice** in DOM ([...demos, ...demos]) so the
 * CSS `@keyframes featured-demos-marquee` can animate from translateX(0) to
 * translateX(-50%) and the loop seam lines up with an identical second copy.
 * The duplicate copies carry `aria-hidden` + `inert` (set inside the card
 * component) so screen readers and keyboard nav only see the canonical
 * 5 cards.
 *
 * No JS state is required for the visual carousel layer — auto-scroll,
 * pause-on-hover, and hover-shrink-others are all pure CSS using
 * @keyframes + animation-play-state + the :has() selector. See
 * globals.css §"FeaturedDemos carousel" for the rules.
 */

export function FeaturedDemosClient({ demos }: { demos: Demo[] }) {
  // Render demos twice for seamless infinite-loop. The second copy is a
  // visual repeat used only for the keyframe loop point — it's hidden from
  // assistive tech via `aria-hidden` + `inert` set inside the card.
  const doubled = [...demos, ...demos]

  return (
    <section
      id="featured-demos"
      className="canvas-texture marketing-reading-surface py-14 sm:py-16"
    >
      <div className="mx-auto max-w-6xl px-4 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-3xl text-center">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            真实成片样例
          </p>
          <h2 className="ink-display mt-3 text-3xl text-foreground sm:text-4xl">
            听一段实际配音，比看十张截图更有说服力
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            直接听一段——译文是否像人话、配音是否自然、节奏是否对得上原片。
          </p>
        </div>
      </div>

      <FeaturedDemosProvider>
        {/* The track sits OUTSIDE the max-w-6xl wrapper because we want the
            scroll area to extend edge-to-edge. The cards have their own
            internal padding via gap-6 + the section's left padding. */}
        <div className="demo-carousel mt-12 overflow-hidden">
          <div
            className="demo-track flex gap-6 px-4 sm:px-6 lg:px-8"
            // No JS animation — keyframe in globals.css handles the marquee.
          >
            {doubled.map((demo, idx) => (
              <FeaturedDemoCard
                key={`${demo.id}-${idx < demos.length ? "primary" : "duplicate"}`}
                demo={demo}
                ariaHidden={idx >= demos.length}
              />
            ))}
          </div>
        </div>
      </FeaturedDemosProvider>
    </section>
  )
}
