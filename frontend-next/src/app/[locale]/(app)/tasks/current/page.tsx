"use client"

import { useEffect, useState } from "react"
import { useRouter } from "@/i18n/navigation"

import { EmptyState } from "@/components/empty-state"
import { selectCurrentTaskJob } from "@/features/jobs/selectors"
import { getErrorMessage } from '@/lib/api/errors'
import { getJob, listJobs } from "@/lib/api/jobs"
import { ACTIVE_JOB_STATUSES } from "@/types/jobs"

export default function CurrentTaskPage() {
  const router = useRouter()
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        // 1. Try listJobs via gateway (filtered by user)
        const jobs = await listJobs()
        if (cancelled) return
        const selectedJob = selectCurrentTaskJob(jobs)
        if (selectedJob) {
          router.replace(`/workspace/${selectedJob.id}`)
          return
        }

        // 2. Fallback: check localStorage for latest job ID
        const latestJobId = typeof window !== 'undefined'
          ? localStorage.getItem('avt_latest_job_id')
          : null
        if (latestJobId) {
          try {
            const latestJob = await getJob(latestJobId)
            if (cancelled) return
            if (ACTIVE_JOB_STATUSES.includes(latestJob.status)) {
              router.replace(`/workspace/${latestJob.id}`)
              return
            }
          } catch {
            // Job might not exist anymore, clear stale ID
            try { localStorage.removeItem('avt_latest_job_id') } catch {}
          }
        }

        setIsLoading(false)
      } catch (error) {
        if (!cancelled) {
          setPageError(getErrorMessage(error))
          setIsLoading(false)
        }
      }
    }
    void load()
    return () => { cancelled = true }
  }, [router])

  if (isLoading && !pageError) {
    return <EmptyState description="正在查找当前任务…" title="加载中" />
  }

  if (pageError) {
    return (
      <EmptyState
        actionLabel="新建翻译"
        actionTo="/translations/new"
        description={pageError}
        title="无法读取当前任务"
      />
    )
  }

  return (
    <EmptyState
      actionLabel="新建翻译"
      actionTo="/translations/new"
      description="当前还没有任务，先创建一个翻译任务。"
      title="当前没有任务"
    />
  )
}
