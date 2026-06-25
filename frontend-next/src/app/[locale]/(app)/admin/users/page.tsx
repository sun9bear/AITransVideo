"use client"

import { useEffect, useState, useCallback } from "react"
import { toast } from "sonner"
import { Shield, ChevronDown } from "lucide-react"

type AdminUser = {
  id: string
  email: string
  display_name: string
  role: string
  plan_code: string
  free_jobs_quota_total: number
  free_jobs_quota_used: number
  is_active: boolean
  active_jobs: number
  total_jobs: number
  created_at: string | null
}

type AuditEntry = {
  id: string
  admin_email: string
  action: string
  field_name: string
  old_value: string | null
  new_value: string | null
  created_at: string | null
}

const ROLE_OPTIONS = ["user", "admin"] as const
const PLAN_OPTIONS = ["free", "plus", "pro"] as const

// Ink palette: free=muted (no special tier), plus=ochre (mid tier),
// pro=cinnabar (premium / brand color), admin role=cinnabar (authority).
const PLAN_BADGE: Record<string, string> = {
  free: "bg-muted/40 text-muted-foreground",
  plus: "bg-[color:var(--ochre)]/15 text-[color:var(--ochre)]",
  pro: "bg-[color:var(--cinnabar)]/15 text-[color:var(--cinnabar)]",
}

const ROLE_BADGE: Record<string, string> = {
  user: "bg-muted/40 text-muted-foreground",
  admin: "bg-[color:var(--cinnabar)]/15 text-[color:var(--cinnabar)]",
}

