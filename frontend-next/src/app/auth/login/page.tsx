"use client"

import { Suspense, useState } from "react"
import { useSearchParams } from "next/navigation"
import Link from "next/link"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

function LoginForm() {
  const searchParams = useSearchParams()
  const redirectTo = searchParams.get("from") || "/translations/new"
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [isSubmitting, setIsSubmitting] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!email || !password) {
      toast.error("请填写邮箱和密码")
      return
    }
    setIsSubmitting(true)
    try {
      const res = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
        credentials: "include",
      })
      const data = await res.json()
      if (!res.ok) {
        toast.error(data.detail || "登录失败")
        return
      }
      toast.success("登录成功，正在跳转...")
      await new Promise((r) => setTimeout(r, 300))
      window.location.replace(redirectTo)
    } catch {
      toast.error("网络错误，请重试")
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-5">
      <div className="space-y-2">
        <Label htmlFor="email" className="text-sm text-on-surface-dim">邮箱</Label>
        <Input
          id="email"
          type="email"
          placeholder="your@email.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="h-11 bg-surface-high border-border text-foreground placeholder:text-muted-foreground/50 focus:border-violet-500 focus:ring-violet-500/20"
          required
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor="password" className="text-sm text-on-surface-dim">密码</Label>
        <Input
          id="password"
          type="password"
          placeholder="输入密码"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="h-11 bg-surface-high border-border text-foreground placeholder:text-muted-foreground/50 focus:border-violet-500 focus:ring-violet-500/20"
          required
        />
      </div>
      <Button
        type="submit"
        className="w-full h-11 bg-primary hover:bg-primary/90 text-white font-medium shadow-lg shadow-primary/20 transition-all"
        disabled={isSubmitting}
      >
        {isSubmitting ? "登录中..." : "登录"}
      </Button>
    </form>
  )
}

export default function LoginPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-surface-lowest p-4">
      {/* Background gradient effects */}
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div className="absolute -top-40 -right-40 h-80 w-80 rounded-full bg-violet-500/10 blur-3xl" />
        <div className="absolute -bottom-40 -left-40 h-80 w-80 rounded-full bg-cyan-500/10 blur-3xl" />
      </div>

      <div className="relative w-full max-w-md">
        {/* Logo */}
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 h-14 w-14 rounded-2xl bg-primary flex items-center justify-center shadow-lg shadow-primary/20">
            <span className="text-xl font-bold text-white font-heading">AI</span>
          </div>
          <h1 className="text-2xl font-bold text-on-surface font-heading">AIVideoTrans</h1>
          <p className="mt-1 text-sm text-on-surface-dim">AI 视频翻译配音工作台</p>
        </div>

        {/* Card */}
        <div className="rounded-2xl border border-border bg-surface/80 backdrop-blur-xl p-8 shadow-2xl shadow-black/20">
          <Suspense fallback={null}>
            <LoginForm />
          </Suspense>

          <div className="mt-6 text-center">
            <p className="text-sm text-on-surface-dim">
              还没有账号？{" "}
              <Link href="/auth/register" className="text-violet-400 hover:text-violet-300 transition-colors">
                注册
              </Link>
            </p>
          </div>
        </div>

        {/* Version */}
        <p className="mt-6 text-center text-xs text-on-surface-dim/40">v2.0</p>
      </div>
    </div>
  )
}
