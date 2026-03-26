import type { Metadata } from "next"
import "./globals.css"
import { Toaster } from "@/components/ui/sonner"
import { AppShell } from "@/components/app-shell"

export const metadata: Metadata = {
  title: "AIVideoTrans",
  description: "AI 视频翻译配音工作台",
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
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Noto+Sans+SC:wght@400;500;700&display=swap" rel="stylesheet" />
      </head>
      <body className="min-h-full bg-background">
        <a href="#main-content" className="skip-to-main">跳到主内容</a>
        <AppShell>{children}</AppShell>
        <Toaster position="top-center" richColors />
      </body>
    </html>
  )
}
