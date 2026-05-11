"use client"

import { useState } from "react"
import { useSearchParams } from "next/navigation"
import { toast } from "sonner"
import { KeyRound, Lock, Mail, User } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { CaptchaGate, type CaptchaChallenge } from "./captcha-gate"
import { goToPostAuthRedirect, resolvePostAuthRedirect } from "@/lib/auth/post-auth-redirect"

type Step = "email" | "code" | "password"

export function EmailRegisterForm() {
  const searchParams = useSearchParams()
  const redirectTo = resolvePostAuthRedirect(searchParams)
  const [step, setStep] = useState<Step>("email")
  const [email, setEmail] = useState("")
  const [code, setCode] = useState("")
  const [registrationToken, setRegistrationToken] = useState("")
  const [displayName, setDisplayName] = useState("")
  const [password, setPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [captchaToken, setCaptchaToken] = useState<string | null>(null)
  const [captchaChallenge, setCaptchaChallenge] = useState<CaptchaChallenge | null>(null)
  const [captchaExecuting, setCaptchaExecuting] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  const normalizedEmail = email.trim()
  const emailLooksValid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalizedEmail)

  const resolveCaptchaToken = async () => {
    if (captchaToken) return captchaToken
    if (!captchaChallenge) {
      toast.error("人机验证仍在加载，请稍后再试")
      return null
    }
    setCaptchaExecuting(true)
    try {
      const token = await captchaChallenge.execute()
      setCaptchaToken(token)
      return token
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "人机验证失败，请重试")
      captchaChallenge.reset()
      return null
    } finally {
      setCaptchaExecuting(false)
    }
  }

  const handleSendCode = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!emailLooksValid) {
      toast.error("请输入正确的邮箱地址")
      return
    }
    const token = await resolveCaptchaToken()
    if (!token) return

    setSubmitting(true)
    try {
      const res = await fetch("/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: normalizedEmail,
          captcha_token: token,
        }),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        setCaptchaToken(null)
        captchaChallenge?.reset()
        toast.error(data?.detail || "验证码发送失败")
        return
      }
      toast.success("验证码已发送到邮箱")
      setStep("code")
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "网络错误，请重试")
    } finally {
      setSubmitting(false)
    }
  }

  const handleVerifyCode = async (e: React.FormEvent) => {
    e.preventDefault()
    const cleanCode = code.trim()
    if (!cleanCode) {
      toast.error("请输入邮箱验证码")
      return
    }

    setSubmitting(true)
    try {
      const res = await fetch("/auth/email/verify-registration-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: normalizedEmail,
          code: cleanCode,
        }),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || "邮箱验证失败")
        return
      }
      setRegistrationToken(data.registration_token || "")
      toast.success("邮箱验证通过，请设置登录密码")
      setStep("password")
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "网络错误，请重试")
    } finally {
      setSubmitting(false)
    }
  }

  const handleCompleteRegistration = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!password || password.length < 12) {
      toast.error("密码至少 12 位")
      return
    }
    if (password !== confirmPassword) {
      toast.error("两次密码输入不一致")
      return
    }
    if (!registrationToken) {
      toast.error("邮箱验证已失效，请重新验证")
      setStep("email")
      return
    }

    setSubmitting(true)
    try {
      const res = await fetch("/auth/email/complete-registration", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: normalizedEmail,
          registration_token: registrationToken,
          display_name: displayName.trim(),
          password,
        }),
        credentials: "include",
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data?.detail || "注册失败")
        return
      }
      toast.success("邮箱注册成功，欢迎使用")
      await goToPostAuthRedirect(redirectTo)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "网络错误，请重试")
    } finally {
      setSubmitting(false)
    }
  }

  if (step === "code") {
    return (
      <form onSubmit={handleVerifyCode} className="space-y-5">
        <div className="rounded-lg border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
          验证码已发送至 <span className="font-medium text-foreground">{normalizedEmail}</span>
        </div>

        <div className="space-y-2">
          <Label htmlFor="register-email-code" className="text-sm font-medium text-foreground">
            邮箱验证码
          </Label>
          <div className="relative">
            <KeyRound
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="register-email-code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="请输入邮箱验证码"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\s/g, "").slice(0, 16))}
              className="h-11 pl-10 tracking-widest"
              required
            />
          </div>
        </div>

        <Button type="submit" className="h-11 w-full" disabled={submitting || !code.trim()}>
          {submitting ? "验证中..." : "验证邮箱"}
        </Button>

        <Button
          type="button"
          variant="ghost"
          className="h-10 w-full"
          disabled={submitting}
          onClick={() => {
            setStep("email")
            setCode("")
            setRegistrationToken("")
            setCaptchaToken(null)
            captchaChallenge?.reset()
          }}
        >
          修改邮箱或重新发送
        </Button>
      </form>
    )
  }

  if (step === "password") {
    return (
      <form onSubmit={handleCompleteRegistration} className="space-y-5">
        <div className="rounded-lg border border-border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
          邮箱已验证：<span className="font-medium text-foreground">{normalizedEmail}</span>
        </div>

        <div className="space-y-2">
          <Label htmlFor="register-name" className="text-sm font-medium text-foreground">
            昵称
          </Label>
          <div className="relative">
            <User
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="register-name"
              type="text"
              autoComplete="nickname"
              placeholder="可选"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="h-11 pl-10"
            />
          </div>
        </div>

        <div className="space-y-2">
          <Label htmlFor="register-password" className="text-sm font-medium text-foreground">
            登录密码
          </Label>
          <div className="relative">
            <Lock
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="register-password"
              type="password"
              autoComplete="new-password"
              placeholder="至少 12 位"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="h-11 pl-10"
              required
            />
          </div>
        </div>

        <div className="space-y-2">
          <Label htmlFor="register-confirm-password" className="text-sm font-medium text-foreground">
            确认密码
          </Label>
          <div className="relative">
            <Lock
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="register-confirm-password"
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

        <p className="text-xs leading-relaxed text-muted-foreground">
          邮箱注册会创建普通免费账号；免费试用仍以手机号注册和验证为准。
        </p>

        <Button
          type="submit"
          className="h-11 w-full"
          disabled={submitting || !password || !confirmPassword}
        >
          {submitting ? "注册中..." : "完成注册"}
        </Button>
      </form>
    )
  }

  return (
    <form onSubmit={handleSendCode} className="space-y-5">
      <div className="space-y-2">
        <Label htmlFor="register-email" className="text-sm font-medium text-foreground">
          邮箱
        </Label>
        <div className="relative">
          <Mail
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            id="register-email"
            type="email"
            inputMode="email"
            autoComplete="email"
            placeholder="请输入邮箱地址"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="h-11 pl-10"
            required
          />
        </div>
      </div>

      <CaptchaGate
        scenario="register"
        verified={Boolean(captchaToken)}
        onVerify={(token) => setCaptchaToken(token)}
        onReady={setCaptchaChallenge}
        disabled={submitting}
      />

      <p className="text-xs leading-relaxed text-muted-foreground">
        邮箱注册需要先验证邮箱，验证通过后再设置登录密码。
      </p>

      <Button
        type="submit"
        className="h-11 w-full"
        disabled={
          submitting ||
          captchaExecuting ||
          !emailLooksValid ||
          (!captchaToken && !captchaChallenge)
        }
      >
        {captchaExecuting ? "验证中..." : submitting ? "发送中..." : "发送邮箱验证码"}
      </Button>
    </form>
  )
}
