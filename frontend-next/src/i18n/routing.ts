import { defineRouting } from "next-intl/routing"

/**
 * 界面语言路由配置（UI page locale）。
 * - 默认 zh 裸路径（`localePrefix: "as-needed"`）；en 走 `/en`。
 * - `localeDetection: false`：URL 是唯一语言真源，绝不按 cookie/Accept-Language 自动重定向
 *   （方案 §1.2a / 红线 8）。cookie 仅作偏好提示。
 * 本单元（UI-01）只声明配置，不接入 proxy（结构迁移在 UI-02）。
 */
export const routing = defineRouting({
  locales: ["zh", "en"],
  defaultLocale: "zh",
  localePrefix: "as-needed",
  localeDetection: false,
  // alternateLinks:false 关掉 next-intl middleware 给【每条】路由自动 emit 的
  // `Link: rel="alternate" hreflang` 响应头。该通道绕过 site.ts/localizedRoutes，会把未翻旗的
  // legal 页（/terms /privacy /refund）也挂上 en hreflang（2026-06-29 生产实测 /terms 响应头含
  // en，违反「legal 不翻旗」口径）。关掉后 hreflang 只由 page 级 generateMetadata 产出：localized
  // 路由仍互惠（HTML <link rel=alternate>），legal 干净无 en，并顺带消除 header 用 `zh`/HTML 用
  // `zh-Hans` 的重复不一致。守卫见 scripts/hreflang-check.mjs。
  alternateLinks: false,
  localeCookie: { maxAge: 60 * 60 * 24 * 365 },
})
