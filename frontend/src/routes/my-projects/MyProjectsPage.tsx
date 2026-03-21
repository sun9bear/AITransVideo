import { Link } from 'react-router-dom'
import { useState } from 'react'

import { ConfigSummaryCard } from '@/components/ConfigSummaryCard'
import { EmptyState } from '@/components/EmptyState'
import { StatusBadge } from '@/components/StatusBadge'
import { getJobDisplayTitle, getJobSecondaryLabel, getStageLabel } from '@/features/jobs/presentation'
import { ApiError } from '@/lib/api/client'
import { listJobs } from '@/lib/api/jobs'
import { usePollingTask } from '@/lib/react/usePollingTask'
import { ACTIVE_JOB_STATUSES, type JobSummary } from '@/types/jobs'

export function MyProjectsPage() {
  const [jobs, setJobs] = useState<JobSummary[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)

  const loadJobs = async (silent = false) => {
    if (!silent) {
      setIsLoading(true)
    }

    try {
      const nextJobs = await listJobs()
      setJobs(sortJobsByUpdatedAt(nextJobs))
      setPageError(null)
    } catch (error) {
      setPageError(getErrorMessage(error))
    } finally {
      setIsLoading(false)
    }
  }

  usePollingTask(() => loadJobs(!isLoading), {
    intervalMs: 8000,
  })

  if (isLoading && jobs.length === 0 && !pageError) {
    return (
      <EmptyState
        description="正在读取项目列表和最近更新记录。"
        title="我的项目加载中"
      />
    )
  }

  if (pageError && jobs.length === 0) {
    return (
      <EmptyState
        actionLabel="新建翻译"
        actionTo="/translations/new"
        description={pageError}
        title="无法读取我的项目"
      />
    )
  }

  if (jobs.length === 0) {
    return (
      <div className="space-y-6">
        <section className="surface-card p-6 lg:p-8 text-center">
          <div className="mx-auto max-w-2xl space-y-4">
            <p className="eyebrow">欢迎</p>
            <h2 className="section-title">欢迎使用 AIVideoTrans</h2>
            <p className="muted-copy">
              帮助你把外语 YouTube 视频翻译为中文配音版本。从提交链接到拿到成品视频，全程在这个工作台完成。
            </p>
            <Link className="primary-button" to="/translations/new">
              开始第一个翻译
            </Link>
          </div>
        </section>

        <ConfigSummaryCard
          description="完成以下四步即可拿到翻译配音视频。"
          items={[
            {
              label: '第一步',
              value: '填写 YouTube 链接并创建翻译任务',
              hint: '当前支持公开的 YouTube 视频链接。',
            },
            {
              label: '第二步',
              value: '按提示完成审核',
              hint: '系统会在关键节点暂停，请确认说话人、翻译和音色。',
            },
            {
              label: '第三步',
              value: '等待处理完成',
              hint: '配音、对齐和视频合成会自动完成。',
            },
            {
              label: '第四步',
              value: '在项目详情页下载结果',
              hint: '成品视频、配音音频和字幕文件都可以下载。',
            },
          ]}
          title="使用流程"
        />
      </div>
    )
  }

  const latestUpdatedAt = jobs[0]?.updatedAt ?? null
  const activeCount = jobs.filter((job) => ACTIVE_JOB_STATUSES.includes(job.status)).length
  const completedCount = jobs.filter((job) => job.status === 'succeeded').length

  return (
    <div className="space-y-6">
      <section className="surface-card p-6 lg:p-8">
        <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-3">
            <h2 className="section-title">我的项目</h2>
            <p className="muted-copy max-w-3xl">
              这里优先展示项目标题、状态、最近更新时间和结果回看入口。已完成项目优先进入结果与下载，进行中的项目优先回到当前任务继续处理。
            </p>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <MetricTile label="项目总数" value={String(jobs.length)} />
            <MetricTile label="进行中" value={String(activeCount)} />
            <MetricTile label="已完成" value={String(completedCount)} />
            <MetricTile
              label="最近更新"
              value={latestUpdatedAt ? formatDateTime(latestUpdatedAt) : '-'}
            />
          </div>
        </div>
      </section>

      {pageError ? (
        <section className="notice-panel border border-coral-500/20 bg-coral-500/8">
          <p className="text-sm font-semibold text-coral-700">刷新项目列表失败</p>
          <p className="mt-2 text-sm text-coral-700/85">{pageError}</p>
        </section>
      ) : null}

      <section className="space-y-4">
        {jobs.map((job) => {
          const entry = getProjectEntry(job)

          return (
            <article key={job.id} className="surface-card p-5">
              <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                <div className="space-y-3">
                  <div className="flex flex-wrap items-center gap-3">
                    <h3 className="text-lg font-semibold text-ink-950">{getJobDisplayTitle(job)}</h3>
                    <StatusBadge status={job.status} />
                  </div>

                  <div className="space-y-1 text-sm text-ink-900/68">
                    <p>结果可见性：{entry.visibilityLabel}</p>
                    <p>最近更新：{formatDateTime(job.updatedAt)}</p>
                    <p>当前阶段：{getStageLabel(job.currentStage)}</p>
                    <p>{getJobSecondaryLabel(job)}</p>
                  </div>
                </div>

                <div className="flex flex-wrap gap-3">
                  <Link className="primary-button" to={entry.primaryTo}>
                    {entry.primaryLabel}
                  </Link>

                  {entry.secondaryTo ? (
                    <Link className="secondary-button" to={entry.secondaryTo}>
                      {entry.secondaryLabel}
                    </Link>
                  ) : null}
                </div>
              </div>
            </article>
          )
        })}
      </section>
    </div>
  )
}

function MetricTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-3xl border border-ink-950/8 bg-sand-50/75 px-4 py-4">
      <p className="eyebrow">{label}</p>
      <p className="mt-2 text-sm font-semibold text-ink-950">{value}</p>
    </div>
  )
}

function getProjectEntry(job: JobSummary) {
  if (job.status === 'succeeded') {
    return {
      primaryLabel: '查看结果与下载',
      primaryTo: `/projects/${job.id}#result-downloads`,
      secondaryLabel: '查看项目详情',
      secondaryTo: `/projects/${job.id}`,
      visibilityLabel: '主结果已可查看',
    }
  }

  if (job.status === 'waiting_for_review') {
    return {
      primaryLabel: '继续处理任务',
      primaryTo: '/tasks/current',
      secondaryLabel: '查看项目详情',
      secondaryTo: `/projects/${job.id}`,
      visibilityLabel: '等待处理审核',
    }
  }

  if (job.status === 'failed') {
    return {
      primaryLabel: '查看项目详情',
      primaryTo: `/projects/${job.id}`,
      secondaryLabel: null,
      secondaryTo: null,
      visibilityLabel: '可查看失败说明',
    }
  }

  if (ACTIVE_JOB_STATUSES.includes(job.status)) {
    return {
      primaryLabel: '查看当前进度',
      primaryTo: '/tasks/current',
      secondaryLabel: '查看项目详情',
      secondaryTo: `/projects/${job.id}`,
      visibilityLabel: '结果尚未生成',
    }
  }

  return {
    primaryLabel: '查看项目详情',
    primaryTo: `/projects/${job.id}`,
    secondaryLabel: null,
    secondaryTo: null,
    visibilityLabel: '当前没有可下载结果',
  }
}

function sortJobsByUpdatedAt(jobs: JobSummary[]) {
  return [...jobs].sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt))
}

function formatDateTime(value: string) {
  const parsed = new Date(value)

  if (Number.isNaN(parsed.getTime())) {
    return value
  }

  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed)
}

function getErrorMessage(error: unknown) {
  if (error instanceof ApiError) {
    return error.message
  }

  if (error instanceof Error) {
    return error.message
  }

  return '请求失败，请稍后重试。'
}
