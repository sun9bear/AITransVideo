"use client"

import { useState } from "react"
import { useRouter } from "next/navigation"
import Link from "next/link"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

export default function RegisterPage() {
  const router = useRouter()
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [displayName, setDisplayName] = useState("")
  const [isSubmitting, setIsSubmitting] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!email || !password) {
      toast.error("请填写邮箱和密码")
      return
    }
    if (password.length < 6) {
      toast.error("密码至少 6 位")
      return
    }
    setIsSubmitting(true)
    try {
      const res = await fetch("/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, display_name: displayName }),
        credentials: "include",
      })
      const data = await res.json()
      if (!res.ok) {
        toast.error(data.detail || "注册失败")
        return
      }
      toast.success("注册成功")
      router.push("/translations/new")
    } catch {
      toast.error("网络错误，请重试")
    } finally {
      setIsSubmitting(false)
    }
  }

  const inputClass = "h-11 bg-surface-high border-border text-foreground placeholder:text-muted-foreground/50 focus:border-violet-500 focus:ring-violet-500/20"

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface-lowest p-4">
      {/* Background gradient effects */}
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div className="absolute -top-40 -left-40 h-80 w-80 rounded-full bg-cyan-500/10 blur-3xl" />
        <div className="absolute -bottom-40 -right-40 h-80 w-80 rounded-full bg-violet-500/10 blur-3xl" />
      </div>

      <div className="relative w-full max-w-md">
        {/* Logo */}
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 h-14 w-14 rounded-2xl bg-gradient-to-br from-violet-500 to-cyan-500 flex items-center justify-center shadow-lg shadow-violet-500/25">
            <span className="text-xl font-bold text-white font-heading">AI</span>
          </div>
          <h1 className="text-2xl font-bold text-on-surface font-heading">创建账户</h1>
          <p className="mt-1 text-sm text-on-surface-dim">加入 AIVideoTrans，开始翻译配音</p>
        </div>

        {/* Card */}
        <div className="rounded-2xl border border-border bg-surface/80 backdrop-blur-xl p-8 shadow-2xl shadow-black/20">
          <form onSubmit={handleSubmit} className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="email" className="text-sm text-on-surface-dim">邮箱</Label>
              <Input
                id="email"
                type="email"
                placeholder="your@email.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className={inputClass}
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="display-name" className="text-sm text-on-surface-dim">昵称（可选）</Label>
              <Input
                id="display-name"
                placeholder="你的昵称"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className={inputClass}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password" className="text-sm text-on-surface-dim">密码</Label>
              <Input
                id="password"
                type="password"
                placeholder="至少 6 位"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={inputClass}
                required
              />
            </div>
            <Button
              type="submit"
              className="w-full h-11 bg-gradient-to-r from-violet-600 to-violet-500 hover:from-violet-500 hover:to-violet-400 text-white font-medium shadow-lg shadow-violet-500/25 transition-all"
              disabled={isSubmitting}
            >
              {isSubmitting ? "注册中..." : "注册"}
            </Button>
          </form>

          <div className="mt-6 text-center">
            <p className="text-sm text-on-surface-dim">
              已有账号？{" "}
              <Link href="/auth/login" className="text-violet-400 hover:text-violet-300 transition-colors">
                登录
              </Link>
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
