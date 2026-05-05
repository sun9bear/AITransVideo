"use client"

import { useState } from "react"
import { useSearchParams } from "next/navigation"
import Link from "next/link"
import { toast } from "sonner"
import { User, Lock } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { goToPostAuthRedirect, resolvePostAuthRedirect } from "@/lib/auth/post-auth-redirect"

/**
 * Account + password login form (A1 unified login).
 *
 * The "account" field accepts phone number OR email. Backend resolves both.
 */
export function PasswordLoginForm() {
  const searchParams = useSearchParams()
  const redirectTo = resolvePostAuthRedirect(searchParams)
  const [account, setAccount] = useState("")
  const [password, setPassword] = useState("")
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!account || !password) {
      toast.error("请填写账号和密码")
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: account, password }),
        credentials: "include",
      })
      const data = await res.json()
      if (!res.ok) {
        toast.error(data.detail || "登录失败")
        return
      }
      toast.success("登录成功")
      await goToPostAuthRedirect(redirectTo)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "网络错误,请重试")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      <div className="space-y-2">
        <Label htmlFor="account" className="text-sm font-medium text-foreground">
          账号
        </Label>
        <div className="relative">
          <User
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="account"
            type="text"
            inputMode="text"
            autoComplete="username"
            placeholder="请输入手机号"
            value={account}
            onChange={(e) => setAccount(e.target.value)}
            className="h-11 pl-10"
            required
          />
        </div>
      </div>
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label htmlFor="password" className="text-sm font-medium text-foreground">
            密码
          </Label>
          <Link
            href="/auth/forgot-password"
            className="text-xs text-muted-foreground hover:text-primary transition-colors"
          >
            找回密码
          </Link>
        </div>
        <div className="relative">
          <Lock
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            placeholder="请输入密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="h-11 pl-10"
            required
          />
        </div>
      </div>
      <Button type="submit" className="h-11 w-full" disabled={submitting}>
        {submitting ? "登录中…" : "登录"}
      </Button>
    </form>
  )
}
