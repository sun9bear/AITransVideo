import { EmptyState } from "@/components/empty-state"

export default function NotFound() {
  return (
    <EmptyState
      title="页面不存在"
      description="请检查链接是否正确。"
      actionLabel="返回首页"
      actionTo="/translations/new"
    />
  )
}
