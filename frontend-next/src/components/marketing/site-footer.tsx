import Link from "next/link"
import { BrandLockup } from "./brand-mark"

/**
 * Marketing layer footer — minimal and functional. No drama, no oversized type.
 * Uses the same foundation tokens as the rest of the app.
 */
export function SiteFooter() {
  return (
    <footer className="border-t border-border/70 bg-background">
      <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6 lg:px-8">
        <div className="flex flex-col gap-6 md:flex-row md:items-center md:justify-between">
          <div className="space-y-2">
            <BrandLockup />
            <p className="text-sm text-muted-foreground max-w-md">
              专注长视频的 AI 翻译配音工作台，精准对齐，直出剪映草稿。
            </p>
          </div>
          <nav className="flex flex-wrap gap-x-6 gap-y-2 text-sm text-muted-foreground" aria-label="页脚导航">
            <Link href="/" className="hover:text-foreground transition-colors">
              首页
            </Link>
            <Link href="/pricing" className="hover:text-foreground transition-colors">
              定价
            </Link>
            <Link href="/trial" className="hover:text-foreground transition-colors">
              免费试用
            </Link>
            <Link href="/auth/login" className="hover:text-foreground transition-colors">
              登录
            </Link>
          </nav>
        </div>
        <div className="mt-8 border-t border-border/60 pt-6 text-xs text-muted-foreground">
          © {new Date().getFullYear()} AIVideoTrans · 专业视频本地化工作台
        </div>
      </div>
    </footer>
  )
}
