"use client"

import { Suspense } from "react"
import Link from "next/link"
import { BrandMark } from "@/components/marketing/brand-mark"
import { PhoneLoginForm } from "@/components/auth/phone-login-form"

/**
 * Registration page at /auth — primary entry for "免费开始试用".
 * Phone verification + set password for new users.
 * Existing users who verify phone are auto-logged in.
 */
export default function RegisterPage() {
  return (
    <div className="w-full">
      <div className="mb-6 flex items-center justify-between">
        <Link href="/" className="inline-flex items-center" aria-label="AITrans.Video 首页">
          <BrandMark size={44} />
        </Link>
        <Link
          href="/auth/login"
          className="inline-flex h-9 items-center justify-center rounded-lg border border-border bg-card px-3 text-sm font-medium text-foreground shadow-sm transition-colors hover:border-primary/50 hover:text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
        >
          登录
        </Link>
      </div>

      <div className="mb-6 text-center sm:mb-8">
        <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
          开始本地化
        </p>
        <h1 className="ink-display mt-2 text-3xl tracking-tight text-foreground sm:text-4xl">
          注册 AITrans.Video
        </h1>
        <p className="mt-3 zh-body text-sm text-muted-foreground">
          使用手机号验证码注册，新用户需设置登录密码
        </p>
      </div>

      <div className="rounded-2xl border border-border bg-card p-4 shadow-sm sm:p-8">
        <Suspense fallback={null}>
          <PhoneLoginForm />
        </Suspense>

        <div className="mt-6 border-t border-border pt-5 text-center text-sm text-muted-foreground">
          已有账号？
          <Link href="/auth/login" className="ml-1 text-primary hover:underline">
            返回登录
          </Link>
        </div>
      </div>
    </div>
  )
}
