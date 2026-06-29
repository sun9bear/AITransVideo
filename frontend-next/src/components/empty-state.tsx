import { Link } from "@/i18n/navigation"
import type { LucideIcon } from "lucide-react"

interface EmptyStateProps {
  icon?: LucideIcon
  // 顶部 eyebrow 标签。默认中文「页面提示」（zh 字节一致）；本地化调用方（如
  // [locale]/not-found.tsx）传入 t("…") 以在 /en 出英文，避免 eyebrow 在 /en 泄漏中文。
  eyebrow?: string
  title: string
  description?: string
  actionLabel?: string
  actionTo?: string
}

export function EmptyState({ icon: Icon, eyebrow = "页面提示", title, description, actionLabel, actionTo }: EmptyStateProps) {
  return (
    <div className="max-w-md mx-auto mt-20 rounded-2xl border border-border bg-card p-8 text-center space-y-3">
      {Icon ? (
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted">
          <Icon className="h-6 w-6 text-muted-foreground" />
        </div>
      ) : null}
      <p className="text-xs font-medium text-muted-foreground">{eyebrow}</p>
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
