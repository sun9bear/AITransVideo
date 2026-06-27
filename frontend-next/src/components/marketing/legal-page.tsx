import type { ReactNode } from "react"
import {
  COMPANY_ADDRESS,
  COMPANY_NAME,
  PAYMENT_CHANNEL_NOTE,
  SUPPORT_EMAIL,
  SUPPORT_EMAIL_HREF,
} from "./company-info"

/**
 * Shared shell for long-form legal pages (Terms / Privacy / Refund / Contact).
 * These pages use a neutral reading surface with Chinese-first body copy and a
 * single shared operator-info block at the bottom.
 */
export function LegalPage({
  eyebrow,
  title,
  titleEn,
  updatedAt,
  intro,
  children,
}: {
  eyebrow: string
  title: string
  titleEn: string
  updatedAt: string
  intro?: ReactNode
  children: ReactNode
}) {
  return (
    <>
      <section className="marketing-reading-surface pb-8 pt-16 sm:pb-10 sm:pt-20">
        <div className="mx-auto max-w-3xl px-4 sm:px-6 lg:px-8">
          <p className="ink-heading text-xs uppercase tracking-widest text-[color:var(--cinnabar,#C73E3A)]">
            {eyebrow}
          </p>
          <h1 className="ink-display mt-3 text-4xl tracking-tight text-foreground sm:text-5xl">
            {title}
          </h1>
          <p className="mt-2 text-sm text-muted-foreground">{titleEn}</p>
          <div className="mt-5 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
            <span>最后更新 / Last updated：{updatedAt}</span>
            <span>生效日期 / Effective date：{updatedAt}</span>
          </div>
          {intro ? (
            <div className="mt-8 zh-body text-muted-foreground">{intro}</div>
          ) : null}
        </div>
      </section>

      <section className="marketing-reading-surface pb-20 sm:pb-24">
        <div className="mx-auto max-w-3xl px-4 sm:px-6 lg:px-8">
          <div className="space-y-10">{children}</div>
          <ContactBlock />
        </div>
      </section>
    </>
  )
}

/**
 * One numbered section inside a legal page.
 */
export function LegalSection({
  number,
  title,
  children,
}: {
  number: string
  title: string
  children: ReactNode
}) {
  return (
    <section className="rounded-xl border border-border bg-card p-6 sm:p-8">
      <h2 className="ink-heading flex items-baseline gap-3 text-xl font-semibold text-foreground sm:text-2xl">
        <span className="ink-num text-base font-medium text-[color:var(--cinnabar,#C73E3A)] sm:text-lg">{number}</span>
        <span>{title}</span>
      </h2>
      <div className="mt-4 space-y-4 zh-body text-muted-foreground">{children}</div>
    </section>
  )
}

/**
 * Ordered list with slightly tighter spacing, for enumerated clauses like
 * “（1）… （2）…”.
 */
export function LegalClauseList({ items }: { items: ReactNode[] }) {
  return (
    <ol className="list-none space-y-2 pl-0">
      {items.map((item, idx) => (
        <li key={idx} className="flex gap-3">
          <span className="shrink-0 tabular-nums text-muted-foreground/80">
            （{idx + 1}）
          </span>
          <span className="flex-1">{item}</span>
        </li>
      ))}
    </ol>
  )
}

/**
 * Company / legal entity contact block shown at the bottom of every legal page.
 */
export function ContactBlock() {
  return (
    <div className="mt-12 rounded-xl border border-border/70 bg-muted/40 p-6 sm:p-8">
      <p className="ink-heading text-sm font-semibold text-foreground">运营主体信息 / Operating Entity</p>
      <dl className="mt-4 grid gap-y-2 text-sm text-muted-foreground sm:grid-cols-[auto_1fr] sm:gap-x-6">
        <dt>主体名称 / Entity name</dt>
        <dd className="text-foreground/90">{COMPANY_NAME}</dd>
        <dt>联系邮箱 / Email</dt>
        <dd className="text-foreground/90">
          <a href={SUPPORT_EMAIL_HREF} className="underline-offset-4 hover:underline">
            {SUPPORT_EMAIL}
          </a>
        </dd>
        {COMPANY_ADDRESS ? (
          <>
            <dt>联系地址 / Address</dt>
            <dd className="text-foreground/90">{COMPANY_ADDRESS}</dd>
          </>
        ) : null}
      </dl>
      <p className="mt-6 text-xs leading-6 text-muted-foreground/80">
        {PAYMENT_CHANNEL_NOTE}
      </p>
    </div>
  )
}
