import type { StageProgressItem } from '@/types/jobs'

type StageProgressProps = {
  items: readonly StageProgressItem[]
}

const stateStyles = {
  complete: 'bg-emerald-500 text-white',
  current: 'bg-ink-950 text-white ring-2 ring-ink-950/30',
  error: 'bg-coral-500 text-white',
  upcoming: 'bg-sand-200 text-ink-900/50',
} as const

const connectorStyles = {
  complete: 'bg-emerald-400',
  current: 'bg-ink-950/30',
  error: 'bg-coral-400',
  upcoming: 'bg-sand-200',
} as const

const labelStyles = {
  complete: 'text-ink-900/70',
  current: 'text-ink-950 font-semibold',
  error: 'text-coral-700 font-semibold',
  upcoming: 'text-ink-900/40',
} as const

export function StageProgress({ items }: StageProgressProps) {
  return (
    <section className="surface-card px-5 py-4">
      <div className="flex items-center gap-0 overflow-x-auto">
        {items.map((item, index) => (
          <div key={item.key} className="flex items-center">
            {/* Step circle + label */}
            <div className="flex flex-col items-center gap-1 min-w-[60px]">
              <div
                className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${stateStyles[item.state]}`}
                title={item.description}
              >
                {item.state === 'complete' ? '✓' : index + 1}
              </div>
              <span className={`text-[10px] leading-tight text-center whitespace-nowrap ${labelStyles[item.state]}`}>
                {item.label}
              </span>
            </div>
            {/* Connector line */}
            {index < items.length - 1 ? (
              <div className={`h-[2px] w-6 shrink-0 ${connectorStyles[item.state]}`} />
            ) : null}
          </div>
        ))}
      </div>
    </section>
  )
}
