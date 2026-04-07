"use client"

import Link from "next/link"
import { ShieldCheck } from "lucide-react"
import { buttonVariants } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * Trial entry banner. DESIGN.md §3.5 + T2 preflight: Trial is a state / conversion
 * entry, never a fourth permanent pricing card.
 *
 * Copy boundary: while `trial.frozen === false`, this banner must not tie the
 * trial to a specific plan tier (no "Plus 试用权益" phrasing), must not mention
 * numeric facts (days / minutes / Studio inclusion), and must not sound like an
 * internal placeholder. Only qualitative, trust-led phrasing is allowed here.
 *
 * When the gateway flips `trial.frozen` to true and publishes concrete fields
 * via GET /api/plans, re-introduce a small numeric strip reading from
 * `usePlans()` — not by hardcoding values here.
 */
export function TrialBanner() {
  return (
    <div className="rounded-2xl border border-border bg-card p-6 md:p-8">
      <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-primary">
            <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            <span className="text-xs font-semibold uppercase tracking-wider">
              免费试用
            </span>
          </div>
          <h3 className="text-xl font-semibold text-foreground">
            无需绑卡,先体验再决定
          </h3>
          <p className="zh-body text-muted-foreground max-w-2xl">
            注册即享 7 天试用,含 20 分钟源视频额度与 Studio 精校模式。试用结束不会自动扣费,你的项目数据会一直保留在账户中。
          </p>
        </div>
        <Link
          href="/trial"
          className={cn(
            buttonVariants({ variant: "default", size: "lg" }),
            "h-11 shrink-0 px-6",
          )}
        >
          查看试用说明
        </Link>
      </div>
    </div>
  )
}
