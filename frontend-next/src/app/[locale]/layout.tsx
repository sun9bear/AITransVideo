import type { Metadata } from "next"
import { notFound } from "next/navigation"
import { hasLocale, NextIntlClientProvider } from "next-intl"
import { setRequestLocale, getMessages } from "next-intl/server"
import "../globals.css"
import { Toaster } from "@/components/ui/sonner"
import { SessionProvider } from "@/components/providers/session-provider"
import { defaultDescription, defaultTitle, siteName, siteUrl } from "@/lib/seo/site"
import { routing } from "@/i18n/routing"

/**
 * 本地化主子树的 **唯一 root layout**（UI-02：删顶层 app/layout.tsx 后此为 root）。
 * Root metadata — 保持窄：title 模板/默认描述/OG locale/verification 等 fall-through 项留这里；
 * canonical / 营销专属 OG title·description 仍只在 page 级（GEO §7.4，红线 4）。
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
  openGraph: {
    siteName,
    locale: "zh_CN",
    type: "website",
  },
  verification: {
    google: "VSf8VEhNmB5UDyf3asBHgFJtagelrwzkiC7xvpm5Hrs",
    other: {
      "msvalidate.01": "2AE8618E42C11345B5006A2EA9084308",
    },
  },
}

export function generateStaticParams() {
  return routing.locales.map((locale) => ({ locale }))
}

export default async function LocaleLayout({
  children,
  params,
}: Readonly<{
  children: React.ReactNode
  params: Promise<{ locale: string }>
}>) {
  const { locale } = await params
  if (!hasLocale(routing.locales, locale)) {
    notFound()
  }
  // 让本 layout + 其下静态页可静态渲染（漏调会静默退化为 dynamic，R10）。
  setRequestLocale(locale)
  const messages = await getMessages()
  // zh→zh-Hans（注：旧 root 为 zh-CN，此为已登记的单点字节豁免），en→en。
  const htmlLang = locale === "zh" ? "zh-Hans" : "en"

  return (
    <html lang={htmlLang} className="dark h-full antialiased">
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
        <NextIntlClientProvider messages={messages}>
          <SessionProvider>{children}</SessionProvider>
        </NextIntlClientProvider>
        <Toaster position="top-center" richColors />
      </body>
    </html>
  )
}
