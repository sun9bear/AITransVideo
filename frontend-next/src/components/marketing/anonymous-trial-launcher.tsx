"use client"

import Link from "next/link"
import { UploadCloud } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { buttonVariants } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { AnonymousTrialPanel } from "./anonymous-trial-panel"

/**
 * AnonymousTrialLauncher — the hero "立即试用" CTA.
 *
 * When NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW === "1", renders the real
 * upload → preview panel (AnonymousTrialPanel). Otherwise shows the
 * existing "即将开放" placeholder so the marketing page stays honest
 * while the feature flag is off.
 */
const ANONYMOUS_PREVIEW_ENABLED =
  process.env.NEXT_PUBLIC_ENABLE_ANONYMOUS_PREVIEW === "1"

export function AnonymousTrialLauncher({ className }: { className?: string }) {
  if (ANONYMOUS_PREVIEW_ENABLED) {
    return <AnonymousTrialPanel className={className} />
  }

  // ── Feature flag OFF: original placeholder ──────────────────────────────
  return (
    <Dialog>
      <DialogTrigger
        className={cn(
          buttonVariants({ variant: "default", size: "lg" }),
          "h-11 px-6 text-base",
          className,
        )}
      >
        立即试用
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-lg">免注册上传试用 · 即将开放</DialogTitle>
          <DialogDescription>
            上传本地视频、看前 3 分钟中文配音预览的免注册流程正在开放中。现在可以先看上方的真实译制样本感受效果。
          </DialogDescription>
        </DialogHeader>

        {/* Illustrative (non-interactive) upload zone — previews the coming
            flow without faking a working upload. */}
        <div
          aria-hidden="true"
          className="flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-border px-4 py-9 text-center opacity-70"
        >
          <UploadCloud className="h-7 w-7 text-[color:var(--cinnabar,#C73E3A)]" />
          <p className="text-sm font-medium text-foreground">上传你的视频（即将开放）</p>
          <p className="text-xs text-muted-foreground">本地视频 · 前 3 分钟预览 · 带水印</p>
        </div>

        <Link
          href="/auth"
          className={cn(buttonVariants({ variant: "default", size: "lg" }), "h-11 w-full")}
        >
          注册账号，开放后第一时间体验
        </Link>
        <p className="text-center text-xs text-muted-foreground">
          上传与实时预览即将上线，敬请期待。
        </p>
      </DialogContent>
    </Dialog>
  )
}
