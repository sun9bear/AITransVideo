"use client"

import { useState } from "react"
import Link from "next/link"
import { toast } from "sonner"
import { KeyRound, Lock, Mail, Phone } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { BrandMark } from "@/components/marketing/brand-mark"
import { CaptchaGate } from "@/components/auth/captcha-gate"
import { cn } from "@/lib/utils"

type Method = "phone" | "email"
type Step = "identity" | "reset"

export default function ForgotPasswordPage() {
  const [method, setMethod] = useState<Method>("phone")
  const [step, setStep] = useState<Step>("identity")
  const [phone, setPhone] = useState("")
  const [email, setEmail] = useState("")
  const [captchaToken, setCaptchaToken] = useState<string | null>(null)
  const [code, setCode] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [submitting, setSubmitting] = useState(false)

  const normalizedPhone = phone.trim()
  const normalizedEmail = email.trim()
  const phoneLooksValid = /^(\+?86)?\s*1[3-9]\d{9}$/.test(phone.replace(/[\s\-]/g, ""))
  const emailLooksValid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalizedEmail)
  const identityLooksValid = method === "phone" ? phoneLooksValid : emailLooksValid

  const switchMethod = (next: Method) => {
    if (method === next || submitting) return
    setMethod(next)
    setStep("identity")
    setCaptchaToken(null)
    setCode("")
    setNewPassword("")
    setConfirmPassword("")
  }

  const handleSendCode = async () => {
    if (!identityLooksValid || !captchaToken) return
    setSubmitting(true)
    try {
      const endpoint = method === "phone" ? "/auth/phone/send-code" : "/auth/email/send-reset-code"
      const payload =
        method === "phone"
          ? { phone_number: normalizedPhone, captcha_token: captchaToken }
          : { email: normalizedEmail, captcha_token: captchaToken }
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || "验证码发送失败")
        return
      }
      toast.success(method === "phone" ? "短信验证码已发送" : "邮箱验证码已发送")
      setStep("reset")
    } catch {
      toast.error("网络错误")
    } finally {
      setSubmitting(false)
    }
  }

  const handleReset = async (e: React.FormEvent) => {
    e.preventDefault()
    const cleanCode = code.trim()
    if (!cleanCode) {
      toast.error("请输入验证码")
      return
    }
    if (newPassword.length < 12) {
      toast.error("密码至少 12 位")
      return
    }
    if (newPassword !== confirmPassword) {
      toast.error("两次密码不一致")
      return
    }
    setSubmitting(true)
    try {
      const endpoint = method === "phone" ? "/auth/phone/reset-password" : "/auth/email/reset-password"
      const payload =
        method === "phone"
          ? {
              phone_number: normalizedPhone,
              code: cleanCode,
              new_password: newPassword,
            }
          : {
              email: normalizedEmail,
              code: cleanCode,
              new_password: newPassword,
            }
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || "重置失败")
        return
      }
      toast.success("密码重置成功，已自动登录")
      await new Promise((r) => setTimeout(r, 300))
      window.location.replace("/translations/new")
    } catch {
      toast.error("网络错误")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="w-full">
      <div className="mb-6 text-center sm:mb-8">
        <div className="mb-4 flex justify-center">
          <BrandMark size={44} />
        </div>
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">
          找回密码
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          通过手机号或邮箱验证码重置登录密码
        </p>
      </div>

      <div className="rounded-2xl border border-border bg-card p-4 shadow-sm sm:p-8">
        {step === "identity" && (
          <div className="space-y-5">
            <div className="grid grid-cols-2 rounded-lg border border-border bg-muted/40 p-1">
              <button
                type="button"
                onClick={() => switchMethod("phone")}
                className={cn(
                  "flex h-9 items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors",
                  method === "phone"
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                <Phone className="h-4 w-4" aria-hidden="true" />
                手机号
              </button>
              <button
                type="button"
                onClick={() => switchMethod("email")}
                className={cn(
                  "flex h-9 items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors",
                  method === "email"
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                <Mail className="h-4 w-4" aria-hidden="true" />
                邮箱
              </button>
            </div>

            {method === "phone" ? (
              <div className="space-y-2">
                <Label htmlFor="reset-phone" className="text-sm font-medium text-foreground">
                  手机号
                </Label>
                <div className="relative">
                  <Phone
                    className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                    aria-hidden="true"
                  />
                  <Input
                    id="reset-phone"
                    type="tel"
                    inputMode="numeric"
                    placeholder="请输入注册时使用的手机号"
                    value={phone}
                    onChange={(e) => setPhone(e.target.value)}
                    className="h-11 pl-10"
                    required
                  />
                </div>
              </div>
            ) : (
              <div className="space-y-2">
                <Label htmlFor="reset-email" className="text-sm font-medium text-foreground">
                  邮箱
                </Label>
                <div className="relative">
                  <Mail
                    className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                    aria-hidden="true"
                  />
                  <Input
                    id="reset-email"
                    type="email"
                    inputMode="email"
                    autoComplete="email"
                    placeholder="请输入注册时使用的邮箱"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className="h-11 pl-10"
                    required
                  />
                </div>
              </div>
            )}

            <CaptchaGate
              key={method}
              scenario="login"
              verified={Boolean(captchaToken)}
              onVerify={(token) => setCaptchaToken(token)}
              disabled={submitting}
            />
            <Button
              type="button"
              className="h-11 w-full"
              disabled={submitting || !identityLooksValid || !captchaToken}
              onClick={handleSendCode}
            >
              {submitting ? "发送中..." : "发送验证码"}
            </Button>
          </div>
        )}

        {step === "reset" && (
          <form onSubmit={handleReset} className="space-y-5">
            <div className="rounded-lg border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
              验证码已发送至{" "}
              <span className="font-medium text-foreground">
                {method === "phone" ? normalizedPhone : normalizedEmail}
              </span>
            </div>
            <div className="space-y-2">
              <Label htmlFor="reset-code" className="text-sm font-medium text-foreground">
                验证码
              </Label>
              <div className="relative">
                <KeyRound
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                  aria-hidden="true"
                />
                <Input
                  id="reset-code"
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder={method === "phone" ? "请输入短信验证码" : "请输入邮箱验证码"}
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\s/g, "").slice(0, 16))}
                  className="h-11 pl-10 tracking-widest"
                  required
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="new-pw" className="text-sm font-medium text-foreground">
                新密码
              </Label>
              <div className="relative">
                <Lock
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                  aria-hidden="true"
                />
                <Input
                  id="new-pw"
                  type="password"
                  autoComplete="new-password"
                  placeholder="至少 12 位"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  className="h-11 pl-10"
                  required
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label htmlFor="confirm-pw" className="text-sm font-medium text-foreground">
                确认新密码
              </Label>
              <div className="relative">
                <Lock
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                  aria-hidden="true"
                />
                <Input
                  id="confirm-pw"
                  type="password"
                  autoComplete="new-password"
                  placeholder="再次输入新密码"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  className="h-11 pl-10"
                  required
                />
              </div>
            </div>
            <Button type="submit" className="h-11 w-full" disabled={submitting}>
              {submitting ? "重置中..." : "重置密码"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="h-10 w-full"
              disabled={submitting}
              onClick={() => {
                setStep("identity")
                setCaptchaToken(null)
                setCode("")
              }}
            >
              重新发送验证码
            </Button>
          </form>
        )}

        <div className="mt-6 border-t border-border pt-5 text-center text-xs text-muted-foreground">
          手机号和已验证邮箱均可用于找回密码。
        </div>
      </div>

      <div className="mt-6 text-center text-sm text-muted-foreground">
        <Link
          href="/auth"
          className="text-primary transition-colors hover:text-primary/80"
        >
          返回登录
        </Link>
      </div>
    </div>
  )
}
