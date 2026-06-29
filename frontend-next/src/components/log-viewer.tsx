import { useState } from 'react'
import { useTranslations } from 'next-intl'

import { getStageLabel, getUserFacingProgressMessage } from '@/features/jobs/presentation'
import { type JobLogEntry } from '@/types/jobs'

type LogViewerProps = {
  entries: readonly JobLogEntry[]
  title?: string
  description?: string
  emptyMessage?: string
  initialVisibleCount?: number
}

const levelToneMap = {
  error: 'border-red-500/20 bg-red-500/8 text-red-400',
  info: 'border-emerald-500/20 bg-emerald-500/5 text-foreground/80',
  warn: 'border-amber-500/20 bg-amber-500/5 text-amber-400',
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
  const t = useTranslations('app')
  const [isExpanded, setIsExpanded] = useState(false)
  const shouldTruncate = entries.length > initialVisibleCount
  const visibleEntries =
    shouldTruncate && !isExpanded ? entries.slice(-initialVisibleCount) : entries

  return (
    <section className="rounded-2xl border border-border bg-card p-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-foreground">{title}</h2>
          <p className="text-sm text-muted-foreground">{description}</p>
        </div>
        <div className="flex items-center gap-2">
          <span className="rounded-full bg-muted/40 px-3 py-1 text-xs font-semibold text-muted-foreground">
            最近 {visibleEntries.length} / 共 {entries.length} 条
          </span>
          {shouldTruncate ? (
            <button
              className="rounded-full border border-border px-3 py-1 text-xs font-semibold text-muted-foreground transition hover:border-border hover:bg-muted/30"
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
        <div className="mt-5 rounded-xl border border-dashed border-border bg-card/50 px-4 py-5 text-sm text-muted-foreground">
          {emptyMessage}
        </div>
      ) : (
        <div className="mt-5 space-y-3">
          {visibleEntries.map((entry) => (
            <article
              key={entry.id}
              className={[
                'rounded-xl border px-4 py-3',
                levelToneMap[entry.level],
              ].join(' ')}
            >
              <div className="flex flex-wrap items-center gap-2 text-xs font-semibold uppercase tracking-[0.12em] opacity-70">
                <span>{levelLabelMap[entry.level]}</span>
                {entry.stage ? <span>{getStageLabel(t, entry.stage)}</span> : null}
                {entry.status ? <span>{t(`status.${entry.status}`)}</span> : null}
                <span>{entry.createdAt}</span>
              </div>
              <p className="mt-2 text-sm leading-6">
                {getUserFacingProgressMessage(t, entry.message) ?? entry.message}
              </p>
            </article>
          ))}
        </div>
      )}
    </section>
  )
}
