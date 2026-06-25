import "../globals.css"

/**
 * Locale-neutral 独立 root layout（UI-02 / 方案 §1.5 / CodeX 二审 #3）。
 * 删顶层 app/layout.tsx 后，`[locale]` 之外的 paddle-checkout 必须自带 root（含 <html>/<body>），
 * 否则 build 报错。它是支付 handoff，硬编码 zh-Hans 传 Paddle.js，与界面 locale 解耦，
 * 故不包 NextIntlClientProvider。
 */
export default function PaddleCheckoutLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="zh-Hans" className="dark h-full antialiased">
      <body className="min-h-full bg-background">{children}</body>
    </html>
  )
}
