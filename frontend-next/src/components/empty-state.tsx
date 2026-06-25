import { Link } from "@/i18n/navigation"
import type { LucideIcon } from "lucide-react"

interface EmptyStateProps {
  icon?: LucideIcon
  title: string
  description?: string
  actionLabel?: string
  actionTo?: string
}

export function EmptyState({ icon: Icon, title, description, actionLabel, actionTo }: EmptyStateProps) {
  return (
    <div className="max-w-md mx-auto mt-20 rounded-2xl border border-border bg-card p-8 text-center space-y-3">
      {Icon ? (
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted">
          <Icon className="h-6 w-6 text-muted-foreground" />
        </div>
      ) : null}
      <p className="text-xs font-medium text-muted-foreground">页面提示</p>
      <h2 className="text-xl font-semibold text-foreground">{title}</h2>
      {description ? <p className="text-sm text-muted-foreground">{description}</p> : null}
      {actionLabel && actionTo ? (
        <Link
          href={actionTo}
          className="inline-flex items-center justify-center rounded-full bg-gradient-to-r from-primary to-primary/80 px-5 py-2 text-sm font-semibold text-white shadow-lg shadow-primary/25 transition hover:shadow-primary/40 hover:brightness-110 mt-2"
        >
          {actionLabel}
        </Link>
      ) : null}
    </div>
  )
}
