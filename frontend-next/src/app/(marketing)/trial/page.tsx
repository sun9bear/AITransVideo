import type { Metadata } from "next"
import { ShieldCheck, ArrowRight } from "lucide-react"
import { TrialDetails } from "@/components/marketing/trial-details"
import { PrimaryCta } from "@/components/marketing/primary-cta"
import { LinkButton } from "@/components/marketing/link-button"
import { getPlansSafeServer } from "@/lib/billing/get-plans"
import { BreadcrumbJsonLd } from "@/components/seo/breadcrumb-json-ld"
import { absoluteUrl } from "@/lib/seo/site"

const PAGE_DESCRIPTION =
  "免费试用 AITrans.Video 的完整视频翻译配音工作流，无需绑卡，试用结束不会自动扣费。"

export const metadata: Metadata = {
  title: "免费试用 · AITrans.Video",
  description: PAGE_DESCRIPTION,
  alternates: { canonical: "/trial" },
  openGraph: {
    title: "免费试用 · 爱译视频",
    description: PAGE_DESCRIPTION,
    url: absoluteUrl("/trial"),
    type: "website",
  },
}

/**
 * `/trial` — marketing landing page for trial conversion.
 *
 * IMPORTANT: this is NOT the Task 3 phone/SMS auth page. It is a marketing
 * surface whose only job is to explain trial value, establish trust, and hand
 * the visitor off to the existing `/auth/register` flow.
 *
 * Numeric trial facts (days / source_minutes / Studio inclusion) come from the
 * gateway `GET /api/plans` response and are only rendered when frozen === true.
 * The lead paragraph below was previously hardcoded — fixed during the
 * 2026-04-29 marketing redesign so trial copy stays in sync with policy.
 *
 * See: docs/plans/2026-04-29-marketing-redesign-ink-aesthetic.md §1.1 issue 4
 */
export default async function TrialPage() {
  const data = await getPlansSafeServer()
  const trial = data.trial
  const hasNumbers = Boolean(
    trial &&
      trial.frozen &&
      typeof trial.days === "number" &&
      typeof trial.source_minutes === "number",
  )
  const leadParagraph = hasNumbers
    ? `注册即享 ${trial!.days} 天试用，含 ${trial!.source_minutes} 分钟源视频额度${trial!.includes_studio ? "与 Studio 精校模式" : ""}。亲自验证对齐质量与配音自然度。试用结束后不会自动扣费，账户信息和已购点数也会保留下来。`
    : "注册即享免费试用。亲自验证对齐质量与配音自然度。试用结束后不会自动扣费，账户信息和已购点数也会保留下来。"

  return (
    <>
      <BreadcrumbJsonLd
        items={[
          { name: "首页", path: "/" },
          { name: "免费试用", path: "/trial" },
        ]}
      />
      <section className="marketing-reading-surface pt-16 pb-8 sm:pt-20">
        <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
          <div className="max-w-2xl">
            <div className="inline-flex items-center gap-2 rounded-full border border-[color:var(--cinnabar,#C73E3A)]/30 bg-[color:var(--cinnabar,#C73E3A)]/5 px-3 py-1 text-xs font-semibold text-[color:var(--cinnabar,#C73E3A)]">
              <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
              无需绑卡
            </div>
            <h1 className="ink-display mt-5 text-4xl tracking-tight text-foreground sm:text-5xl">
              先免费体验，再决定是否升级
            </h1>
            <p className="mt-5 zh-body-lg text-muted-foreground">{leadParagraph}</p>
          </div>
        </div>
      </section>

      <section className="marketing-reading-surface pb-20 sm:pb-24">
        <div className="mx-auto max-w-5xl px-4 sm:px-6 lg:px-8">
          <div className="grid gap-10 lg:grid-cols-5 lg:gap-16">
            <div className="lg:col-span-3">
              <TrialDetails />
            </div>

            <aside className="lg:col-span-2" aria-label="开始试用">
              <div className="sticky top-24 rounded-2xl border border-border bg-card p-6 shadow-sm">
                <h2 className="ink-heading text-xl font-semibold text-foreground">立即开始</h2>
                <p className="mt-2 text-sm text-muted-foreground">
                  创建账户即可查看并领取你的试用额度，整个过程不超过一分钟。
                </p>
                <div className="mt-6 space-y-3">
                  <PrimaryCta className="w-full" />
                  <LinkButton href="/pricing" variant="outline" className="w-full gap-1.5">
                    先看看定价
                    <ArrowRight className="h-4 w-4" aria-hidden="true" />
                  </LinkButton>
                </div>
                <div className="mt-6 space-y-2 border-t border-border pt-5 text-xs text-muted-foreground">
                  <p>· 无需绑定支付方式</p>
                  <p>· 试用结束不会自动扣费</p>
                  <p>· 账户信息和已购点数始终保留</p>
                </div>
              </div>
            </aside>
          </div>
        </div>
      </section>
    </>
  )
}
