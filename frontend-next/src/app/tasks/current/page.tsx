"use client"

import { useEffect, useState } from "react"
import { useRouter } from "next/navigation"

import { EmptyState } from "@/components/empty-state"
import { selectCurrentTaskJob } from "@/features/jobs/selectors"
import { ApiError } from "@/lib/api/client"
import { listJobs } from "@/lib/api/jobs"

export default function CurrentTaskPage() {
  const router = useRouter()
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const jobs = await listJobs()
        if (cancelled) return
        const selectedJob = selectCurrentTaskJob(jobs)
        if (selectedJob) {
          router.replace(`/workspace/${selectedJob.id}`)
        } else {
          setIsLoading(false)
        }
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
    return <EmptyState description="正在查找当前任务..." title="加载中" />
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

function getErrorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return "请求失败，请稍后重试。"
}
