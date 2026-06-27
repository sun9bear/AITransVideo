import { Link } from "@/i18n/navigation"
import { getTranslations } from "next-intl/server"
import {
  ArrowRight,
  CircleCheckBig,
  CreditCard,
  FolderOutput,
  ShieldCheck,
} from "lucide-react"

/**
 * i18n (UI-03b): copy lives in messages/{zh,en}/marketing.json under
 * `marketing.pricingAssurance`. The `PAYMENT_CHANNEL_NOTE` and
 * `DIGITAL_DELIVERABLES` constants previously imported from `company-info.ts`
 * are dictionary-ized at this consumption point (UI-03a handover): they are
 * user-facing prose, not identity fields. `company-info.ts` keeps the
 * identity-only constants (COMPANY_NAME / SUPPORT_EMAIL / COMPANY_ADDRESS),
 * which are content and stay verbatim.
 */
export async function PricingAssurance() {
  const t = await getTranslations("marketing.pricingAssurance")
  const deliverables = t.raw("deliverables") as string[]
  const rules = t.raw("rules") as string[]

  return (
    <section className="mt-12 rounded-3xl border border-border bg-muted/40 p-6 sm:p-8">
      <div className="grid gap-8 lg:grid-cols-[1.15fr_0.85fr]">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider text-primary">
            {t("eyebrow")}
          </p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
            {t("heading")}
          </h2>
          <p className="mt-4 zh-body text-muted-foreground">
            {t("lead")}
          </p>

          <div className="mt-6 grid gap-3 sm:grid-cols-2">
            {deliverables.map((item) => (
              <div
                key={item}
                className="flex items-center gap-3 rounded-2xl border border-border bg-card px-4 py-3 text-sm text-foreground"
              >
                <CircleCheckBig className="h-4 w-4 shrink-0 text-primary" />
                <span>{item}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="space-y-4">
          <div className="rounded-2xl border border-border bg-card p-5">
            <div className="flex items-center gap-3">
              <span className="rounded-full bg-primary/10 p-2 text-primary">
                <CreditCard className="h-4 w-4" />
              </span>
              <h3 className="text-base font-semibold text-foreground">{t("settlementTitle")}</h3>
            </div>
            <p className="mt-3 text-sm leading-6 text-muted-foreground">
              {t("paymentChannelNote")}
            </p>
          </div>

          <div className="rounded-2xl border border-border bg-card p-5">
            <div className="flex items-center gap-3">
              <span className="rounded-full bg-primary/10 p-2 text-primary">
                <FolderOutput className="h-4 w-4" />
              </span>
              <h3 className="text-base font-semibold text-foreground">{t("afterTitle")}</h3>
            </div>
            <ul className="mt-3 space-y-2 text-sm leading-6 text-muted-foreground">
              {rules.map((rule) => (
                <li key={rule} className="flex items-start gap-2">
                  <CircleCheckBig className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                  <span>{rule}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="rounded-2xl border border-border bg-card p-5">
            <div className="flex items-center gap-3">
              <span className="rounded-full bg-primary/10 p-2 text-primary">
                <ShieldCheck className="h-4 w-4" />
              </span>
              <h3 className="text-base font-semibold text-foreground">{t("previewTitle")}</h3>
            </div>
            <div className="mt-3 flex flex-col gap-3 text-sm">
              <Link
                href="/trial"
                className="inline-flex items-center justify-between rounded-xl border border-border px-4 py-3 text-muted-foreground transition-colors hover:text-foreground"
              >
                <span>{t("linkTrial")}</span>
                <ArrowRight className="h-4 w-4" />
              </Link>
              <Link
                href="/refund"
                className="inline-flex items-center justify-between rounded-xl border border-border px-4 py-3 text-muted-foreground transition-colors hover:text-foreground"
              >
                <span>{t("linkRefund")}</span>
                <ArrowRight className="h-4 w-4" />
              </Link>
              <Link
                href="/contact"
                className="inline-flex items-center justify-between rounded-xl border border-border px-4 py-3 text-muted-foreground transition-colors hover:text-foreground"
              >
                <span>{t("linkContact")}</span>
                <ArrowRight className="h-4 w-4" />
              </Link>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
