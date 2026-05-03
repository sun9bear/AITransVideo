import type { Metadata } from "next"
import "./globals.css"
import { Toaster } from "@/components/ui/sonner"
import { SessionProvider } from "@/components/providers/session-provider"
import { defaultDescription, defaultTitle, siteName, siteUrl } from "@/lib/seo/site"

/**
 * Root metadata — kept narrow on purpose.
 *
 * This layout wraps both `(marketing)` and `(app)` route groups, so anything
 * declared here leaks into `/workspace`, `/projects`, `/admin` etc. Things
 * that are safe to fall through (title template, default description as a
 * fallback before a page declares its own) stay here. Things that would
 * mis-attribute logged-in pages (`alternates.canonical`, marketing-only OG
 * title/description) MUST be declared per-page, not here.
 *
 * See docs/plans/2026-05-03-geo-optimization-plan.md §7.4.
 */
export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: defaultTitle,
    template: "%s · 爱译视频 AITrans.Video",
  },
  description: defaultDescription,
  keywords: [
    "英文视频翻译",
    "AI 视频翻译",
    "中文配音",
    "AI 配音",
    "AI 字幕",
    "YouTube 视频翻译",
    "长视频翻译",
    "SRT 字幕导出",
    "视频本地化",
    "爱译视频",
    "AITrans.Video",
  ],
  // Defaults only — per-page metadata overrides title/description/url.
  // Locale + siteName + type bubble through to legal/contact pages that
  // don't declare their own openGraph block.
  openGraph: {
    siteName,
    locale: "zh_CN",
    type: "website",
  },
  // Search-console site-verification tokens. Next.js renders these as the
  // appropriate <meta> tags in the homepage <head>, which both tools accept
  // for ownership verification. Adding more tokens here (e.g. Bing
  // `msvalidate.01` under `verification.other`) is the single-deploy way to
  // prove ownership across multiple webmaster tools.
  verification: {
    google: "VSf8VEhNmB5UDyf3asBHgFJtagelrwzkiC7xvpm5Hrs",
    other: {
      "msvalidate.01": "2AE8618E42C11345B5006A2EA9084308",
    },
  },
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="zh-CN" className="dark h-full antialiased">
      <head>
        <meta name="theme-color" content="#0a1628" media="(prefers-color-scheme: dark)" />
        <meta name="theme-color" content="#f5f5f4" media="(prefers-color-scheme: light)" />
        <meta name="color-scheme" content="dark light" />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Noto+Sans+SC:wght@400;500;700&family=Noto+Serif+SC:wght@600;900&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="min-h-full bg-background">
        <a href="#main-content" className="skip-to-main">跳到主内容</a>
        <SessionProvider>
          {children}
        </SessionProvider>
        <Toaster position="top-center" richColors />
      </body>
    </html>
  )
}
