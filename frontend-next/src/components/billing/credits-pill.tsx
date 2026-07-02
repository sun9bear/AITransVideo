"use client"

import { useEffect, useState } from "react"
import { useTranslations } from "next-intl"
import { Coins } from "lucide-react"

import { Link } from "@/i18n/navigation"
import { getMyCredits } from "@/lib/billing/get-credits"

/**
 * fe-ux P0-4（plan 2026-07-02 §P0-4）：顶栏常驻余额 pill——让"能付钱"的入口
 * 在任意工作台页面 1 击可达。
 *
 * - fail-quiet：未登录 / 拉取失败时不渲染（getMyCredits 抛裸 Error），不阻塞顶栏。
 * - 刷新时机：mount + window focus（不加高频轮询）。
 * - 低余额高亮用 primary（朱砂）：ink 主题下 --destructive 与 --primary 刻意同值
 *   （globals.css "single warm color principle"），destructive 无区分度。
 * - <sm 降级：常态只显图标；低余额时保留「充值」文字（转化 CTA 不降级）。
 */

// 阈值 = 一次音色克隆的最小动作成本（600 点）。后续可改为读
// /api/voice-selection/pricing 的 voice_clone_cost_credits 或 admin 可配。
const LOW_BALANCE_THRESHOLD = 600

export function CreditsPill({ isAuthenticated }: { isAuthenticated: boolean }) {
  const t = useTranslations("common.creditsPill")
  const [balance, setBalance] = useState<number | null>(null)
  const [status, setStatus] = useState<"loading" | "ready" | "failed">("loading")

  useEffect(() => {
    if (!isAuthenticated) return
    let cancelled = false
    const load = () => {
      getMyCredits()
        .then((c) => {
          if (cancelled) return
          setBalance(c.total_available)
          setStatus("ready")
        })
        .catch(() => {
          // 已经拿到过余额就保留旧值继续展示（focus 刷新失败不闪没）。
          if (cancelled) return
          setStatus((prev) => (prev === "ready" ? prev : "failed"))
        })
    }
    load()
    window.addEventListener("focus", load)
    return () => {
      cancelled = true
      window.removeEventListener("focus", load)
    }
  }, [isAuthenticated])

  if (!isAuthenticated || status === "failed") return null

  if (status === "loading") {
    return <div className="h-7 w-14 shrink-0 animate-pulse rounded-full bg-muted/60" aria-hidden="true" />
  }

  const isLow = balance != null && balance < LOW_BALANCE_THRESHOLD

  return (
    <Link
      href="/settings/billing"
      aria-label={t("aria")}
      className={
        isLow
          ? "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full bg-primary px-3 text-xs font-semibold text-white transition hover:bg-primary/90"
          : "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full border border-border bg-muted/30 px-2.5 text-xs font-medium text-muted-foreground transition hover:border-primary/40 hover:text-foreground"
      }
    >
      <Coins className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      {isLow ? (
        <>
          <span>{t("topUp")}</span>
          <span className="hidden font-normal opacity-90 sm:inline">
            {t("balance", { n: balance ?? 0 })}
          </span>
        </>
      ) : (
        <span className="hidden sm:inline">{t("balance", { n: balance ?? 0 })}</span>
      )}
    </Link>
  )
}
