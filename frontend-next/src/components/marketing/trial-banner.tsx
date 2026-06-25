import { Link } from "@/i18n/navigation"
import { ShieldCheck } from "lucide-react"
import { buttonVariants } from "@/components/ui/button-variants"
import { cn } from "@/lib/utils"
import { getPlansSafeServer } from "@/lib/billing/get-plans"

/**
 * Trial entry banner — Server Component.
 *
 * DESIGN.md §3.5 + plan §7: Trial is a state / conversion entry, never a
 * fourth permanent pricing card. This banner shows above PricingGrid in the
 * homepage `PricingPreview` section.
 *
 * Why Server Component:
 *   The previous version was `"use client"` and **hardcoded** "7 天" /
 *   "20 分钟" / "Studio 精校模式" inline. That contradicted the file's own
 *   doc comment and let the marketing copy drift from gateway truth. This
 *   version reads `trial.days` / `trial.source_minutes` / `trial.includes_studio`
 *   from `getPlansSafeServer()` and renders them in the initial HTML.
 *
 *   On gateway failure, falls back to a copy-only message that stays
 *   trust-led but omits specific numbers — preferable to drifted lies.
 *
 * Frozen-state semantics:
 *   While `trial.frozen` is true (current production state), concrete numbers
 *   are committed to publicly. Once gateway flips frozen=false, this banner
 *   should switch to qualitative phrasing — see plan §9 O6 for verification.
 */
export async function TrialBanner() {
  const data = await getPlansSafeServer()
  const trial = data.trial

  const hasNumbers = Boolean(
    trial &&
      trial.frozen &&
      typeof trial.days === "number" &&
      typeof trial.source_minutes === "number",
  )

  // Description text — numbered when gateway gave us frozen facts, otherwise
  // qualitative-only. Either branch keeps the same trust-led tone.
  // Retention phrasing: project files default to 7-day retention (see
  // TrustBanner). Account-level info — login identity, history records, and
  // any purchased credits — persist indefinitely. Earlier copy conflated the
  // two ("项目数据会一直保留") which read inconsistently with the 7-day
  // promise; corrected per user note 2026-05-01.
  const description = hasNumbers
    ? `注册即享 ${trial!.days} 天试用，含 ${trial!.source_minutes} 分钟源视频额度${trial!.includes_studio ? "与 Studio 精校模式" : ""}。试用结束不会自动扣费，你的账户信息和已购点数会一直保留。`
    : "注册即享免费试用，亲自验证对齐质量与配音自然度。试用结束不会自动扣费，你的账户信息和已购点数会一直保留。"

  return (
    <div className="rounded-2xl border border-border bg-card p-6 md:p-8">
      <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div className="space-y-2">
          <div
            className="flex items-center gap-2"
            style={{ color: "var(--cinnabar)" }}
          >
            <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            <span className="ink-heading text-xs font-semibold uppercase tracking-widest">
              免费试用
            </span>
          </div>
          <h3 className="ink-heading text-xl font-semibold text-foreground">
            无需绑卡，先体验再决定
          </h3>
          <p className="zh-body text-muted-foreground max-w-2xl">{description}</p>
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
