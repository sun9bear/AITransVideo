"use client"

import { Users } from "lucide-react"
import { EmptyState } from "@/components/empty-state"

export default function AdminUsersPage() {
  return (
    <div className="min-h-screen p-6">
      <EmptyState
        icon={Users}
        title="用户管理"
        description="用户列表和权限管理功能即将上线。"
      />
    </div>
  )
}
