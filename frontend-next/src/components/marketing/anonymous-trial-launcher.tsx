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

/**
 * AnonymousTrialLauncher — the hero "立即试用" CTA.
 *
 * Phase 1 scope (IMPORTANT): the anonymous upload → preview pipeline does NOT
 * exist yet (session, chunked upload, probe, compliance, teaser are Phase 2/3).
 * So this panel is honest about that — it does NOT promise "立即预览" while
 * silently doing nothing. It shows what's coming (an illustrative, non-clickable
 * upload zone marked 即将开放) and offers a REAL next step (register, so the
 * visitor can be first in when it opens). Per the funnel, anonymous intake is
 * LOCAL UPLOAD ONLY — no YouTube field is ever shown to anonymous/Free users.
 *
 * When Phase 2 lands, this becomes the live `anonymous-trial-panel.tsx` (drag &
 * drop, progress, probe, compliance status) and the copy can promise the real
 * "免注册立即预览". See docs/plans/2026-06-01-anonymous-preview-funnel-ux-plan.md.
 */
export function AnonymousTrialLauncher({ className }: { className?: string }) {
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
