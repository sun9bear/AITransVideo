import { EmptyState } from '@/components/EmptyState'

export function NotFoundPage() {
  return (
    <div className="mx-auto flex min-h-screen max-w-3xl items-center px-4 py-8">
      <EmptyState
        actionLabel="回到新建翻译"
        actionTo="/translations/new"
        description="当前前端阶段只初始化首批 3 个页面路由，其他路径暂不在 MVP 首轮范围内。"
        title="页面尚未纳入本轮范围"
      />
    </div>
  )
}