export default function AdminUsersPage() {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [loading, setLoading] = useState(true)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editForm, setEditForm] = useState<{
    role: string; plan_code: string; free_jobs_quota_total: number; free_jobs_quota_used: number
  }>({ role: "user", plan_code: "free", free_jobs_quota_total: 5, free_jobs_quota_used: 0 })
  const [saving, setSaving] = useState(false)
  const [auditUserId, setAuditUserId] = useState<string | null>(null)
  const [auditEntries, setAuditEntries] = useState<AuditEntry[]>([])

  const loadUsers = useCallback(async () => {
    try {
      const resp = await fetch("/api/admin/users", { credentials: "include" })
      if (!resp.ok) throw new Error("加载失败")
      const data = await resp.json()
      setUsers(data.users || [])
    } catch {
      toast.error("加载用户列表失败")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadUsers() }, [loadUsers])

  const startEdit = (u: AdminUser) => {
    setEditingId(u.id)
    setEditForm({
      role: u.role,
      plan_code: u.plan_code,
      free_jobs_quota_total: u.free_jobs_quota_total,
      free_jobs_quota_used: u.free_jobs_quota_used,
    })
  }

  const saveEdit = async (userId: string) => {
    setSaving(true)
    try {
      const resp = await fetch(`/api/admin/users/${userId}/entitlements`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(editForm),
      })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}))
        throw new Error(err.detail || "保存失败")
      }
      const data = await resp.json()
      if (data.updated) {
        toast.success(`已更新：${data.changes?.map((c: { field: string; old: string; new: string }) => `${c.field}: ${c.old} → ${c.new}`).join("，")}`)
      } else {
        toast.info("无变更")
      }
      setEditingId(null)
      await loadUsers()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败")
    } finally {
      setSaving(false)
    }
  }

  const loadAudit = async (userId: string) => {
    if (auditUserId === userId) {
      setAuditUserId(null)
      return
    }
    setAuditUserId(userId)
    try {
      const resp = await fetch(`/api/admin/users/${userId}/audit-log`, { credentials: "include" })
      if (!resp.ok) throw new Error()
      const data = await resp.json()
      setAuditEntries(data.entries || [])
    } catch {
      setAuditEntries([])
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <div className="animate-spin h-6 w-6 border-2 border-primary border-t-transparent rounded-full" />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold font-heading text-foreground">用户管理</h1>
        <p className="text-sm text-muted-foreground mt-1">
          查看用户、修改套餐和角色、查看审计日志。共 {users.length} 位用户。
        </p>
      </div>

      <div className="rounded-2xl border border-border bg-card overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/30">
              <th className="text-left px-4 py-3 font-medium text-muted-foreground">用户</th>
              <th className="text-left px-4 py-3 font-medium text-muted-foreground">角色</th>
              <th className="text-left px-4 py-3 font-medium text-muted-foreground">套餐</th>
              <th className="text-left px-4 py-3 font-medium text-muted-foreground">额度</th>
              <th className="text-left px-4 py-3 font-medium text-muted-foreground">任务</th>
              <th className="text-right px-4 py-3 font-medium text-muted-foreground">操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <UserRow
                key={u.id}
                user={u}
                isEditing={editingId === u.id}
                editForm={editForm}
                saving={saving}
                onStartEdit={() => startEdit(u)}
                onCancelEdit={() => setEditingId(null)}
                onSaveEdit={() => saveEdit(u.id)}
                onEditFormChange={setEditForm}
                isAuditOpen={auditUserId === u.id}
                auditEntries={auditEntries}
                onToggleAudit={() => loadAudit(u.id)}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function UserRow({
  user: u, isEditing, editForm, saving,
  onStartEdit, onCancelEdit, onSaveEdit, onEditFormChange,
  isAuditOpen, auditEntries, onToggleAudit,
}: {
  user: AdminUser
  isEditing: boolean
  editForm: { role: string; plan_code: string; free_jobs_quota_total: number; free_jobs_quota_used: number }
  saving: boolean
  onStartEdit: () => void
  onCancelEdit: () => void
  onSaveEdit: () => void
  onEditFormChange: (f: typeof editForm) => void
  isAuditOpen: boolean
  auditEntries: AuditEntry[]
  onToggleAudit: () => void
}) {
  return (
    <>
      <tr className="border-b border-border/50 hover:bg-muted/20 transition">
        <td className="px-4 py-3">
          <div className="font-medium text-foreground">{u.display_name || u.email}</div>
          <div className="text-xs text-muted-foreground">{u.email}</div>
        </td>
        <td className="px-4 py-3">
          {isEditing ? (
            <select
              className="rounded-lg border border-border bg-muted/30 px-2 py-1 text-xs"
              value={editForm.role}
              onChange={(e) => onEditFormChange({ ...editForm, role: e.target.value })}
            >
              {ROLE_OPTIONS.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          ) : (
            <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${ROLE_BADGE[u.role] || ROLE_BADGE.user}`}>
              {u.role === "admin" && <Shield className="h-3 w-3" />}
              {u.role}
            </span>
          )}
        </td>
        <td className="px-4 py-3">
          {isEditing ? (
            <select
              className="rounded-lg border border-border bg-muted/30 px-2 py-1 text-xs"
              value={editForm.plan_code}
              onChange={(e) => onEditFormChange({ ...editForm, plan_code: e.target.value })}
            >
              {PLAN_OPTIONS.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          ) : (
            <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${PLAN_BADGE[u.plan_code] || PLAN_BADGE.free}`}>
              {u.plan_code}
            </span>
          )}
        </td>
        <td className="px-4 py-3 text-xs text-muted-foreground tabular-nums">
          {isEditing ? (
            <div className="flex gap-1 items-center">
              <input
                type="number" min={0}
                className="w-12 rounded border border-border bg-muted/30 px-1 py-0.5 text-xs text-center"
                value={editForm.free_jobs_quota_used}
                onChange={(e) => onEditFormChange({ ...editForm, free_jobs_quota_used: Number(e.target.value) })}
              />
              <span>/</span>
              <input
                type="number" min={0}
                className="w-12 rounded border border-border bg-muted/30 px-1 py-0.5 text-xs text-center"
                value={editForm.free_jobs_quota_total}
                onChange={(e) => onEditFormChange({ ...editForm, free_jobs_quota_total: Number(e.target.value) })}
              />
            </div>
          ) : (
            u.plan_code === "free" ? `${u.free_jobs_quota_used} / ${u.free_jobs_quota_total}` : "-"
          )}
        </td>
        <td className="px-4 py-3 text-xs text-muted-foreground tabular-nums">
          {u.active_jobs > 0 && <span className="text-[color:var(--ochre)]">{u.active_jobs} 活跃</span>}
          {u.active_jobs > 0 && " · "}
          {u.total_jobs} 总计
        </td>
        <td className="px-4 py-3 text-right">
          <div className="flex gap-1.5 justify-end">
            {isEditing ? (
              <>
                <button
                  className="rounded-lg bg-primary/20 px-3 py-1 text-xs font-medium text-primary hover:bg-primary/30 transition disabled:opacity-50"
                  onClick={onSaveEdit}
                  disabled={saving}
                >
                  {saving ? "保存中…" : "保存"}
                </button>
                <button
                  className="rounded-lg border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-muted/30 transition"
                  onClick={onCancelEdit}
                >
                  取消
                </button>
              </>
            ) : (
              <button
                className="rounded-lg border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-muted/30 transition"
                onClick={onStartEdit}
              >
                编辑
              </button>
            )}
            <button
              className="rounded-lg border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-muted/30 transition"
              onClick={onToggleAudit}
              title="审计日志"
            >
              <ChevronDown className={`h-3.5 w-3.5 transition ${isAuditOpen ? "rotate-180" : ""}`} />
            </button>
          </div>
        </td>
      </tr>
      {isAuditOpen && (
        <tr>
          <td colSpan={6} className="px-4 py-3 bg-muted/10">
            {auditEntries.length === 0 ? (
              <p className="text-xs text-muted-foreground">暂无审计记录</p>
            ) : (
              <div className="space-y-1">
                <p className="text-xs font-medium text-muted-foreground mb-2">最近变更记录</p>
                {auditEntries.map((e) => (
                  <div key={e.id} className="flex gap-3 text-xs text-muted-foreground">
                    <span className="text-foreground/60 tabular-nums w-36 shrink-0">
                      {e.created_at ? new Date(e.created_at).toLocaleString("zh-CN") : "-"}
                    </span>
                    <span className="text-foreground/80">{e.admin_email}</span>
                    <span>
                      {e.field_name}: <span className="text-[color:var(--cinnabar)]">{e.old_value ?? "-"}</span>
                      {" → "}
                      <span className="text-[color:var(--bamboo)]">{e.new_value ?? "-"}</span>
                    </span>
                  </div>
                ))}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  )
}
