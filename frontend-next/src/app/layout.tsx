import type { Metadata } from "next"
import "./globals.css"
import { Toaster } from "@/components/ui/sonner"
import { SessionProvider } from "@/components/providers/session-provider"

export const metadata: Metadata = {
  title: {
    default: "爱译视频 · 让世界视频开口说中文",
    template: "%s · 爱译视频 AIVideoTrans",
  },
  description:
    "面向中文创作者的 AI 视频翻译配音工作台。支持最长 3 小时视频，自动生成中文字幕、配音和多种交付结果，工作台里逐句修改，不满意就单段重生成。",
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
