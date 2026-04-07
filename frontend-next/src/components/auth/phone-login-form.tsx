"use client"

import { useEffect, useState } from "react"
import { useSearchParams } from "next/navigation"
import { toast } from "sonner"
import { Phone, KeyRound, Lock } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { CaptchaGate } from "./captcha-gate"

/**
 * Phone verification login / registration form (A1 rewrite).
 *
 * Three steps:
 * 1. Phone + captcha → send code
 * 2. Code input → verify code
 * 3. (NEW users only) Set password → complete registration
 *
 * For existing users, step 2 directly logs in.
 * For new users, step 2 returns a registration_token, and step 3 collects
 * a password before creating the account, granting trial, and logging in.
 */
type Step = "phone" | "code" | "set-password"

export function PhoneLoginForm() {
  const searchParams = useSearchParams()
  const redirectTo = searchParams.get("from") || "/translations/new"

  const [step, setStep] = useState<Step>("phone")
  const [phone, setPhone] = useState("")
  const [captchaToken, setCaptchaToken] = useState<string | null>(null)
  const [code, setCode] = useState("")
  const [registrationToken, setRegistrationToken] = useState<string | null>(null)
  const [password, setPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [resendCountdown, setResendCountdown] = useState(0)

  useEffect(() => {
    if (resendCountdown <= 0) return
    const timer = window.setInterval(() => {
      setResendCountdown((n) => Math.max(0, n - 1))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [resendCountdown])

  const phoneLooksValid = /^(\+?86)?\s*1[3-9]\d{9}$/.test(phone.replace(/[\s\-]/g, ""))

  const handleSendCode = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!phoneLooksValid) {
      toast.error("请输入正确的手机号")
      return
    }
    if (!captchaToken) {
      toast.error("请先完成人机验证")
      return
    }
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
      setStep("code")
      setResendCountdown(60)
    } catch {
      toast.error("网络错误,请重试")
    } finally {
      setSubmitting(false)
    }
  }

  const handleVerifyCode = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!code.trim()) {
      toast.error("请输入验证码")
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch("/auth/phone/verify-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phone_number: phone.trim(),
          code: code.trim(),
        }),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || "验证失败")
        return
      }

      if (data?.needs_password) {
        // NEW user — need to set password before registration is complete.
        setRegistrationToken(data.registration_token)
        setStep("set-password")
        return
      }

      // EXISTING user — already logged in.
      toast.success("登录成功")
      await new Promise((r) => setTimeout(r, 250))
      window.location.replace(redirectTo)
    } catch {
      toast.error("网络错误,请重试")
    } finally {
      setSubmitting(false)
    }
  }

  const handleSetPassword = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!password || password.length < 6) {
      toast.error("密码至少 6 位")
      return
    }
    if (password !== confirmPassword) {
      toast.error("两次密码输入不一致")
      return
    }
    if (!registrationToken) {
      toast.error("注册令牌无效,请重新开始")
      setStep("phone")
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch("/auth/phone/complete-registration", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          registration_token: registrationToken,
          password,
        }),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || "注册失败")
        return
      }
      toast.success("注册成功,欢迎使用")
      await new Promise((r) => setTimeout(r, 250))
      window.location.replace(redirectTo)
    } catch {
      toast.error("网络错误,请重试")
    } finally {
      setSubmitting(false)
    }
  }

  // === Step 1: Phone + captcha ===
  if (step === "phone") {
    return (
      <form onSubmit={handleSendCode} className="space-y-5">
        <div className="space-y-2">
          <Label htmlFor="phone" className="text-sm font-medium text-foreground">
            手机号
          </Label>
          <div className="relative">
            <Phone
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="phone"
              type="tel"
              inputMode="numeric"
              autoComplete="tel"
              placeholder="请输入中国大陆手机号"
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
          type="submit"
          className="h-11 w-full"
          disabled={submitting || !phoneLooksValid || !captchaToken}
        >
          {submitting ? "发送中…" : "发送验证码"}
        </Button>
      </form>
    )
  }

  // === Step 2: Code input ===
  if (step === "code") {
    return (
      <form onSubmit={handleVerifyCode} className="space-y-5">
        <div className="space-y-2">
          <Label htmlFor="code" className="text-sm font-medium text-foreground">
            验证码
          </Label>
          <div className="relative">
            <KeyRound
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="请输入短信验证码"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 8))}
              className="h-11 pl-10 tracking-widest"
              required
            />
          </div>
          <p className="text-xs text-muted-foreground">
            已向 <span className="font-medium text-foreground">{phone}</span> 发送验证码
          </p>
        </div>

        <Button type="submit" className="h-11 w-full" disabled={submitting}>
          {submitting ? "验证中…" : "验证并继续"}
        </Button>

        <div className="flex items-center justify-between text-xs">
          <button
            type="button"
            onClick={() => { setStep("phone"); setCode("") }}
            className="text-muted-foreground hover:text-foreground transition-colors"
            disabled={submitting}
          >
            使用其他手机号
          </button>
          <button
            type="button"
            onClick={async () => {
              if (resendCountdown > 0 || submitting || !captchaToken) return
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
                if (res.ok) {
                  toast.success("已重新发送")
                  setResendCountdown(60)
                }
              } finally {
                setSubmitting(false)
              }
            }}
            className="text-primary hover:text-primary/80 transition-colors disabled:text-muted-foreground"
            disabled={resendCountdown > 0 || submitting}
          >
            {resendCountdown > 0 ? `${resendCountdown}s 后可重发` : "重发验证码"}
          </button>
        </div>
      </form>
    )
  }

  // === Step 3: Set password (NEW users only) ===
  return (
    <form onSubmit={handleSetPassword} className="space-y-5">
      <div className="rounded-md border border-primary/20 bg-primary/5 p-3 text-sm text-foreground">
        <p className="font-medium">设置登录密码</p>
        <p className="mt-1 text-xs text-muted-foreground">
          设置密码后即完成注册,并自动领取免费试用权益。
        </p>
      </div>

      <div className="space-y-2">
        <Label htmlFor="new-password" className="text-sm font-medium text-foreground">
          密码
        </Label>
        <div className="relative">
          <Lock
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="new-password"
            type="password"
            autoComplete="new-password"
            placeholder="至少 6 位"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="h-11 pl-10"
            required
          />
        </div>
      </div>
      <div className="space-y-2">
        <Label htmlFor="confirm-password" className="text-sm font-medium text-foreground">
          确认密码
        </Label>
        <div className="relative">
          <Lock
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="confirm-password"
            type="password"
            autoComplete="new-password"
            placeholder="再次输入密码"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            className="h-11 pl-10"
            required
          />
        </div>
      </div>

      <Button type="submit" className="h-11 w-full" disabled={submitting}>
        {submitting ? "注册中…" : "完成注册"}
      </Button>
    </form>
  )
}
