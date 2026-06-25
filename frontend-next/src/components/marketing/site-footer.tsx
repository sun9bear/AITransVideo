import { getTranslations } from "next-intl/server"
import { Link } from "@/i18n/navigation"
import { BrandLockup } from "./brand-mark"
import { SUPPORT_EMAIL, SUPPORT_EMAIL_HREF } from "./company-info"

/**
 * Marketing footer with public trust information.
 * The operator identity and support mailbox are shared with the legal pages so
 * payment-partner review sees one consistent public identity.
 *
 * i18n (UI-03a): chrome (tagline / column titles / link labels / aria / copyright)
 * lives in messages/{zh,en}/marketing.json under `marketing.footer`. The support
 * email + operator identity stay as content constants (company-info.ts), not
 * translated. Server component → getTranslations.
 */
export async function SiteFooter() {
  const t = await getTranslations("marketing.footer")

  return (
    <footer className="border-t border-border/70 bg-background">
      <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6 lg:px-8">
        <div className="grid gap-10 md:grid-cols-[1.4fr_1fr_1fr_1fr]">
          <div className="space-y-3">
            <BrandLockup />
            <p className="max-w-sm text-sm text-muted-foreground">
              {t("tagline")}
            </p>
          </div>

          <nav aria-label={t("ariaProductNav")} className="space-y-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-foreground">
              {t("colProduct")}
            </h3>
            <ul className="space-y-2 text-sm text-muted-foreground">
              <li>
                <Link href="/" className="transition-colors hover:text-foreground">
                  {t("linkHome")}
                </Link>
              </li>
              <li>
                <Link href="/pricing" className="transition-colors hover:text-foreground">
                  {t("linkPricing")}
                </Link>
              </li>
              <li>
                <Link href="/trial" className="transition-colors hover:text-foreground">
                  {t("linkTrial")}
                </Link>
              </li>
              <li>
                <Link href="/auth/login" className="transition-colors hover:text-foreground">
                  {t("linkLogin")}
                </Link>
              </li>
            </ul>
          </nav>

          <nav aria-label={t("ariaLegalNav")} className="space-y-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-foreground">
              {t("colLegal")}
            </h3>
            <ul className="space-y-2 text-sm text-muted-foreground">
              <li>
                <Link href="/terms" className="transition-colors hover:text-foreground">
                  {t("linkTerms")}
                </Link>
              </li>
              <li>
                <Link href="/privacy" className="transition-colors hover:text-foreground">
                  {t("linkPrivacy")}
                </Link>
              </li>
              <li>
                <Link href="/refund" className="transition-colors hover:text-foreground">
                  {t("linkRefund")}
                </Link>
              </li>
            </ul>
          </nav>

          <nav aria-label={t("ariaSupportNav")} className="space-y-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-foreground">
              {t("colSupport")}
            </h3>
            <ul className="space-y-2 text-sm text-muted-foreground">
              <li>
                <Link href="/contact" className="transition-colors hover:text-foreground">
                  {t("linkContact")}
                </Link>
              </li>
              <li>
                <a href={SUPPORT_EMAIL_HREF} className="transition-colors hover:text-foreground">
                  {SUPPORT_EMAIL}
                </a>
              </li>
            </ul>
          </nav>
        </div>

        {/* Copyright row only — full operator identity (运营主体 / 联系邮箱 /
            联系地址 / 支付渠道说明) lives on the legal pages (Contact / Terms /
            Privacy / Refund) via the shared ContactBlock component. Keeping it
            duplicated in the footer cluttered the rice-paper aesthetic without
            adding compliance value. */}
        <div className="mt-10 border-t border-border/60 pt-6 text-xs text-muted-foreground">
          {t("copyright", { year: new Date().getFullYear() })}
        </div>
      </div>
    </footer>
  )
}
