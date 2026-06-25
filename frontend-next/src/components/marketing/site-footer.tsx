import { Link } from "@/i18n/navigation"
import { BrandLockup } from "./brand-mark"
import { SUPPORT_EMAIL, SUPPORT_EMAIL_HREF } from "./company-info"

/**
 * Marketing footer with public trust information.
 * The operator identity and support mailbox are shared with the legal pages so
 * payment-partner review sees one consistent public identity.
 */
export function SiteFooter() {
  return (
    <footer className="border-t border-border/70 bg-background">
      <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6 lg:px-8">
        <div className="grid gap-10 md:grid-cols-[1.4fr_1fr_1fr_1fr]">
          <div className="space-y-3">
            <BrandLockup />
            <p className="max-w-sm text-sm text-muted-foreground">
              爱译视频，让世界视频开口说中文。专注长视频的 AI 翻译配音工作台，支持中文字幕、中文配音、多格式导出和逐句修改。
            </p>
          </div>

          <nav aria-label="产品导航" className="space-y-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-foreground">
              产品
            </h3>
            <ul className="space-y-2 text-sm text-muted-foreground">
              <li>
                <Link href="/" className="transition-colors hover:text-foreground">
                  首页
                </Link>
              </li>
              <li>
                <Link href="/pricing" className="transition-colors hover:text-foreground">
                  定价
                </Link>
              </li>
              <li>
                <Link href="/trial" className="transition-colors hover:text-foreground">
                  免费试用
                </Link>
              </li>
              <li>
                <Link href="/auth/login" className="transition-colors hover:text-foreground">
                  登录
                </Link>
              </li>
            </ul>
          </nav>

          <nav aria-label="法律与合规" className="space-y-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-foreground">
              法律与合规
            </h3>
            <ul className="space-y-2 text-sm text-muted-foreground">
              <li>
                <Link href="/terms" className="transition-colors hover:text-foreground">
                  服务条款
                </Link>
              </li>
              <li>
                <Link href="/privacy" className="transition-colors hover:text-foreground">
                  隐私政策
                </Link>
              </li>
              <li>
                <Link href="/refund" className="transition-colors hover:text-foreground">
                  退款政策
                </Link>
              </li>
            </ul>
          </nav>

          <nav aria-label="支持" className="space-y-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-foreground">
              支持
            </h3>
            <ul className="space-y-2 text-sm text-muted-foreground">
              <li>
                <Link href="/contact" className="transition-colors hover:text-foreground">
                  联系我们
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
          © {new Date().getFullYear()} 爱译视频 AITrans.Video · 长视频翻译配音工作台
        </div>
      </div>
    </footer>
  )
}
