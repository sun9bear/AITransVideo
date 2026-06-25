import { SearchX } from "lucide-react"
import { EmptyState } from "@/components/empty-state"

export default function NotFound() {
  return (
    <EmptyState
      icon={SearchX}
      title="找不到页面"
      description="你访问的页面不存在或已移动，试试回到首页。"
      actionLabel="返回首页"
      actionTo="/projects?new=1"
    />
  )
}
