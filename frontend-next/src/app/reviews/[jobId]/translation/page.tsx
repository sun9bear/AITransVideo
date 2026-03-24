"use client"

import { useEffect } from 'react'
import { useParams, useRouter } from 'next/navigation'

import { EmptyState } from '@/components/empty-state'

export default function TranslationReviewRedirect() {
  const params = useParams()
  const router = useRouter()
  const jobId = (params.jobId as string) ?? ''

  useEffect(() => {
    if (jobId) {
      router.replace(`/workspace/${jobId}`)
    }
  }, [jobId, router])

  return <EmptyState description="正在跳转到工作区..." title="页面已迁移" />
}
