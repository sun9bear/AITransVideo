import { useState } from 'react'

import { getStageLabel, getUserFacingProgressMessage } from '@/features/jobs/presentation'
import { JOB_STATUS_LABELS, type JobLogEntry } from '@/types/jobs'

type LogViewerProps = {
  entries: readonly JobLogEntry[]
  title?: string
  description?: string
  emptyMessage?: string
  initialVisibleCount?: number
}

const levelToneMap = {
  error: 'border-coral-500/30 bg-coral-500/8 text-coral-700',
  info: 'border-mint-500/20 bg-mint-500/8 text-ink-950',
  warn: 'border-amber-500/25 bg-amber-500/10 text-amber-900',
} as const

const levelLabelMap = {
  error: '错误',
  info: '信息',
  warn: '提醒',
} as const

export function LogViewer({
  entries,
  title = '关键进展',
  description = '默认只展示最近关键进展，完整日志可按需展开查看。',
  emptyMessage = '当前没有可显示的日志。',
  initialVisibleCount = 5,
}: LogViewerProps) {
  const [isExpanded, setIsExpanded] = useState(false)
  const shouldTruncate = entries.length > initialVisibleCount
  const visibleEntries =
    shouldTruncate && !isExpanded ? entries.slice(-initialVisibleCount) : entries

  return (
    <section className="surface-card p-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-ink-950">{title}</h2>
          <p className="muted-copy">{description}</p>
        </div>
        <div className="flex items-center gap-2">
          <span className="rounded-full bg-sand-100 px-3 py-1 text-xs font-semibold text-ink-900/65">
            最近 {visibleEntries.length} / 共 {entries.length} 条
          </span>
          {shouldTruncate ? (
            <button
              className="rounded-full border border-ink-950/10 px-3 py-1 text-xs font-semibold text-ink-900 transition hover:border-ink-950/30 hover:bg-sand-100"
              onClick={() => {
                setIsExpanded((currentValue) => !currentValue)
              }}
              type="button"
            >
              {isExpanded ? '收起' : '展开'}
            </button>
          ) : null}
        </div>
      </div>

      {visibleEntries.length === 0 ? (
        <div className="mt-5 rounded-2xl border border-dashed border-ink-950/12 bg-sand-50/80 px-4 py-5 text-sm text-ink-900/65">
          {emptyMessage}
        </div>
      ) : (
        <div className="mt-5 space-y-3">
          {visibleEntries.map((entry) => (
            <article
              key={entry.id}
              className={[
                'rounded-2xl border px-4 py-3',
                levelToneMap[entry.level],
              ].join(' ')}
            >
              <div className="flex flex-wrap items-center gap-2 text-xs font-semibold uppercase tracking-[0.12em]">
                <span>{levelLabelMap[entry.level]}</span>
                {entry.stage ? <span>{getStageLabel(entry.stage)}</span> : null}
                {entry.status ? <span>{JOB_STATUS_LABELS[entry.status]}</span> : null}
                <span>{entry.createdAt}</span>
              </div>
              <p className="mt-2 text-sm leading-6">
                {getUserFacingProgressMessage(entry.message) ?? entry.message}
              </p>
            </article>
          ))}
        </div>
      )}
    </section>
  )
}
