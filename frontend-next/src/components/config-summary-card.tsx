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
    <section className="rounded-2xl border border-border bg-card p-5">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold text-foreground">{title}</h2>
        {description ? <p className="text-sm text-muted-foreground">{description}</p> : null}
      </div>

      <dl className="mt-5 space-y-4">
        {items.map((item) => (
          <div
            key={`${item.label}-${item.value}`}
            className="rounded-2xl border border-border bg-muted/30 px-4 py-3"
          >
            <dt className="text-xs font-semibold uppercase tracking-[0.18em] text-muted-foreground">
              {item.label}
            </dt>
            <dd className="mt-2 text-sm font-medium text-foreground">{item.value}</dd>
            {item.hint ? <dd className="mt-1 text-sm text-muted-foreground">{item.hint}</dd> : null}
          </div>
        ))}
      </dl>
    </section>
  )
}
