import { Badge } from "@/components/ui/badge"
import { JOB_STATUS_LABELS, type JobStatus } from "@/types/jobs"

const statusVariants: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  cancelled: "secondary",
  failed: "destructive",
  idle: "outline",
  queued: "outline",
  running: "default",
  succeeded: "default",
  stopping: "secondary",
  waiting_for_review: "default",
}

export function StatusBadge({ status }: { status: string }) {
  const label = JOB_STATUS_LABELS[status as JobStatus] ?? status
  const variant = statusVariants[status] ?? "outline"

  return <Badge variant={variant}>{label}</Badge>
}
