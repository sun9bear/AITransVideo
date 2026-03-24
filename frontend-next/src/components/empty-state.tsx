import Link from "next/link"
import { Card, CardContent } from "@/components/ui/card"
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
    <Card className="max-w-md mx-auto mt-20">
      <CardContent className="pt-6 text-center space-y-3">
        {Icon && (
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted">
            <Icon className="h-6 w-6 text-muted-foreground" />
          </div>
        )}
        <h2 className="text-xl font-semibold">{title}</h2>
        {description ? <p className="text-sm text-muted-foreground">{description}</p> : null}
        {actionLabel && actionTo ? (
          <Link
            href={actionTo}
            className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 mt-2"
          >
            {actionLabel}
          </Link>
        ) : null}
      </CardContent>
    </Card>
  )
}
