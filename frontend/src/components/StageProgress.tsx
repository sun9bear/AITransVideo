import type { StageProgressItem } from '@/types/jobs'

type StageProgressProps = {
  items: readonly StageProgressItem[]
}

const stateToneMap = {
  complete: 'border-mint-500/20 bg-mint-500/10 text-ink-950',
  current: 'border-ink-950/15 bg-ink-950 text-white',
  error: 'border-coral-500/30 bg-coral-500/8 text-coral-700',
  upcoming: 'border-ink-950/8 bg-sand-50 text-ink-900/60',
} as const

const stateLabelMap = {
  complete: '已完成',
  current: '进行中',
  error: '已失败',
  upcoming: '待开始',
} as const

export function StageProgress({ items }: StageProgressProps) {
  return (
    <section className="surface-card p-5">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold text-ink-950">阶段进展</h2>
        <p className="muted-copy">只展示用户可理解的处理阶段，不直接暴露内部阶段编号。</p>
      </div>

      <ol className="mt-5 space-y-3">
        {items.map((item, index) => (
          <li
            key={item.key}
            className={[
              'flex gap-4 rounded-2xl border px-4 py-4',
              stateToneMap[item.state],
            ].join(' ')}
          >
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-current/20 bg-white/15 text-sm font-semibold">
              {index + 1}
            </div>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <p className="text-sm font-semibold">{item.label}</p>
                <span className="rounded-full bg-white/15 px-2 py-1 text-[11px]">
                  {stateLabelMap[item.state]}
                </span>
              </div>
              <p className="mt-2 text-sm leading-6 opacity-80">{item.description}</p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  )
}
