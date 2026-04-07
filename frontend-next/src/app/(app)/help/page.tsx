import { HelpCircle } from "lucide-react"
import { EmptyState } from "@/components/empty-state"

export default function HelpPage() {
  return (
    <EmptyState
      icon={HelpCircle}
      title="帮助中心"
      description="此功能正在开发中，敬请期待。"
    />
  )
}
