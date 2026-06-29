import type { Metadata } from "next"
import { notFound } from "next/navigation"
import { hasLocale, NextIntlClientProvider } from "next-intl"
import { setRequestLocale, getMessages, getTranslations } from "next-intl/server"
import "../globals.css"
import { Toaster } from "@/components/ui/sonner"
import { SessionProvider } from "@/components/providers/session-provider"
import { localeSeo, siteUrl, type Locale } from "@/lib/seo/site"
import { routing } from "@/i18n/routing"

/** zh 默认 keywords —— 改造前 static 对象的字面列表，逐字节保留（红线 R1）。 */
const ZH_KEYWORDS = [
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
]

/** en keywords —— zh 11 个关键词的英文对应集；两个品牌写法（爱译视频 / AITrans.Video）合并为单一 AITrans.Video → 10 条（UI-03d-1 翻旗新增）。 */
const EN_KEYWORDS = [
  "English video translation",
  "AI video translation",
  "Chinese voiceover",
  "AI dubbing",
  "AI subtitles",
  "YouTube video translation",
  "long video translation",
  "SRT subtitle export",
  "video localization",
  "AITrans.Video",
]

/**
 * 本地化主子树的 **唯一 root layout**（UI-02：删顶层 app/layout.tsx 后此为 root）。
 * Root metadata（UI-03d-1：static → generateMetadata 以按 locale 本地化窄项）—— 保持窄：
 * title 模板/默认/描述/keywords/OG siteName·locale + metadataBase/verification 留这里。
 *
 * **R4 红线（防泄漏）**：本 layout **绝不**含 `alternates`（canonical/hreflang）或
 * `openGraph.url` —— 这些仅在 page 级产出（GEO §7.4）。
 * **R1 红线（zh 字节一致）**：locale==="zh" 时输出与改造前 static 对象逐字节相同
 * （title/template/description/keywords/OG siteName=爱译视频/locale=zh_CN/verification 全不变）。
 */
export async function generateMetadata({
  params,
}: {
  params: Promise<{ locale: Locale }>
}): Promise<Metadata> {
  const { locale } = await params
  const seo = localeSeo[locale]
  return {
    metadataBase: new URL(siteUrl),
    title: {
      default: seo.defaultTitle,
      template: locale === "en" ? "%s · AITrans.Video" : "%s · 爱译视频 AITrans.Video",
    },
    description: seo.defaultDescription,
    keywords: locale === "en" ? EN_KEYWORDS : ZH_KEYWORDS,
    openGraph: {
      siteName: seo.siteName,
      locale: locale === "en" ? "en_US" : "zh_CN",
      type: "website",
    },
    verification: {
      google: "VSf8VEhNmB5UDyf3asBHgFJtagelrwzkiC7xvpm5Hrs",
      other: {
        "msvalidate.01": "2AE8618E42C11345B5006A2EA9084308",
      },
    },
  }
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
  const t = await getTranslations("common")
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
        <a href="#main-content" className="skip-to-main">{t("skipToMain")}</a>
        <NextIntlClientProvider messages={messages}>
          <SessionProvider>{children}</SessionProvider>
        </NextIntlClientProvider>
        <Toaster position="top-center" richColors />
      </body>
    </html>
  )
}
