"use client"

import { createContext, useCallback, useContext, useState, type ReactNode } from "react"

/**
 * FeaturedDemos — currently-playing-card coordination context.
 *
 * Each <FeaturedDemoCard> registers its id with this context the moment its
 * <video> fires `onPlay`. Other cards subscribe to `currentlyPlayingId` and
 * pause themselves when the id changes to a value other than their own.
 *
 * Design note: we use a single string id (or null) rather than a set, because
 * native browsers only allow one foreground audio stream at a time anyway,
 * and the UX is "starting one card pauses the previous" — the set semantics
 * would just complicate this.
 */

type FeaturedDemosContextValue = {
  currentlyPlayingId: string | null
  setCurrentlyPlayingId: (id: string | null) => void
}

const FeaturedDemosContext = createContext<FeaturedDemosContextValue>({
  currentlyPlayingId: null,
  setCurrentlyPlayingId: () => {},
})

export function FeaturedDemosProvider({ children }: { children: ReactNode }) {
  const [currentlyPlayingId, setCurrentlyPlayingIdState] = useState<string | null>(null)
  // Wrap the setter so we get a stable reference across renders — consumers
  // depend on it inside useEffect; an unstable identity would cause the
  // effect to re-fire each render.
  const setCurrentlyPlayingId = useCallback((id: string | null) => {
    setCurrentlyPlayingIdState(id)
  }, [])
  return (
    <FeaturedDemosContext.Provider value={{ currentlyPlayingId, setCurrentlyPlayingId }}>
      {children}
    </FeaturedDemosContext.Provider>
  )
}

export function useFeaturedDemos() {
  return useContext(FeaturedDemosContext)
}
