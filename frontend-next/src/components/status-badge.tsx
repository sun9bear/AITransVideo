import { JOB_STATUS_LABELS, type JobStatus } from "@/types/jobs"

const statusStyles: Record<string, string> = {
  succeeded: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  failed: "bg-red-500/15 text-red-600 dark:text-red-400",
  cancelled: "bg-muted text-muted-foreground",
  // running uses project secondary color (cyan #06B6D4). Keeps parity with the
  // "正在生成" feel — not the generic blue that doesn't exist in DESIGN.md.
  running: "bg-cyan-500/15 text-cyan-600 dark:text-cyan-400",
  queued: "bg-muted text-muted-foreground",
  waiting_for_review: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  // editing uses project primary color (violet #8B5CF6) to signal a user-owned
  // session that the user must resume or abandon. See plan 2026-04-18 §11.6.
  editing: "bg-violet-500/15 text-violet-600 dark:text-violet-400",
  idle: "bg-muted text-muted-foreground",
  stopping: "bg-muted text-muted-foreground",
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
  const style = statusStyles[status] ?? "bg-muted text-muted-foreground"

  return (
    <span className={`inline-flex rounded-full px-3 py-1 text-xs font-semibold ${style}`}>
      {label}
    </span>
  )
}
