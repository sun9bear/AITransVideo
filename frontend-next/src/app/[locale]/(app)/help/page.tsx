/**
 * Help center landing page.
 *
 * Plan 2026-05-08 §13 P1 — replace the "正在开发中" placeholder with a
 * real entry that points users at:
 *
 *   1. The most-asked FAQ topics (links to the marketing FAQ section).
 *   2. The on-page support widget (rendered by AppShell — we just nudge
 *      the user toward it; the floating launcher remains the canonical
 *      entry point).
 *   3. Direct contact channels for billing / privacy / copyright issues.
 *
 * The widget itself is mounted globally by AppShell, so this page does
 * not import SupportWidget directly.
 */

import { Link } from "@/i18n/navigation"

const HELP_TOPICS: Array<{
  title: string
  body: string
  href?: string
}> = [
  {
    title: "试用、套餐、计费",
    body: "了解试用规则、套餐对比、视频时长计算、失败不计费等。",
    href: "/pricing#faq",
  },
  {
    title: "上传视频与授权",
    body: "支持的视频来源（本地 / YouTube / 其他链接）以及授权要求。",
    href: "/#faq",
  },
  {
    title: "Express 与 Studio 模式",
    body: "两种处理模式的差别，什么场景该选哪一个。",
    href: "/#faq",
  },
  {
    title: "下载剪映草稿与素材包",
    body: "在哪里下载、何时重新生成、如何在剪映里继续编辑。",
    href: "/#faq",
  },
  {
    title: "任务失败排查",
    body: "常见失败原因、重试策略、是否扣除额度。",
  },
  {
    title: "退款、版权、隐私",
    body: "这些场景由人工客服直接处理，请通过客服浮窗或邮箱联系。",
    href: "/contact",
  },
]

export default function HelpPage() {
  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <header className="space-y-2">
        <h1 className="ink-display text-2xl text-foreground sm:text-3xl">
          帮助中心
        </h1>
        <p className="text-sm text-muted-foreground">
          常见问题先看下面的话题；如果都没解决，点击右下角「客服」浮窗，
          AI 会先尝试帮你，必要时会转人工。
        </p>
      </header>

      <ul className="grid gap-4 sm:grid-cols-2">
        {HELP_TOPICS.map((topic) => {
          const card = (
            <article className="h-full rounded-xl border border-border bg-card p-4 transition-shadow hover:shadow-md">
              <h2 className="text-base font-semibold text-foreground">
                {topic.title}
              </h2>
              <p className="mt-2 text-sm text-muted-foreground">
                {topic.body}
              </p>
            </article>
          )
          return (
            <li key={topic.title}>
              {topic.href ? (
                <Link href={topic.href} className="block h-full">
                  {card}
                </Link>
              ) : (
                card
              )}
            </li>
          )
        })}
      </ul>

      <section className="rounded-xl border border-border bg-card p-4 text-sm">
        <h2 className="text-base font-semibold text-foreground">
          联系人工客服
        </h2>
        <p className="mt-2 text-muted-foreground">
          点击右下角的「客服」浮窗发起对话，AI 客服解决不了或涉及账单、
          退款、版权、隐私时，会自动创建人工工单，运营会通过邮件回复。
        </p>
        <p className="mt-2 text-muted-foreground">
          也可以直接发邮件到{" "}
          <a
            className="underline-offset-2 hover:underline"
            href="mailto:sxz999@proton.me"
          >
            sxz999@proton.me
          </a>
          。
        </p>
      </section>
    </div>
  )
}
