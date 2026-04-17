"use client"

import { useState } from "react"
import Link from "next/link"
import { toast } from "sonner"
import { Phone, KeyRound, Lock } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { BrandMark } from "@/components/marketing/brand-mark"
import { CaptchaGate } from "@/components/auth/captcha-gate"

/**
 * Phone-based password reset page.
 *
 * Only supports phone number. Old email-only accounts cannot self-reset
 * (handled manually by admin). This is an intentional product decision
 * to keep the reset flow simple and phone-first.
 */
type Step = "phone" | "reset"

export default function ForgotPasswordPage() {
  const [step, setStep] = useState<Step>("phone")
  const [phone, setPhone] = useState("")
  const [captchaToken, setCaptchaToken] = useState<string | null>(null)
  const [code, setCode] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [submitting, setSubmitting] = useState(false)

  const phoneLooksValid = /^(\+?86)?\s*1[3-9]\d{9}$/.test(phone.replace(/[\s\-]/g, ""))

  const handleSendCode = async () => {
    if (!phoneLooksValid || !captchaToken) return
    setSubmitting(true)
    try {
      const res = await fetch("/auth/phone/send-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phone_number: phone.trim(),
          captcha_token: captchaToken,
        }),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || "验证码发送失败")
        return
      }
      toast.success("验证码已发送")
      setStep("reset")
    } catch {
      toast.error("网络错误")
    } finally {
      setSubmitting(false)
    }
  }

  const handleReset = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!code.trim()) {
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
      const res = await fetch("/auth/phone/reset-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phone_number: phone.trim(),
          code: code.trim(),
          new_password: newPassword,
        }),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || "重置失败")
        return
      }
      toast.success("密码重置成功,已自动登录")
      await new Promise((r) => setTimeout(r, 300))
      window.location.replace("/translations/new")
    } catch {
      toast.error("网络错误")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="w-full max-w-md px-4 py-10">
      <div className="mb-8 text-center">
        <div className="mb-4 flex justify-center">
          <BrandMark size={44} />
        </div>
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">
          找回密码
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          通过手机号验证码重置密码
        </p>
      </div>

      <div className="rounded-2xl border border-border bg-card p-6 shadow-sm sm:p-8">
        {step === "phone" && (
          <div className="space-y-5">
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
            <CaptchaGate
              verified={Boolean(captchaToken)}
              onVerify={(token) => setCaptchaToken(token)}
              disabled={submitting}
            />
            <Button
              type="button"
              className="h-11 w-full"
              disabled={submitting || !phoneLooksValid || !captchaToken}
              onClick={handleSendCode}
            >
              {submitting ? "发送中…" : "发送验证码"}
            </Button>
          </div>
        )}

        {step === "reset" && (
          <form onSubmit={handleReset} className="space-y-5">
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
                  placeholder="请输入短信验证码"
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 8))}
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
              {submitting ? "重置中…" : "重置密码"}
            </Button>
          </form>
        )}

        <div className="mt-6 border-t border-border pt-5 text-center text-xs text-muted-foreground">
          仅支持手机号找回密码。
        </div>
      </div>

      <div className="mt-6 text-center text-sm text-muted-foreground">
        <Link
          href="/auth"
          className="text-primary hover:text-primary/80 transition-colors"
        >
          返回登录
        </Link>
      </div>
    </div>
  )
}
