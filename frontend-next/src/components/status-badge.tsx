import type { CSSProperties } from "react"
import { JOB_STATUS_LABELS, type JobStatus } from "@/types/jobs"

/**
 * Status pill — ink-aesthetic soft style: tinted bg + matching border + same-
 * hue text, inspired by the 23 个克隆 cinnabar pill the user pointed at as the
 * target style. Tied to ink theme tokens so it harmonizes in both ink (light)
 * and ink-dark surfaces; the legacy emerald/red/amber/violet/cyan palette was
 * pulled because those Tailwind named colors don't sit in either ink scope.
 *
 * Color semantics:
 *   succeeded            → bamboo  (success)
 *   running              → ochre   (warm "in progress")
 *   waiting_for_review   → ochre   (attention required)
 *   editing              → ochre   (user-owned active session)
 *   failed               → cinnabar (error / destructive — also brand red)
 *   queued/cancelled/    → muted gray (neutral / past)
 *     purged/idle/stopping
 */

type Tone = "bamboo" | "ochre" | "cinnabar" | "muted"

const STATUS_TONE: Record<string, Tone> = {
  succeeded: "bamboo",
  running: "ochre",
  waiting_for_review: "ochre",
  editing: "ochre",
  failed: "cinnabar",
  cancelled: "muted",
  queued: "muted",
  purged: "muted",
  idle: "muted",
  stopping: "muted",
}

function toneStyle(tone: Tone): CSSProperties {
  if (tone === "muted") {
    return {
      backgroundColor: "color-mix(in oklab, var(--muted-foreground) 12%, transparent)",
      color: "var(--muted-foreground)",
      border: "1px solid color-mix(in oklab, var(--muted-foreground) 25%, transparent)",
    }
  }
  // bamboo / ochre / cinnabar — all defined in both ink theme blocks.
  const cssVar = `var(--${tone})`
  return {
    backgroundColor: `color-mix(in oklab, ${cssVar} 12%, transparent)`,
    color: cssVar,
    border: `1px solid color-mix(in oklab, ${cssVar} 35%, transparent)`,
  }
}

export interface StatusBadgeProps {
  status: string
  /**
   * When `status === 'running'` and `editGeneration > 0`, the badge label is
   * swapped from "处理中" to "重合成中 · 第 N 次修改" (plan D33). No effect for
   * other statuses.
   */
  editGeneration?: number
}

export function StatusBadge({ status, editGeneration }: StatusBadgeProps) {
  let label: string = JOB_STATUS_LABELS[status as JobStatus] ?? status
  if (status === "running" && editGeneration && editGeneration > 0) {
    label = `重合成中 · 第 ${editGeneration} 次修改`
  }
  const tone: Tone = STATUS_TONE[status] ?? "muted"

  return (
    <span
      className="inline-flex shrink-0 items-center rounded-full px-3 py-0.5 text-xs font-medium whitespace-nowrap"
      style={toneStyle(tone)}
    >
      {label}
    </span>
  )
}
