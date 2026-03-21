import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

type EmptyStateProps = {
  title: string
  description: string
  actionLabel?: string
  actionTo?: string
  children?: ReactNode
}

export function EmptyState({
  title,
  description,
  actionLabel,
  actionTo,
  children,
}: EmptyStateProps) {
  const canRenderAction = Boolean(actionLabel && actionTo)

  return (
    <section className="surface-card border border-dashed border-ink-950/15 p-6 text-center">
      <div className="mx-auto max-w-xl space-y-3">
        <p className="eyebrow">页面提示</p>
        <h2 className="text-xl font-semibold text-ink-950">{title}</h2>
        <p className="muted-copy">{description}</p>
        {children ? <div className="text-sm text-ink-900/70">{children}</div> : null}
        {canRenderAction ? (
          <Link
            className="inline-flex rounded-full bg-ink-950 px-5 py-3 text-sm font-semibold text-white transition hover:bg-ink-900"
            to={actionTo as string}
          >
            {actionLabel}
          </Link>
        ) : null}
      </div>
    </section>
  )
}
