import { BarChart3 } from "lucide-react"
import { EmptyState } from "@/components/empty-state"

export default function UsagePage() {
  return (
    <EmptyState
      icon={BarChart3}
      title="用量统计"
      description="此功能正在开发中，敬请期待。"
    />
  )
}
