"use client"

import { useState } from "react"
import { useTranslations } from "next-intl"
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

// Non-"free" plan codes render as their proper-noun brand names (not localized);
// the "free" plan label is localized via t("account.planLabel.free").
const PLAN_LABELS: Record<string, string> = {
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
  const t = useTranslations("appSettings")
  const { user, loading } = useSession()
  const router = useRouter()
  const formatLocale = useIntlLocale()

  if (loading) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold">{t("account.title")}</h1>
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
        <h1 className="text-2xl font-bold">{t("account.title")}</h1>
        <p className="text-muted-foreground">{t("account.loginPrompt")}</p>
      </div>
    )
  }

  const planLabel =
    user.plan_code && user.plan_code !== "free"
      ? PLAN_LABELS[user.plan_code] ?? user.plan_code
      : t("account.planLabel.free")

  return (
    <div className="space-y-6 max-w-2xl">
      <h1 className="text-2xl font-bold">{t("account.title")}</h1>

      {/* 个人信息 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("profile.title")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <InfoRow label={t("profile.phone")} value={user.phone_number ? maskPhone(user.phone_number) : t("profile.notBound")} />
          <InfoRow label={t("profile.email")} value={user.email || t("profile.notBound")} />
          <InfoRow label={t("profile.displayName")} value={user.display_name || "—"} />
        </CardContent>
      </Card>

      {/* 绑定邮箱 */}
      {!user.email && <BindEmailCard />}

      {/* 修改密码 */}
      <ChangePasswordCard hasPassword={!!user.email} />

      {/* 套餐信息 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("plan.title")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <InfoRow label={t("plan.current")} value={planLabel} />
          <Link href="/settings/billing">
            <Button variant="outline" size="sm">{t("plan.viewBilling")}</Button>
          </Link>
        </CardContent>
      </Card>

      {/* 账户信息 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("accountInfo.title")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <InfoRow label={t("accountInfo.registeredAt")} value={user.created_at ? new Date(user.created_at).toLocaleDateString(formatLocale) : "—"} />
          <InfoRow label={t("accountInfo.role")} value={user.role === "admin" ? t("accountInfo.roleAdmin") : t("accountInfo.roleUser")} />
          <InfoRow label={t("accountInfo.accountId")} value={user.id.slice(0, 8) + "..."} />
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
              toast.success(t("logout.success"))
              router.push("/auth")
            }}
          >
            {t("logout.button")}
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
  const t = useTranslations("appSettings")
  const [oldPwd, setOldPwd] = useState("")
  const [newPwd, setNewPwd] = useState("")
  const [confirmPwd, setConfirmPwd] = useState("")
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit() {
    if (newPwd.length < 12) {
      toast.error(t("password.tooShort"))
      return
    }
    if (newPwd !== confirmPwd) {
      toast.error(t("password.mismatch"))
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
        throw new Error(data.detail || t("password.changeFailed"))
      }
      toast.success(t("password.changeSuccess"))
      setOldPwd("")
      setNewPwd("")
      setConfirmPwd("")
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t("password.changeFailed"))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{hasPassword ? t("password.changeTitle") : t("password.setTitle")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 max-w-sm">
        {hasPassword && (
          <div className="space-y-1">
            <Label htmlFor="old-pwd">{t("password.current")}</Label>
            <Input id="old-pwd" type="password" value={oldPwd} onChange={(e) => setOldPwd(e.target.value)} />
          </div>
        )}
        <div className="space-y-1">
          <Label htmlFor="new-pwd">{t("password.new")}</Label>
          <Input id="new-pwd" type="password" value={newPwd} onChange={(e) => setNewPwd(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="confirm-pwd">{t("password.confirm")}</Label>
          <Input id="confirm-pwd" type="password" value={confirmPwd} onChange={(e) => setConfirmPwd(e.target.value)} />
        </div>
        <Button size="sm" onClick={handleSubmit} disabled={submitting}>
          {submitting ? t("password.submitting") : t("password.save")}
        </Button>
      </CardContent>
    </Card>
  )
}

function BindEmailCard() {
  const t = useTranslations("appSettings")
  const [email, setEmail] = useState("")
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit() {
    if (!email || !email.includes("@")) {
      toast.error(t("bindEmail.invalidEmail"))
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
        throw new Error(data.detail || t("bindEmail.bindFailed"))
      }
      toast.success(t("bindEmail.bindSuccess"))
      window.location.reload()
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t("bindEmail.bindFailed"))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("bindEmail.title")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 max-w-sm">
        <div className="space-y-1">
          <Label htmlFor="bind-email">{t("bindEmail.addressLabel")}</Label>
          <Input id="bind-email" type="email" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <Button size="sm" onClick={handleSubmit} disabled={submitting}>
          {submitting ? t("bindEmail.binding") : t("bindEmail.bind")}
        </Button>
      </CardContent>
    </Card>
  )
}
