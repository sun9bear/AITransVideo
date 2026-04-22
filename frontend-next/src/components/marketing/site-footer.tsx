import Link from "next/link"
import { BrandLockup } from "./brand-mark"
import {
  COMPANY_ADDRESS,
  COMPANY_NAME,
  PAYMENT_CHANNEL_NOTE,
  SUPPORT_EMAIL,
  SUPPORT_EMAIL_HREF,
} from "./company-info"

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
              专注长视频的 AI 翻译配音工作台，精准对齐，直出剪映草稿。
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

        <div className="mt-10 flex flex-col gap-3 border-t border-border/60 pt-6 text-xs text-muted-foreground sm:flex-row sm:items-start sm:justify-between">
          <div>© {new Date().getFullYear()} AIVideoTrans · 专业视频本地化工作台</div>
          <div className="flex max-w-3xl flex-wrap gap-x-3 gap-y-1 sm:justify-end">
            <span>运营主体：{COMPANY_NAME}</span>
            <span aria-hidden="true">·</span>
            <span>联系邮箱：{SUPPORT_EMAIL}</span>
            <span aria-hidden="true">·</span>
            <span>联系地址：{COMPANY_ADDRESS}</span>
            <span aria-hidden="true">·</span>
            <span>{PAYMENT_CHANNEL_NOTE}</span>
          </div>
        </div>
      </div>
    </footer>
  )
}
