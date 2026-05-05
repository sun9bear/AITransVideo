"use client"

import { Suspense, useState } from "react"
import Link from "next/link"
import { BrandMark } from "@/components/marketing/brand-mark"
import { PasswordLoginForm } from "@/components/auth/password-login-form"
import { PhoneLoginForm } from "@/components/auth/phone-login-form"
import { cn } from "@/lib/utils"

/**
 * Login page at /auth/login — supports password (default) and phone code login.
 */
type LoginMode = "password" | "phone"

export default function LoginPage() {
  const [mode, setMode] = useState<LoginMode>("password")

  return (
    <div className="w-full">
      <div className="mb-6 text-center sm:mb-8">
        <div className="mb-5 flex justify-center">
          <BrandMark size={52} />
        </div>
        <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
          欢迎回来
        </p>
        <h1 className="ink-display mt-2 text-3xl tracking-tight text-foreground sm:text-4xl">
          登录 AITrans.Video
        </h1>
        <p className="mt-3 zh-body text-sm text-muted-foreground">
          {mode === "password" ? "使用账号和密码登录" : "使用手机号验证码登录"}
        </p>
      </div>

      <div className="rounded-2xl border border-border bg-card p-4 shadow-sm sm:p-8">
        {/* Mode toggle */}
        <div className="mb-6 flex rounded-lg border border-border bg-muted/30 p-1">
          <button
            type="button"
            onClick={() => setMode("password")}
            className={cn(
              "flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              mode === "password"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            密码登录
          </button>
          <button
            type="button"
            onClick={() => setMode("phone")}
            className={cn(
              "flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              mode === "phone"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            验证码登录
          </button>
        </div>

        <Suspense fallback={null}>
          {mode === "password" ? <PasswordLoginForm /> : <PhoneLoginForm captchaScenario="login" />}
        </Suspense>

        <div className="mt-6 border-t border-border pt-5 text-center text-sm text-muted-foreground">
          还没有账号？
          <Link href="/auth" className="ml-1 text-primary hover:underline">
            免费注册
          </Link>
        </div>
      </div>
    </div>
  )
}
