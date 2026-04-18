import { ACTIVE_JOB_STATUSES, type JobSummary } from '@/types/jobs'

export function selectCurrentTaskJob(jobs: readonly JobSummary[]) {
  const orderedJobs = [...jobs].sort((left, right) => {
    return getTimestampValue(right.updatedAt ?? right.createdAt) -
      getTimestampValue(left.updatedAt ?? left.createdAt)
  })

  // 优先选活跃任务（running/queued/waiting_for_review）
  const activeJob = orderedJobs.find((job) => ACTIVE_JOB_STATUSES.includes(job.status))
  if (activeJob) return activeJob

  // 没有活跃任务时，选最近 1 小时内更新的任务（避免跳到很久前完成的任务）
  const oneHourAgo = Date.now() - 3600_000
  const recentJob = orderedJobs.find((job) => getTimestampValue(job.updatedAt ?? job.createdAt) > oneHourAgo)
  return recentJob ?? null
}

export function selectActiveTaskJob(jobs: JobSummary[]): JobSummary | null {
  // Only truly active statuses — NO fallback to "recent 1 hour" like selectCurrentTaskJob.
  // Reuses ACTIVE_JOB_STATUSES so `editing` is naturally included (plan 2026-04-18).
  const active = jobs.filter(j => ACTIVE_JOB_STATUSES.includes(j.status))
  if (active.length === 0) return null
  // Priority: editing > waiting_for_review > running > queued, then by most recent update.
  // `editing` is highest because it's a user-initiated session the user should be nudged
  // to resume. See docs/internal/status-touchpoints-2026-04-18.md §0.
  const priorityOrder: readonly JobSummary['status'][] = [
    'editing',
    'waiting_for_review',
    'running',
    'queued',
  ]
  active.sort((a, b) => {
    const pa = priorityOrder.indexOf(a.status)
    const pb = priorityOrder.indexOf(b.status)
    if (pa !== pb) return pa - pb
    return new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime()
  })
  return active[0]
}

function getTimestampValue(value: string | null) {
  if (!value) {
    return 0
  }

  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? 0 : parsed
}
