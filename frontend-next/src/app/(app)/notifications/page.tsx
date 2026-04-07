import { Bell } from "lucide-react"
import { EmptyState } from "@/components/empty-state"

export default function NotificationsPage() {
  return (
    <EmptyState
      icon={Bell}
      title="通知中心"
      description="此功能正在开发中，敬请期待。"
    />
  )
}
