"use client"

import { useEffect, useState } from "react"
import { useTranslations } from "next-intl"
import { useParams } from "next/navigation"
import { useIntlLocale } from "@/lib/intl-locale"
import { Link } from "@/i18n/navigation"
import { EmptyState } from "@/components/empty-state"
import { StatusBadge } from "@/components/status-badge"
import { ResultDownloadList } from "@/components/result-download-list"
import { ResultMediaCard } from "@/components/workspace/ResultMediaCard"
import { SmartPreviewResultCard } from "@/components/workspace/SmartPreviewResultCard"
import { getJobDisplayTitle, getJobSecondaryLabel, getStageLabel } from "@/features/jobs/presentation"
import { getProjectDetail } from "@/lib/api/jobs"
import type { ProjectDetailResource } from "@/types/jobs"
import { ApiError } from "@/lib/api/client"
import { useApiErrorMessage } from "@/lib/api/error-localization"

export default function ProjectDetailPage() {
  const t = useTranslations("app")
  const tp = useTranslations("appProjects")
  const localizeError = useApiErrorMessage()
  const formatLocale = useIntlLocale()
  const params = useParams()
  const jobId = (params.jobId as string)?.trim() ?? ""
  const [detail, setDetail] = useState<ProjectDetailResource | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!jobId) return
    getProjectDetail(jobId)
      .then(setDetail)
      .catch((e) => setError(e instanceof ApiError ? localizeError(e) : tp("detail.loadFailed")))
      .finally(() => setIsLoading(false))
  }, [jobId, tp, localizeError])

  if (isLoading) return <EmptyState title={tp("detail.loading")} description={tp("detail.loadingDescription")} />
  if (error) return <EmptyState title={tp("detail.loadFailed")} description={error} actionLabel={tp("detail.backToList")} actionTo="/projects" />
  if (!detail) return <EmptyState title={tp("detail.notFound")} actionLabel={tp("detail.backToList")} actionTo="/projects" />

  const { job, downloads } = detail
  const availableDownloads = downloads.filter((d) => d.available)

  return (
    <div className="space-y-6">
      {/* Header */}
      <section className="rounded-2xl border border-border bg-card p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-2 min-w-0">
            <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">{tp("detail.projectDetail")}</p>
            <h1 className="text-2xl font-bold font-heading text-foreground truncate">{getJobDisplayTitle(t, job)}</h1>
            <p className="text-sm text-muted-foreground">{getJobSecondaryLabel(t, job)}</p>
            <div className="flex gap-3 text-xs text-muted-foreground/60">
              <span>{tp("detail.stagePrefix")}{getStageLabel(t, job.currentStage)}</span>
              <span>·</span>
              <span>{tp("detail.updatedPrefix")}{new Date(job.updatedAt).toLocaleString(formatLocale)}</span>
            </div>
          </div>
          <StatusBadge status={job.status} />
        </div>
        <div className="mt-5 flex flex-wrap gap-3">
          {(job.status === "waiting_for_review" || job.status === "running" || job.status === "queued") ? (
            <Link className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-primary to-primary/80 px-5 py-2 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110" href={`/workspace/${jobId}`}>
              {tp("detail.enterWorkspace")}
            </Link>
          ) : null}
          <Link className="rounded-lg border border-border bg-muted/30 px-4 py-2 text-sm text-muted-foreground transition hover:bg-muted/60" href="/projects">
            {tp("detail.backToList")}
          </Link>
          <Link className="rounded-lg border border-border bg-muted/30 px-4 py-2 text-sm text-muted-foreground transition hover:bg-muted/60" href="/projects?new=1">
            {tp("detail.newTranslation")}
          </Link>
        </div>
      </section>

      {/* Media player — 对完成/接近完成的 job 独立渲染。
          ResultMediaCard 内部会自行 fetch materials-availability，并在
          hasVideo=false && hasAudio=true 时显示 "生成视频" fallback
          按钮（这对老 Express job 的补救路径至关重要，不能被 downloads
          是否为空 gate 住）。CodeX 评审 P2。 */}
      {job.status === "succeeded" && job.smartPreviewMode === true ? (
        /* 智能版预览：stream-only teaser 播放器 + 转完整 CTA（无下载 / 导出口）。 */
        <SmartPreviewResultCard job={job} />
      ) : job.status === "succeeded" ? (
        <ResultMediaCard jobId={jobId} serviceMode={job.serviceMode} />
      ) : null}

      {/* Downloads — 预览任务 stream-only（后端 403 全部下载），不渲染下载区 / 空状态。 */}
      {job.smartPreviewMode === true ? null : availableDownloads.length > 0 ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
            {tp("detail.moreDownloads")}
          </summary>
          <div className="mt-2">
            <ResultDownloadList items={downloads} serviceMode={job.serviceMode} />
          </div>
        </details>
      ) : job.status === "succeeded" ? (
        <section className="rounded-2xl border border-dashed border-border bg-card/50 p-6 text-center">
          <p className="text-sm text-muted-foreground">{tp("detail.noDownloads")}</p>
        </section>
      ) : null}
    </div>
  )
}
