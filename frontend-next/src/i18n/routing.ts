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
  localeCookie: { maxAge: 60 * 60 * 24 * 365 },
})
