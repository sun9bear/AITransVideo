"use client"

import { useState } from "react"
import { Mail, Phone } from "lucide-react"
import { cn } from "@/lib/utils"
import { PhoneLoginForm } from "@/components/auth/phone-login-form"
import { EmailRegisterForm } from "@/components/auth/email-register-form"

type RegisterMode = "phone" | "email"

export function RegisterMethodForm() {
  const [mode, setMode] = useState<RegisterMode>("phone")

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 rounded-lg border border-border bg-muted/30 p-1">
        <button
          type="button"
          onClick={() => setMode("phone")}
          className={cn(
            "inline-flex h-9 items-center justify-center gap-2 rounded-md px-3 text-sm font-medium transition-colors",
            mode === "phone"
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <Phone className="h-4 w-4" aria-hidden="true" />
          手机号
        </button>
        <button
          type="button"
          onClick={() => setMode("email")}
          className={cn(
            "inline-flex h-9 items-center justify-center gap-2 rounded-md px-3 text-sm font-medium transition-colors",
            mode === "email"
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <Mail className="h-4 w-4" aria-hidden="true" />
          邮箱
        </button>
      </div>

      {mode === "phone" ? <PhoneLoginForm /> : <EmailRegisterForm />}
    </div>
  )
}
