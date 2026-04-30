import type { CSSProperties } from "react"
import type { StageProgressItem } from "@/types/jobs"

/**
 * Stage progress dots — ink theme.
 *   complete  → bamboo  (success / done)
 *   current   → cinnabar with ring halo (active focus, brand red)
 *   error     → cinnabar with stronger ring (collapses with destructive)
 *   upcoming  → muted gray (inactive)
 *
 * Connectors and labels track the same family. Was hardcoded to
 * cyan-500 / red-500 / primary mix that didn't sit on the ink palette.
 */

type StageState = "complete" | "current" | "error" | "upcoming"

const dotBase =
  "flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold transition-colors"
const labelBase = "text-[10px] leading-tight text-center whitespace-nowrap"
const connectorBase = "h-[2px] w-6 shrink-0 transition-colors"

function dotStyle(state: StageState): CSSProperties {
  switch (state) {
    case "complete":
      return {
        backgroundColor: "var(--bamboo)",
        color: "var(--primary-foreground)",
      }
    case "current":
      return {
        backgroundColor: "var(--cinnabar)",
        color: "var(--primary-foreground)",
        boxShadow: "0 0 0 3px color-mix(in oklab, var(--cinnabar) 30%, transparent)",
      }
    case "error":
      return {
        backgroundColor: "var(--cinnabar)",
        color: "var(--primary-foreground)",
        boxShadow: "0 0 0 3px color-mix(in oklab, var(--cinnabar) 45%, transparent)",
      }
    case "upcoming":
      return {
        backgroundColor: "color-mix(in oklab, var(--muted-foreground) 18%, transparent)",
        color: "var(--muted-foreground)",
      }
  }
}

function connectorStyle(state: StageState): CSSProperties {
  switch (state) {
    case "complete":
      return { backgroundColor: "color-mix(in oklab, var(--bamboo) 60%, transparent)" }
    case "current":
      return { backgroundColor: "color-mix(in oklab, var(--cinnabar) 40%, transparent)" }
    case "error":
      return { backgroundColor: "color-mix(in oklab, var(--cinnabar) 50%, transparent)" }
    case "upcoming":
      return { backgroundColor: "color-mix(in oklab, var(--muted-foreground) 18%, transparent)" }
  }
}

function labelStyle(state: StageState): CSSProperties {
  switch (state) {
    case "complete":
      return { color: "var(--muted-foreground)" }
    case "current":
      return { color: "var(--cinnabar)", fontWeight: 600 }
    case "error":
      return { color: "var(--cinnabar)", fontWeight: 600 }
    case "upcoming":
      return { color: "color-mix(in oklab, var(--muted-foreground) 60%, transparent)" }
  }
}

export function StageProgress({ items }: { items: readonly StageProgressItem[] }) {
  return (
    <div className="flex items-center gap-0 overflow-x-auto py-2">
      {items.map((item, index) => (
        <div key={item.key} className="flex items-center">
          <div className="flex flex-col items-center gap-1 min-w-[60px]">
            <div className={dotBase} style={dotStyle(item.state)} title={item.description}>
              {item.state === "complete" ? <span aria-hidden="true">✓</span> : index + 1}
            </div>
            <span className={labelBase} style={labelStyle(item.state)}>
              {item.label}
            </span>
          </div>
          {index < items.length - 1 ? (
            <div className={connectorBase} style={connectorStyle(item.state)} />
          ) : null}
        </div>
      ))}
    </div>
  )
}
