import { SearchX } from "lucide-react"
import { getTranslations } from "next-intl/server"
import { EmptyState } from "@/components/empty-state"

// [locale]/not-found.tsx 在 [locale]/layout.tsx 内渲染（layout 已 setRequestLocale），
// 故 getTranslations 能取到当前 locale → /en 404 出英文 chrome（修 M1-hardening：
// 原内联中文在 /en 泄漏）。actionTo 仍指 /projects?new=1（登录态工作台首页），只本地化 label。
export default async function NotFound() {
  const t = await getTranslations("common")
  return (
    <EmptyState
      icon={SearchX}
      eyebrow={t("notFound.eyebrow")}
      title={t("notFound.title")}
      description={t("notFound.description")}
      actionLabel={t("notFound.actionLabel")}
      actionTo="/projects?new=1"
    />
  )
}
