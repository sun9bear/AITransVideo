import { JOB_STATUS_LABELS, type JobStatus } from '@/types/jobs'

type StatusBadgeProps = {
  status: JobStatus
}

const statusToneMap = {
  cancelled: 'bg-ink-950/8 text-ink-900/60',
  failed: 'bg-coral-500/12 text-coral-700',
  queued: 'bg-sand-100 text-ink-900/75',
  running: 'bg-sky-500/12 text-sky-700',
  succeeded: 'bg-mint-500/12 text-mint-700',
  waiting_for_review: 'bg-amber-500/15 text-amber-900',
} as const

export function StatusBadge({ status }: StatusBadgeProps) {
  return (
    <span
      className={[
        'inline-flex rounded-full px-3 py-1 text-xs font-semibold',
        statusToneMap[status],
      ].join(' ')}
    >
      {JOB_STATUS_LABELS[status]}
    </span>
  )
}
