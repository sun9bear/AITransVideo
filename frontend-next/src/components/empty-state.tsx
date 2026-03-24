import Link from "next/link"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"

interface EmptyStateProps {
  title: string
  description?: string
  actionLabel?: string
  actionTo?: string
}

export function EmptyState({ title, description, actionLabel, actionTo }: EmptyStateProps) {
  return (
    <Card className="max-w-md mx-auto mt-20">
      <CardContent className="pt-6 text-center space-y-3">
        <p className="text-xs text-muted-foreground">页面提示</p>
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
