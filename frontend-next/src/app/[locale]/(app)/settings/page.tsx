"use client"

import { useState } from "react"
import { Link } from "@/i18n/navigation"
import { useRouter } from "@/i18n/navigation"
import { useSession } from "@/components/providers/session-provider"
import { clearAnonConvertReady } from "@/lib/api/claim"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { toast } from "sonner"
import { useIntlLocale } from "@/lib/intl-locale"

const PLAN_LABELS: Record<string, string> = {
  free: "免费版",
  plus: "Plus",
  pro: "Pro",
}

function maskPhone(phone: string): string {
  if (phone.length >= 7) {
    return phone.slice(0, 3) + "****" + phone.slice(-4)
  }
  return phone
}

export default function SettingsPage() {
  const { user, loading } = useSession()
  const router = useRouter()
  const formatLocale = useIntlLocale()

  if (loading) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold">账户设置</h1>
        <div className="animate-pulse space-y-4">
          <div className="h-40 rounded-lg bg-muted" />
          <div className="h-40 rounded-lg bg-muted" />
        </div>
      </div>
    )
  }

  if (!user) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold">账户设置</h1>
        <p className="text-muted-foreground">请先登录。</p>
      </div>
    )
  }

  return (
    <div className="space-y-6 max-w-2xl">
      <h1 className="text-2xl font-bold">账户设置</h1>

      {/* 个人信息 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">个人信息</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <InfoRow label="手机号" value={user.phone_number ? maskPhone(user.phone_number) : "未绑定"} />
          <InfoRow label="邮箱" value={user.email || "未绑定"} />
          <InfoRow label="显示名称" value={user.display_name || "—"} />
        </CardContent>
      </Card>

      {/* 绑定邮箱 */}
      {!user.email && <BindEmailCard />}

      {/* 修改密码 */}
      <ChangePasswordCard hasPassword={!!user.email} />

      {/* 套餐信息 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">套餐信息</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <InfoRow label="当前套餐" value={PLAN_LABELS[user.plan_code ?? "free"] ?? user.plan_code ?? "免费版"} />
          <Link href="/settings/billing">
            <Button variant="outline" size="sm">查看账单与订阅</Button>
          </Link>
        </CardContent>
      </Card>

      {/* 账户信息 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">账户信息</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <InfoRow label="注册时间" value={user.created_at ? new Date(user.created_at).toLocaleDateString(formatLocale) : "—"} />
          <InfoRow label="账户角色" value={user.role === "admin" ? "管理员" : "普通用户"} />
          <InfoRow label="账户 ID" value={user.id.slice(0, 8) + "..."} />
        </CardContent>
      </Card>

      {/* 退出登录 */}
      <Card>
        <CardContent className="pt-6">
          <Button
            variant="destructive"
            onClick={async () => {
              await fetch("/auth/logout", { method: "POST", credentials: "include" })
              clearAnonConvertReady()
              toast.success("已退出登录")
              router.push("/auth")
            }}
          >
            退出登录
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span>{value}</span>
    </div>
  )
}

function ChangePasswordCard({ hasPassword }: { hasPassword: boolean }) {
  const [oldPwd, setOldPwd] = useState("")
  const [newPwd, setNewPwd] = useState("")
  const [confirmPwd, setConfirmPwd] = useState("")
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit() {
    if (newPwd.length < 12) {
      toast.error("新密码长度至少 12 位")
      return
    }
    if (newPwd !== confirmPwd) {
      toast.error("两次输入的密码不一致")
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch("/api/account/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || "修改失败")
      }
      toast.success("密码修改成功")
      setOldPwd("")
      setNewPwd("")
      setConfirmPwd("")
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "修改失败")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{hasPassword ? "修改密码" : "设置密码"}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 max-w-sm">
        {hasPassword && (
          <div className="space-y-1">
            <Label htmlFor="old-pwd">当前密码</Label>
            <Input id="old-pwd" type="password" value={oldPwd} onChange={(e) => setOldPwd(e.target.value)} />
          </div>
        )}
        <div className="space-y-1">
          <Label htmlFor="new-pwd">新密码</Label>
          <Input id="new-pwd" type="password" value={newPwd} onChange={(e) => setNewPwd(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="confirm-pwd">确认新密码</Label>
          <Input id="confirm-pwd" type="password" value={confirmPwd} onChange={(e) => setConfirmPwd(e.target.value)} />
        </div>
        <Button size="sm" onClick={handleSubmit} disabled={submitting}>
          {submitting ? "提交中..." : "保存"}
        </Button>
      </CardContent>
    </Card>
  )
}

function BindEmailCard() {
  const [email, setEmail] = useState("")
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit() {
    if (!email || !email.includes("@")) {
      toast.error("请输入有效的邮箱地址")
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch("/api/account/bind-email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ email }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || "绑定失败")
      }
      toast.success("邮箱绑定成功")
      window.location.reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "绑定失败")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">绑定邮箱</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 max-w-sm">
        <div className="space-y-1">
          <Label htmlFor="bind-email">邮箱地址</Label>
          <Input id="bind-email" type="email" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <Button size="sm" onClick={handleSubmit} disabled={submitting}>
          {submitting ? "绑定中..." : "绑定"}
        </Button>
      </CardContent>
    </Card>
  )
}
