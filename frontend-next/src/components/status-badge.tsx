import { JOB_STATUS_LABELS, type JobStatus } from "@/types/jobs"

const statusStyles: Record<string, string> = {
  succeeded: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  failed: "bg-red-500/15 text-red-600 dark:text-red-400",
  cancelled: "bg-muted text-muted-foreground",
  running: "bg-cyan-500/15 text-cyan-600 dark:text-cyan-400",
  queued: "bg-muted text-muted-foreground",
  waiting_for_review: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  idle: "bg-muted text-muted-foreground",
  stopping: "bg-muted text-muted-foreground",
}

export function StatusBadge({ status }: { status: string }) {
  const label = JOB_STATUS_LABELS[status as JobStatus] ?? status
  const style = statusStyles[status] ?? "bg-muted text-muted-foreground"

  return (
    <span className={`inline-flex rounded-full px-3 py-1 text-xs font-semibold ${style}`}>
      {label}
    </span>
  )
}
