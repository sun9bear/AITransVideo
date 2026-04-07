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
    <div className="w-full max-w-md px-4 py-10">
      <div className="mb-8 text-center">
        <div className="mb-4 flex justify-center">
          <BrandMark size={44} />
        </div>
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">
          注册 AIVideoTrans
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          使用手机号验证码注册,新用户需设置登录密码
        </p>
      </div>

      <div className="rounded-2xl border border-border bg-card p-6 shadow-sm sm:p-8">
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
