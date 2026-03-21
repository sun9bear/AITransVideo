type ConfigSummaryItem = {
  label: string
  value: string
  hint?: string
}

type ConfigSummaryCardProps = {
  title: string
  description?: string
  items: readonly ConfigSummaryItem[]
}

export function ConfigSummaryCard({
  title,
  description,
  items,
}: ConfigSummaryCardProps) {
  return (
    <section className="surface-card p-5">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold text-ink-950">{title}</h2>
        {description ? <p className="muted-copy">{description}</p> : null}
      </div>

      <dl className="mt-5 space-y-4">
        {items.map((item) => (
          <div
            key={`${item.label}-${item.value}`}
            className="rounded-2xl border border-ink-950/8 bg-sand-50/70 px-4 py-3"
          >
            <dt className="text-xs font-semibold uppercase tracking-[0.18em] text-ink-900/55">
              {item.label}
            </dt>
            <dd className="mt-2 text-sm font-medium text-ink-950">{item.value}</dd>
            {item.hint ? <dd className="mt-1 text-sm text-ink-900/65">{item.hint}</dd> : null}
          </div>
        ))}
      </dl>
    </section>
  )
}
