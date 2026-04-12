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

function getTimestampValue(value: string | null) {
  if (!value) {
    return 0
  }

  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? 0 : parsed
}
